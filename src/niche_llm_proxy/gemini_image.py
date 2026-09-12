"""Image generation request/response transformation for Google Gemini."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import Response

from niche_llm_proxy.config import GeminiImageConfig, ProxyConfig
from niche_llm_proxy.i18n import translate
from niche_llm_proxy.image_relay import (
    TransformError,
    ensure_created,
    relay_upstream,
)
from niche_llm_proxy.logging_feature import ExchangeLog

GEMINI_GENERATIONS_PATH = "/v1beta/openai/images/generations"
GEMINI_MODELS_PATH = "/v1beta/openai/models"


class GeminiRequestKind(Enum):
    """Classification of an incoming gemini-image request."""

    GENERATIONS = "generations"
    MODELS = "models"
    UNSUPPORTED_PATH = "unsupported_path"
    METHOD_NOT_ALLOWED = "method_not_allowed"


def classify_request(path: str, method: str) -> GeminiRequestKind:
    """Classify an incoming request by path and HTTP method."""

    clean_path = path.split("?", 1)[0]
    upper_method = method.upper()

    route_methods = {
        "/v1/images/generations": ("POST", GeminiRequestKind.GENERATIONS),
        "/v1/models": ("GET", GeminiRequestKind.MODELS),
    }

    if clean_path not in route_methods:
        return GeminiRequestKind.UNSUPPORTED_PATH

    expected_method, kind = route_methods[clean_path]
    if upper_method != expected_method:
        return GeminiRequestKind.METHOD_NOT_ALLOWED
    return kind


def transform_request_body(body: bytes, settings: GeminiImageConfig | None) -> bytes:
    """Transform an OpenAI-style images/generations request for Gemini."""

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TransformError(
            status_code=400,
            message=translate(
                "Request body must be a JSON object in 'gemini-image' mode."
            ),
        ) from error

    if not isinstance(data, dict):
        raise TransformError(
            status_code=400,
            message=translate(
                "Request body must be a JSON object in 'gemini-image' mode."
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
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise TransformError(
                status_code=400,
                message=translate("'n' must be an integer equal to 1."),
            )
        if n > 1:
            raise TransformError(
                status_code=400,
                message=translate(
                    "'n' greater than 1 is not supported in 'gemini-image' mode."
                ),
            )
        output["n"] = n

    response_format = data.get("response_format")
    if response_format is None:
        response_format = "b64_json"
    if not isinstance(response_format, str) or response_format != "b64_json":
        raise TransformError(
            status_code=400,
            message=translate(
                "'response_format' must be 'b64_json' in 'gemini-image' mode."
            ),
        )
    output["response_format"] = response_format

    for key, value in data.items():
        if key in output:
            continue
        output[key] = value

    if (
        settings is not None
        and settings.aspect_ratio is not None
        and "size" not in output
        and "aspect_ratio" not in output
    ):
        output["aspect_ratio"] = settings.aspect_ratio

    return json.dumps(output).encode("utf-8")


async def handle_gemini_image_generations(
    config: ProxyConfig,
    exchange: ExchangeLog | None,
    upstream_transport: httpx.AsyncBaseTransport | None,
    request: Request,
) -> Response:
    """Relay an images/generations request with Gemini-specific transformation."""

    return await relay_upstream(
        config,
        exchange,
        upstream_transport,
        request,
        GEMINI_GENERATIONS_PATH,
        transform_request=lambda body: transform_request_body(
            body, config.listener.gemini_image
        ),
        transform_response=ensure_created,
        error_event="gemini_image_transform",
    )


async def handle_gemini_models(
    config: ProxyConfig,
    exchange: ExchangeLog | None,
    upstream_transport: httpx.AsyncBaseTransport | None,
    request: Request,
) -> Response:
    """Relay a models listing to the Gemini OpenAI-compatible layer unchanged."""

    return await relay_upstream(
        config,
        exchange,
        upstream_transport,
        request,
        GEMINI_MODELS_PATH,
        error_event="gemini_image_transform",
    )