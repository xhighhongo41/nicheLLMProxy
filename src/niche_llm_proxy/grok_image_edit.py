"""Image edit request/response transformation for xAI Grok."""

from __future__ import annotations

import base64
import json
from enum import Enum
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import Response

from niche_llm_proxy.config import GrokImageEditConfig, ListenerRuntimeConfig
from niche_llm_proxy.i18n import translate
from niche_llm_proxy.image_relay import (
    TransformError,
    ensure_created,
    relay_upstream,
)
from niche_llm_proxy.logging_feature import ExchangeLog
from niche_llm_proxy.multipart_forms import (
    MultipartError,
    detect_image_type,
    parse_multipart,
)

transform_response_body = ensure_created
"""Backward-compatible alias for :func:`niche_llm_proxy.image_relay.ensure_created`."""

__all__ = [
    "RequestKind",
    "TransformError",
    "classify_request",
    "handle_grok_image_edit_edits",
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

_IMAGE_NAMES = frozenset({"image", "image[]"})


class RequestKind(Enum):
    """Classification of an incoming grok-image-edit request."""

    EDITS = "edits"
    MODELS = "models"
    IMAGE_GENERATION_MODELS = "image_generation_models"
    UNSUPPORTED_PATH = "unsupported_path"
    METHOD_NOT_ALLOWED = "method_not_allowed"


def classify_request(path: str, method: str) -> RequestKind:
    """Classify an incoming request by path and HTTP method."""

    clean_path = path.split("?", 1)[0]
    upper_method = method.upper()

    route_methods = {
        "/v1/images/edits": ("POST", RequestKind.EDITS),
        "/v1/models": ("GET", RequestKind.MODELS),
        "/v1/image-generation-models": ("GET", RequestKind.IMAGE_GENERATION_MODELS),
    }

    if clean_path not in route_methods:
        return RequestKind.UNSUPPORTED_PATH

    expected_method, kind = route_methods[clean_path]
    if upper_method != expected_method:
        return RequestKind.METHOD_NOT_ALLOWED
    return kind


def transform_request_body(
    body: bytes, content_type: str | None, settings: GrokImageEditConfig | None
) -> bytes:
    """Transform an OpenAI-style images/edits multipart request for Grok."""

    if content_type is None or not content_type.lower().startswith("multipart/"):
        raise TransformError(
            status_code=400,
            message=translate(
                "Request body must be multipart/form-data in 'grok-image-edit' mode."
            ),
        )

    try:
        parts = parse_multipart(body, content_type)
    except MultipartError as error:
        raise TransformError(
            status_code=400,
            message=translate("Malformed multipart/form-data body."),
        ) from error

    output: dict[str, Any] = {}
    images: list[str] = []
    for part in parts:
        if part.name == "mask":
            raise TransformError(
                status_code=400,
                message=translate(
                    "mask is not supported in 'grok-image-edit' mode."
                ),
            )

        if part.filename is not None:
            if part.name not in _IMAGE_NAMES:
                raise TransformError(
                    status_code=400,
                    message=translate(
                        "Unexpected file part '{name}' was rejected.",
                        name=part.name,
                    ),
                )
            mime = detect_image_type(part.data)
            if mime is None:
                raise TransformError(
                    status_code=400,
                    message=translate(
                        "Unsupported image type. Only PNG, JPEG and WebP are accepted."
                    ),
                )
            images.append(
                f"data:{mime};base64,{base64.b64encode(part.data).decode('ascii')}"
            )
            continue

        if part.name in _IMAGE_NAMES:
            try:
                value = part.data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise TransformError(
                    status_code=400,
                    message=translate("Malformed multipart/form-data body."),
                ) from error
            if not value.startswith(("http://", "https://", "data:")):
                raise TransformError(
                    status_code=400,
                    message=translate(
                        "'image' must be an http(s) or data URL when sent as a text field."
                    ),
                )
            images.append(value)
            continue

        if part.name in _REMOVED_KEYS:
            continue

        try:
            value = part.data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise TransformError(
                status_code=400,
                message=translate("Malformed multipart/form-data body."),
            ) from error

        output[part.name] = value

    prompt = output.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise TransformError(
            status_code=400,
            message=translate("'prompt' must be a non-empty string."),
        )

    model = output.get("model")
    if (
        model is None or (isinstance(model, str) and not model.strip())
    ) and settings is not None:
        model = settings.default_model
    if model is None:
        raise TransformError(
            status_code=400,
            message=translate(
                "'model' is required when no default model is configured."
            ),
        )
    output["model"] = model

    if "n" in output:
        try:
            n_int = int(output["n"])
            if isinstance(output["n"], bool) or not 1 <= n_int <= 10:
                raise ValueError
        except (ValueError, TypeError):
            raise TransformError(
                status_code=400,
                message=translate("'n' must be an integer between 1 and 10."),
            )
        output["n"] = n_int

    response_format = output.get("response_format")
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

    if not images:
        raise TransformError(
            status_code=400,
            message=translate("At least one input image is required."),
        )
    if len(images) == 1:
        output["image"] = {"url": images[0]}
    else:
        output["images"] = [{"url": uri} for uri in images]

    if settings is not None:
        if "aspect_ratio" not in output and settings.aspect_ratio is not None:
            output["aspect_ratio"] = settings.aspect_ratio
        if "resolution" not in output and settings.resolution is not None:
            output["resolution"] = settings.resolution

    return json.dumps(output).encode("utf-8")


async def handle_grok_image_edit_edits(
    config: ListenerRuntimeConfig,
    exchange: ExchangeLog | None,
    upstream_transport: httpx.AsyncBaseTransport | None,
    request: Request,
) -> Response:
    """Relay an images/edits request with Grok edit-specific transformation."""

    return await relay_upstream(
        config,
        exchange,
        upstream_transport,
        request,
        request.url.path,
        transform_request=lambda body, content_type: transform_request_body(
            body, content_type, config.listener.grok_image_edit
        ),
        transform_response=ensure_created,
        upstream_content_type="application/json",
        error_event="grok_image_edit_transform",
    )
