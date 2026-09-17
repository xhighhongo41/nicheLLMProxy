"""Shared relay flow for image-generation listener modes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from niche_llm_proxy.config import ListenerRuntimeConfig
from niche_llm_proxy.logging_feature import ExchangeLog
from niche_llm_proxy.passthrough import (
    build_upstream_url,
    create_http_client,
    prepare_request_headers,
    prepare_response_headers,
    upstream_error_detail,
)

_LOGGER = logging.getLogger(__name__)

RequestTransform = Callable[[bytes, str | None], bytes]
ResponseTransform = Callable[[bytes], bytes]
QueryTransform = Callable[[str | None], str | None]


class TransformError(Exception):
    """Raised when a request or response body cannot be transformed."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def ensure_created(body: bytes) -> bytes:
    """Ensure an upstream response includes a top-level created timestamp."""

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _LOGGER.warning("Upstream response is not valid JSON; passing through unchanged.")
        return body

    if not isinstance(data, dict):
        _LOGGER.warning("Upstream response is not a JSON object; passing through unchanged.")
        return body

    if "created" in data:
        return body

    data["created"] = int(time.time())
    return json.dumps(data).encode("utf-8")


async def relay_upstream(
    config: ListenerRuntimeConfig,
    exchange: ExchangeLog | None,
    upstream_transport: httpx.AsyncBaseTransport | None,
    request: Request,
    upstream_path: str,
    *,
    transform_request: RequestTransform | None = None,
    transform_response: ResponseTransform | None = None,
    transform_query: QueryTransform | None = None,
    upstream_content_type: str | None = None,
    error_event: str,
) -> Response:
    """Relay a request to the upstream with optional body transformations.

    The request body is sent unchanged when ``transform_request`` is None,
    which supports pass-through relays such as model listings. Transforms
    receive the request body and its Content-Type header value (None when
    absent). The response body is transformed only for upstream 200
    responses. The query string is forwarded unchanged when
    ``transform_query`` is None. The forwarded Content-Type is replaced with
    ``upstream_content_type`` when it is set; otherwise the original header
    is forwarded unchanged.
    """

    body = await _read_request_body(request, exchange)

    if transform_request is None:
        upstream_body = body
    else:
        try:
            upstream_body = transform_request(body, request.headers.get("content-type"))
        except TransformError as error:
            if exchange is not None:
                exchange.fail(error_event, error)
            return JSONResponse(status_code=error.status_code, content={"detail": error.message})

    if exchange is not None:
        exchange.emit(
            "upstream_request_sent",
            upstream_request_bytes=len(upstream_body),
            upstream_request_sha256=hashlib.sha256(upstream_body).hexdigest(),
        )

    client = create_http_client(config, transport=upstream_transport)
    # Strip content-length (recomputed by httpx for the transformed body) and
    # accept-encoding so httpx advertises only the encodings it can decode.
    # Otherwise an upstream could answer with e.g. br, which aread() would
    # return undecoded.
    forwarded_headers = [
        (name, value)
        for name, value in prepare_request_headers(request.headers, config.upstream.api_key)
        if name.lower() not in (b"content-length", b"accept-encoding")
    ]
    if upstream_content_type is not None:
        forwarded_headers = [
            (name, value)
            for name, value in forwarded_headers
            if name.lower() != b"content-type"
        ]
        forwarded_headers.append((b"content-type", upstream_content_type.encode("latin-1")))
    query = request.url.query
    if transform_query is not None:
        query = transform_query(query)
    upstream_request = client.build_request(
        request.method,
        build_upstream_url(config.upstream.base_url, upstream_path, query),
        headers=forwarded_headers,
        content=upstream_body,
    )

    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.RequestError as error:
        await client.aclose()
        if exchange is not None:
            exchange.fail("upstream_send", error)
        status_code, content = upstream_error_detail(error)
        return JSONResponse(status_code=status_code, content=content)
    except asyncio.CancelledError:
        await client.aclose()
        if exchange is not None:
            exchange.cancel("upstream_send")
        raise
    except BaseException as error:
        await client.aclose()
        if exchange is not None:
            exchange.fail("upstream_send", error)
        raise

    if exchange is not None:
        exchange.response_started(
            upstream_response.status_code,
            upstream_response.headers.raw,
            config.upstream.base_url,
        )

    try:
        upstream_body_raw = await upstream_response.aread()
    except httpx.RequestError as error:
        if exchange is not None:
            exchange.fail("response_read", error)
        status_code, content = upstream_error_detail(error)
        return JSONResponse(status_code=status_code, content=content)
    except asyncio.CancelledError:
        if exchange is not None:
            exchange.cancel("response_read")
        raise
    finally:
        await upstream_response.aclose()
        await client.aclose()

    if exchange is not None:
        exchange.observe_response(upstream_body_raw)
        exchange.complete()

    payload = upstream_body_raw
    if upstream_response.status_code == 200 and transform_response is not None:
        payload = transform_response(upstream_body_raw)

    response = Response(
        content=payload,
        status_code=upstream_response.status_code,
    )
    response.raw_headers = _response_headers(upstream_response, payload)
    return response


async def _read_request_body(
    request: Request,
    exchange: ExchangeLog | None,
) -> bytes:
    """Read the original request body while observing it for logging."""

    if exchange is None:
        return await request.body()
    chunks: list[bytes] = []
    async for chunk in exchange.wrap_request(request.stream()):
        chunks.append(chunk)
    return b"".join(chunks)


def _response_headers(
    upstream_response: httpx.Response,
    payload: bytes,
) -> list[tuple[bytes, bytes]]:
    """Prepare upstream response headers with a recomputed content length.

    ``content-encoding`` is dropped because ``aread()`` returns the upstream
    body already decoded, so relaying the original header would advertise a
    compression that the payload no longer uses.
    """

    headers = [
        (name, value)
        for name, value in prepare_response_headers(upstream_response.headers)
        if name.lower() not in (b"content-length", b"content-encoding")
    ]
    headers.append((b"content-length", str(len(payload)).encode("ascii")))
    return headers