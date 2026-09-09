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

from niche_llm_proxy.config import ProxyConfig
from niche_llm_proxy.logging_feature import ExchangeLog
from niche_llm_proxy.passthrough import (
    build_upstream_url,
    create_http_client,
    prepare_request_headers,
    prepare_response_headers,
    upstream_error_detail,
)

_LOGGER = logging.getLogger(__name__)

BodyTransform = Callable[[bytes], bytes]


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
    config: ProxyConfig,
    exchange: ExchangeLog | None,
    upstream_transport: httpx.AsyncBaseTransport | None,
    request: Request,
    upstream_path: str,
    *,
    transform_request: BodyTransform | None = None,
    transform_response: BodyTransform | None = None,
    error_event: str,
) -> Response:
    """Relay a request to the upstream with optional body transformations.

    The request body is sent unchanged when ``transform_request`` is None,
    which supports pass-through relays such as model listings. The response
    body is transformed only for upstream 200 responses.
    """

    body = await _read_request_body(request, exchange)

    if transform_request is None:
        upstream_body = body
    else:
        try:
            upstream_body = transform_request(body)
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
    upstream_request = client.build_request(
        request.method,
        build_upstream_url(config.upstream.base_url, upstream_path, request.url.query),
        headers=prepare_request_headers(request.headers, config.upstream.api_key),
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
    """Prepare upstream response headers with a recomputed content length."""

    headers = [
        (name, value)
        for name, value in prepare_response_headers(upstream_response.headers)
        if name.lower() != b"content-length"
    ]
    headers.append((b"content-length", str(len(payload)).encode("ascii")))
    return headers