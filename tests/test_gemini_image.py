"""Tests for gemini-image request/response transformation."""

from __future__ import annotations

import json

import pytest

from niche_llm_proxy.config import GeminiImageConfig
from niche_llm_proxy.gemini_image import (
    GeminiRequestKind,
    classify_request,
    transform_request_body,
)
from niche_llm_proxy.i18n import translate
from niche_llm_proxy.image_relay import TransformError


def _body(obj: object) -> bytes:
    """Serialize a value to UTF-8 JSON bytes."""
    return json.dumps(obj).encode("utf-8")


def _loads(body: bytes) -> object:
    """Parse UTF-8 JSON bytes."""
    return json.loads(body.decode("utf-8"))


class TestClassifyRequest:
    """Route and method classification."""

    @pytest.mark.parametrize(
        ("path", "method", "expected"),
        [
            ("/v1/images/generations", "POST", GeminiRequestKind.GENERATIONS),
            ("/v1/models", "GET", GeminiRequestKind.MODELS),
            ("/v1/unknown", "GET", GeminiRequestKind.UNSUPPORTED_PATH),
            ("/v1/image-generation-models", "GET", GeminiRequestKind.UNSUPPORTED_PATH),
            ("/v1/images/generations", "GET", GeminiRequestKind.METHOD_NOT_ALLOWED),
            ("/v1/images/generations", "DELETE", GeminiRequestKind.METHOD_NOT_ALLOWED),
            ("/v1/models", "POST", GeminiRequestKind.METHOD_NOT_ALLOWED),
            ("/v1/models", "DELETE", GeminiRequestKind.METHOD_NOT_ALLOWED),
        ],
    )
    def test_classify_request(
        self, path: str, method: str, expected: GeminiRequestKind
    ) -> None:
        """Classify every supported path/method combination."""
        assert classify_request(path, method) is expected

    def test_classify_request_ignores_query_string(self) -> None:
        """Strip a query string before matching the path."""
        assert classify_request("/v1/models?filters=image", "GET") is GeminiRequestKind.MODELS

    def test_classify_request_is_case_insensitive_for_method(self) -> None:
        """Normalize the HTTP method to upper case."""
        assert classify_request("/v1/models", "get") is GeminiRequestKind.MODELS


class TestTransformRequestBody:
    """Request body transformation for POST /v1/images/generations."""

    def test_passes_through_openai_parameters(self) -> None:
        """Forward every OpenAI-style parameter unchanged."""
        body = _body(
            {
                "prompt": "a cat",
                "model": "gemini-3-pro-image-preview",
                "size": "1024x1024",
                "quality": "hd",
                "style": "natural",
                "seed": 123,
                "background": "transparent",
                "moderation": "low",
                "output_format": "png",
                "output_compression": "low",
            }
        )

        result = transform_request_body(body, None)

        assert _loads(result) == {
            "prompt": "a cat",
            "model": "gemini-3-pro-image-preview",
            "response_format": "b64_json",
            "size": "1024x1024",
            "quality": "hd",
            "style": "natural",
            "seed": 123,
            "background": "transparent",
            "moderation": "low",
            "output_format": "png",
            "output_compression": "low",
        }

    def test_uses_default_model_from_settings(self) -> None:
        """Fill in the model from settings when the request omits it."""
        settings = GeminiImageConfig(default_model="gemini-3-pro-image-preview")
        body = _body({"prompt": "a cat"})

        result = transform_request_body(body, settings)

        assert _loads(result) == {
            "prompt": "a cat",
            "model": "gemini-3-pro-image-preview",
            "response_format": "b64_json",
        }

    def test_defaults_response_format_to_b64_json(self) -> None:
        """Add response_format=b64_json when the request omits it."""
        body = _body({"prompt": "a cat", "model": "gemini-3-pro-image-preview"})

        result = transform_request_body(body, None)

        assert _loads(result)["response_format"] == "b64_json"

    def test_explicit_b64_json_response_format_passes_through(self) -> None:
        """Forward an explicit response_format=b64_json unchanged."""
        body = _body(
            {
                "prompt": "a cat",
                "model": "gemini-3-pro-image-preview",
                "response_format": "b64_json",
            }
        )

        result = transform_request_body(body, None)

        assert _loads(result) == _loads(body)

    def test_null_response_format_defaults_to_b64_json(self) -> None:
        """Treat an explicit null response_format as unset."""
        body = _body(
            {
                "prompt": "a cat",
                "model": "gemini-3-pro-image-preview",
                "response_format": None,
            }
        )

        result = transform_request_body(body, None)

        assert _loads(result)["response_format"] == "b64_json"

    def test_uses_aspect_ratio_default_when_size_and_aspect_ratio_absent(self) -> None:
        """Fill in aspect_ratio from settings only without size or aspect_ratio."""
        settings = GeminiImageConfig(aspect_ratio="1:1")
        body = _body({"prompt": "a cat", "model": "gemini-3-pro-image-preview"})

        result = transform_request_body(body, settings)

        assert _loads(result)["aspect_ratio"] == "1:1"

    def test_request_size_takes_priority_over_aspect_ratio_setting(self) -> None:
        """Keep the request size and skip the aspect_ratio default."""
        settings = GeminiImageConfig(aspect_ratio="1:1")
        body = _body(
            {
                "prompt": "a cat",
                "model": "gemini-3-pro-image-preview",
                "size": "1536x1024",
            }
        )

        result = transform_request_body(body, settings)

        parsed = _loads(result)
        assert isinstance(parsed, dict)
        assert parsed["size"] == "1536x1024"
        assert "aspect_ratio" not in parsed

    def test_request_aspect_ratio_takes_priority_over_setting(self) -> None:
        """Keep an explicit aspect_ratio and skip the configured default."""
        settings = GeminiImageConfig(aspect_ratio="1:1")
        body = _body(
            {
                "prompt": "a cat",
                "model": "gemini-3-pro-image-preview",
                "aspect_ratio": "16:9",
            }
        )

        result = transform_request_body(body, settings)

        assert _loads(result)["aspect_ratio"] == "16:9"

    def test_settings_none_does_not_add_model_or_aspect_ratio(self) -> None:
        """With no settings, only add the mandatory response_format default."""
        body = _body({"prompt": "a cat", "model": "gemini-3-pro-image-preview"})

        result = transform_request_body(body, None)

        assert _loads(result) == {
            "prompt": "a cat",
            "model": "gemini-3-pro-image-preview",
            "response_format": "b64_json",
        }

    def test_gemini_dialect_keys_pass_through(self) -> None:
        """Forward Gemini-specific dialect parameters unchanged."""
        body = _body(
            {
                "prompt": "a cat",
                "model": "gemini-3-pro-image-preview",
                "aspect_ratio": "21:9",
                "generation_config": {"temperature": 0.7},
                "safety_settings": {},
                "tools": [],
            }
        )

        result = transform_request_body(body, None)

        parsed = _loads(result)
        assert isinstance(parsed, dict)
        assert parsed["aspect_ratio"] == "21:9"
        assert parsed["generation_config"] == {"temperature": 0.7}
        assert parsed["safety_settings"] == {}
        assert parsed["tools"] == []

    def test_model_missing_without_default_raises_transform_error(self) -> None:
        """Require a model when no default is configured."""
        body = _body({"prompt": "a cat"})

        with pytest.raises(TransformError) as error:
            transform_request_body(body, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "'model' is required when no default model is configured."
        )

    @pytest.mark.parametrize(
        "prompt",
        [
            pytest.param(None, id="missing"),
            pytest.param(123, id="non-string"),
            pytest.param("   ", id="whitespace-only"),
        ],
    )
    def test_invalid_prompt_raises_transform_error(self, prompt: object) -> None:
        """Reject a missing, non-string, or empty prompt."""
        body = _body({"prompt": prompt, "model": "gemini-3-pro-image-preview"})

        with pytest.raises(TransformError) as error:
            transform_request_body(body, None)

        assert error.value.status_code == 400
        assert error.value.message == translate("'prompt' must be a non-empty string.")

    @pytest.mark.parametrize(
        "n",
        [
            pytest.param(0, id="zero"),
            pytest.param(11, id="too-large"),
            pytest.param(True, id="bool"),
            pytest.param("2", id="string"),
        ],
    )
    def test_invalid_n_raises_transform_error(self, n: object) -> None:
        """Reject an n value outside 1-10 or of the wrong type."""
        body = _body(
            {"prompt": "a cat", "model": "gemini-3-pro-image-preview", "n": n}
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "'n' must be an integer between 1 and 10."
        )

    def test_valid_n_passes_through(self) -> None:
        """Forward a valid n value unchanged."""
        body = _body(
            {"prompt": "a cat", "model": "gemini-3-pro-image-preview", "n": 4}
        )

        result = transform_request_body(body, None)

        assert _loads(result)["n"] == 4

    @pytest.mark.parametrize(
        "response_format",
        [
            pytest.param("url", id="url"),
            pytest.param("png", id="unsupported"),
            pytest.param(["b64_json"], id="non-string"),
        ],
    )
    def test_invalid_response_format_raises_transform_error(
        self, response_format: object
    ) -> None:
        """Reject any response_format other than b64_json."""
        body = _body(
            {
                "prompt": "a cat",
                "model": "gemini-3-pro-image-preview",
                "response_format": response_format,
            }
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "'response_format' must be 'b64_json' in 'gemini-image' mode."
        )

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b"not json", id="invalid-json"),
            pytest.param(b"[1, 2, 3]", id="not-an-object"),
        ],
    )
    def test_invalid_body_raises_transform_error(self, body: bytes) -> None:
        """Reject a non-JSON or non-object request body."""
        with pytest.raises(TransformError) as error:
            transform_request_body(body, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "Request body must be a JSON object in 'gemini-image' mode."
        )