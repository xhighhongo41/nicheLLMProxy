"""Image generation request/response transformation for xAI Grok."""

from __future__ import annotations

import json
import logging
import time
from enum import Enum
from typing import Any

from niche_llm_proxy.config import GrokImageConfig
from niche_llm_proxy.i18n import translate

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
