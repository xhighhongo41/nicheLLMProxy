"""Image generation request/response transformation for xAI Grok."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from enum import Enum
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from niche_llm_proxy.config import GrokImageConfig, ProxyConfig
from niche_llm_proxy.i18n import translate
from niche_llm_proxy.logging_feature import ExchangeLog
from niche_llm_proxy.passthrough import (
    build_upstream_url,
    create_http_client,
    prepare_request_headers,
    prepare_response_headers,
    upstream_error_detail,
)

_LOGGER = logging.getLogger(__name__)

_REMOVED_KEYS = frozenset(
    {
        "size",
        "quality",
        "style",
        "seed",
        "background",
        "moderation",
        "output_format",
        "output_compression",
    }
)


class RequestKind(Enum):
    """Classification of an incoming grok-image request."""

    GENERATIONS = "generations"
    MODELS = "models"
    IMAGE_GENERATION_MODELS = "image_generation_models"
    UNSUPPORTED_PATH = "unsupported_path"
    METHOD_NOT_ALLOWED = "method_not_allowed"


class TransformError(Exception):
    """Raised when a request or response body cannot be transformed."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def classify_request(path: str, method: str) -> RequestKind:
    """Classify an incoming request by path and HTTP method."""

    clean_path = path.split("?", 1)[0]
    upper_method = method.upper()

    route_methods = {
        "/v1/images/generations": ("POST", RequestKind.GENERATIONS),
        "/v1/models": ("GET", RequestKind.MODELS),
        "/v1/image-generation-models": ("GET", RequestKind.IMAGE_GENERATION_MODELS),
    }

    if clean_path not in route_methods:
        return RequestKind.UNSUPPORTED_PATH

    expected_method, kind = route_methods[clean_path]
    if upper_method != expected_method:
        return RequestKind.METHOD_NOT_ALLOWED
    return kind


def transform_request_body(body: bytes, settings: GrokImageConfig | None) -> bytes:
    """Transform an OpenAI-style images/generations request for Grok."""

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TransformError(
            status_code=400,
            message=translate(
                "Request body must be a JSON object in 'grok-image' mode."
            ),
        ) from error

    if not isinstance(data, dict):
        raise TransformError(
            status_code=400,
            message=translate(
                "Request body must be a JSON object in 'grok-image' mode."
            ),
        )

    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise TransformError(
            status_code=400,
            message=translate("'prompt' must be a non-empty string."),
        )

    output: dict[str, Any] = {"prompt": prompt}

    model = data.get("model")
    if model is None and settings is not None:
        model = settings.default_model
    if model is None:
        raise TransformError(
            status_code=400,
            message=translate(
                "'model' is required when no default model is configured."
            ),
        )
    output["model"] = model

    n = data.get("n")
    if n is not None:
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 10:
            raise TransformError(
                status_code=400,
                message=translate("'n' must be an integer between 1 and 10."),
            )
        output["n"] = n

    response_format = data.get("response_format")
    if response_format is None:
        response_format = "b64_json"
    if (
        not isinstance(response_format, str)
        or response_format not in {"url", "b64_json"}
    ):
        raise TransformError(
            status_code=400,
            message=translate("'response_format' must be 'url' or 'b64_json'."),
        )
    output["response_format"] = response_format

    for key, value in data.items():
        if key in _REMOVED_KEYS or key in output:
            continue
        output[key] = value

    if settings is not None:
        if "aspect_ratio" not in output and settings.aspect_ratio is not None:
            output["aspect_ratio"] = settings.aspect_ratio
        if "resolution" not in output and settings.resolution is not None:
            output["resolution"] = settings.resolution

    return json.dumps(output).encode("utf-8")


def transform_response_body(body: bytes) -> bytes:
    """Ensure an upstream response includes a top-level created timestamp."""

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _LOGGER.warning("Upstream response is not valid JSON; passing through unchanged.")
        return body

    if not isinstance(data, dict):
        _LOGGER.warning("Upstream response is not a JSON object; passing through unchanged.")
        return body

    if "created" in data:
        return body

    data["created"] = int(time.time())
    return json.dumps(data).encode("utf-8")


async def handle_grok_image_generations(
    config: ProxyConfig,
    exchange: ExchangeLog | None,
    upstream_transport: httpx.AsyncBaseTransport | None,
    request: Request,
) -> Response:
    """Relay an images/generations request with Grok-specific transformation."""

    body = await _read_request_body(request, exchange)
    try:
        upstream_body = transform_request_body(body, config.listener.grok_image)
    except TransformError as error:
        if exchange is not None:
            exchange.fail("grok_image_transform", error)
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
        build_upstream_url(config.upstream.base_url, request.url.path, request.url.query),
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

    payload = (
        transform_response_body(upstream_body_raw)
        if upstream_response.status_code == 200
        else upstream_body_raw
    )
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
