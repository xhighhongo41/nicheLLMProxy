"""Image generation request/response transformation for xAI Grok."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import Response

from niche_llm_proxy.config import GrokImageConfig, ProxyConfig
from niche_llm_proxy.i18n import translate
from niche_llm_proxy.image_relay import (
    TransformError,
    ensure_created,
    relay_upstream,
)
from niche_llm_proxy.logging_feature import ExchangeLog

transform_response_body = ensure_created
"""Backward-compatible alias for :func:`niche_llm_proxy.image_relay.ensure_created`."""

__all__ = [
    "RequestKind",
    "TransformError",
    "classify_request",
    "handle_grok_image_generations",
    "transform_request_body",
    "transform_response_body",
]

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


async def handle_grok_image_generations(
    config: ProxyConfig,
    exchange: ExchangeLog | None,
    upstream_transport: httpx.AsyncBaseTransport | None,
    request: Request,
) -> Response:
    """Relay an images/generations request with Grok-specific transformation."""

    return await relay_upstream(
        config,
        exchange,
        upstream_transport,
        request,
        request.url.path,
        transform_request=lambda body: transform_request_body(
            body, config.listener.grok_image
        ),
        transform_response=ensure_created,
        error_event="grok_image_transform",
    )