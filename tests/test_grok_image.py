"""Tests for grok-image request/response transformation."""

from __future__ import annotations

import json

import pytest

from niche_llm_proxy.config import GrokImageConfig
from niche_llm_proxy.grok_image import (
    RequestKind,
    TransformError,
    classify_request,
    transform_request_body,
    transform_response_body,
)
from niche_llm_proxy.i18n import translate


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
            ("/v1/images/generations", "POST", RequestKind.GENERATIONS),
            ("/v1/models", "GET", RequestKind.MODELS),
            ("/v1/image-generation-models", "GET", RequestKind.IMAGE_GENERATION_MODELS),
            ("/v1/unknown", "GET", RequestKind.UNSUPPORTED_PATH),
            ("/v1/images/generations", "GET", RequestKind.METHOD_NOT_ALLOWED),
            ("/v1/images/generations", "DELETE", RequestKind.METHOD_NOT_ALLOWED),
            ("/v1/models", "POST", RequestKind.METHOD_NOT_ALLOWED),
            ("/v1/models", "DELETE", RequestKind.METHOD_NOT_ALLOWED),
            ("/v1/image-generation-models", "POST", RequestKind.METHOD_NOT_ALLOWED),
        ],
    )
    def test_classify_request(
        self, path: str, method: str, expected: RequestKind
    ) -> None:
        """Classify every supported path/method combination."""
        assert classify_request(path, method) is expected

    def test_classify_request_ignores_query_string(self) -> None:
        """Strip a query string before matching the path."""
        assert classify_request("/v1/models?filters=image", "GET") is RequestKind.MODELS

    def test_classify_request_is_case_insensitive_for_method(self) -> None:
        """Normalize the HTTP method to upper case."""
        assert classify_request("/v1/models", "get") is RequestKind.MODELS


class TestTransformRequestBody:
    """Request body transformation for POST /v1/images/generations."""

    def test_removes_openai_parameters(self) -> None:
        """Drop unsupported OpenAI-style generation parameters."""
        body = _body(
            {
                "prompt": "a cat",
                "model": "grok-imagine-image-2.0",
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
            "model": "grok-imagine-image-2.0",
            "response_format": "b64_json",
        }

    def test_uses_default_model_from_settings(self) -> None:
        """Fill in the model from settings when the request omits it."""
        settings = GrokImageConfig(default_model="grok-imagine-image-2.0")
        body = _body({"prompt": "a cat"})

        result = transform_request_body(body, settings)

        assert _loads(result) == {
            "prompt": "a cat",
            "model": "grok-imagine-image-2.0",
            "response_format": "b64_json",
        }

    def test_defaults_response_format_to_b64_json(self) -> None:
        """Add response_format=b64_json when the request omits it."""
        body = _body({"prompt": "a cat", "model": "grok-imagine-image-2.0"})

        result = transform_request_body(body, None)

        assert _loads(result)["response_format"] == "b64_json"

    def test_uses_aspect_ratio_default_from_settings(self) -> None:
        """Fill in aspect_ratio from settings when the request omits it."""
        settings = GrokImageConfig(aspect_ratio="1:1")
        body = _body({"prompt": "a cat", "model": "grok-imagine-image-2.0"})

        result = transform_request_body(body, settings)

        assert _loads(result)["aspect_ratio"] == "1:1"

    def test_uses_resolution_default_from_settings(self) -> None:
        """Fill in resolution from settings when the request omits it."""
        settings = GrokImageConfig(resolution="1k")
        body = _body({"prompt": "a cat", "model": "grok-imagine-image-2.0"})

        result = transform_request_body(body, settings)

        assert _loads(result)["resolution"] == "1k"

    def test_request_values_take_priority_over_settings(self) -> None:
        """Use explicit request values over configured defaults."""
        settings = GrokImageConfig(
            default_model="grok-default",
            aspect_ratio="1:1",
            resolution="1k",
        )
        body = _body(
            {
                "prompt": "a cat",
                "model": "custom-model",
                "aspect_ratio": "16:9",
                "resolution": "2k",
            }
        )

        result = transform_request_body(body, settings)

        assert _loads(result) == {
            "prompt": "a cat",
            "model": "custom-model",
            "response_format": "b64_json",
            "aspect_ratio": "16:9",
            "resolution": "2k",
        }

    def test_unknown_keys_pass_through(self) -> None:
        """Forward unknown parameters unchanged."""
        body = _body(
            {
                "prompt": "a cat",
                "model": "grok-imagine-image-2.0",
                "storage_options": {"ttl": 3600},
            }
        )

        result = transform_request_body(body, None)

        assert _loads(result) == {
            "prompt": "a cat",
            "model": "grok-imagine-image-2.0",
            "response_format": "b64_json",
            "storage_options": {"ttl": 3600},
        }

    def test_settings_none_does_not_add_model_aspect_ratio_resolution(self) -> None:
        """With no settings, only add the mandatory response_format default."""
        body = _body({"prompt": "a cat", "model": "grok-imagine-image-2.0"})

        result = transform_request_body(body, None)

        parsed = _loads(result)
        assert parsed == {
            "prompt": "a cat",
            "model": "grok-imagine-image-2.0",
            "response_format": "b64_json",
        }

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
        body = _body({"prompt": prompt, "model": "grok-imagine-image-2.0"})

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
        ],
    )
    def test_invalid_n_raises_transform_error(self, n: object) -> None:
        """Reject an n value outside 1-10 or of the wrong type."""
        body = _body(
            {"prompt": "a cat", "model": "grok-imagine-image-2.0", "n": n}
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "'n' must be an integer between 1 and 10."
        )

    @pytest.mark.parametrize(
        "response_format",
        [
            pytest.param("png", id="unsupported"),
            pytest.param(["url"], id="non-string"),
        ],
    )
    def test_invalid_response_format_raises_transform_error(
        self, response_format: object
    ) -> None:
        """Reject an unsupported or non-string response_format value."""
        body = _body(
            {
                "prompt": "a cat",
                "model": "grok-imagine-image-2.0",
                "response_format": response_format,
            }
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "'response_format' must be 'url' or 'b64_json'."
        )

    def test_null_response_format_defaults_to_b64_json(self) -> None:
        """Treat an explicit null response_format as unset."""
        body = _body(
            {
                "prompt": "a cat",
                "model": "grok-imagine-image-2.0",
                "response_format": None,
            }
        )

        result = transform_request_body(body, None)

        assert _loads(result)["response_format"] == "b64_json"

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b"not json", id="invalid-json"),
            pytest.param(b'[1, 2, 3]', id="not-an-object"),
        ],
    )
    def test_invalid_body_raises_transform_error(self, body: bytes) -> None:
        """Reject a non-JSON or non-object request body."""
        with pytest.raises(TransformError) as error:
            transform_request_body(body, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "Request body must be a JSON object in 'grok-image' mode."
        )


class TestTransformResponseBody:
    """Response body transformation."""

    def test_adds_created_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Inject a unix epoch timestamp when the response lacks created."""
        monkeypatch.setattr("time.time", lambda: 1234567890.0)
        body = _body({"data": []})

        result = transform_response_body(body)

        assert _loads(result) == {"created": 1234567890, "data": []}

    def test_preserves_existing_created(self) -> None:
        """Keep an existing created value unchanged."""
        body = _body({"created": 111, "data": []})

        result = transform_response_body(body)

        assert _loads(result) == {"created": 111, "data": []}

    def test_preserves_other_fields(self) -> None:
        """Leave data, usage, and other fields untouched."""
        body = _body(
            {
                "data": [{"b64_json": "...", "revised_prompt": "cat"}],
                "usage": {"prompt_tokens": 10},
            }
        )

        result = transform_response_body(body)
        parsed = _loads(result)

        assert parsed["data"] == [{"b64_json": "...", "revised_prompt": "cat"}]
        assert parsed["usage"] == {"prompt_tokens": 10}
        assert "created" in parsed

    def test_passes_through_non_json_unchanged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Return a non-JSON body untouched and log a warning."""
        body = b"not json"

        result = transform_response_body(body)

        assert result == body
        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == "WARNING"

    def test_passes_through_non_object_json_unchanged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Return a top-level non-object JSON body untouched and log a warning."""
        body = b'"string response"'

        result = transform_response_body(body)

        assert result == body
        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == "WARNING"
