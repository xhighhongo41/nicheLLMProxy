"""Tests for grok-image-edit request/response transformation."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from niche_llm_proxy.app import PROXY_VERSION, create_app
from niche_llm_proxy.config import (
    GrokImageEditConfig,
    ListenerRuntimeConfig,
    load_config,
)
from niche_llm_proxy.grok_image_edit import (
    RequestKind,
    TransformError,
    classify_request,
    transform_request_body,
    transform_response_body,
)
from niche_llm_proxy.i18n import translate
from niche_llm_proxy.multipart_forms import FormPart, build_multipart


class _BytesStream(httpx.AsyncByteStream):
    """Provide a response body that remains unread until the proxy streams it."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        pass


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"image-png"
_JPEG_BYTES = b"\xff\xd8\xff" + b"image-jpeg"
_WEBP_BYTES = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"image-webp"


def _data_uri(mime: str, data: bytes) -> str:
    """Build a data URI for image data."""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _build_body(*parts: FormPart) -> tuple[bytes, str]:
    """Build a multipart body and its Content-Type header value."""
    return build_multipart(list(parts))


def _txt(name: str, value: str) -> FormPart:
    """Build a text multipart part."""
    return FormPart(name=name, data=value.encode("utf-8"), filename=None, content_type=None)


def _file(name: str, filename: str, data: bytes, content_type: str | None = None) -> FormPart:
    """Build a file multipart part."""
    return FormPart(name=name, data=data, filename=filename, content_type=content_type)


class TestClassifyRequest:
    """Route and method classification."""

    @pytest.mark.parametrize(
        ("path", "method", "expected"),
        [
            ("/v1/images/edits", "POST", RequestKind.EDITS),
            ("/v1/models", "GET", RequestKind.MODELS),
            ("/v1/image-generation-models", "GET", RequestKind.IMAGE_GENERATION_MODELS),
            ("/v1/unknown", "GET", RequestKind.UNSUPPORTED_PATH),
            ("/v1/images/edits", "GET", RequestKind.METHOD_NOT_ALLOWED),
            ("/v1/images/edits", "DELETE", RequestKind.METHOD_NOT_ALLOWED),
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
    """Request body transformation for POST /v1/images/edits."""

    def test_happy_path_single_image_file(self) -> None:
        """Transform a minimal multipart edit into xAI JSON."""
        body, content_type = _build_body(
            _txt("prompt", "turn the cat blue"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "cat.png", _PNG_BYTES, "image/png"),
        )

        result = transform_request_body(body, content_type, None)

        assert json.loads(result) == {
            "prompt": "turn the cat blue",
            "model": "grok-image-edit-beta",
            "response_format": "b64_json",
            "image": {"url": _data_uri("image/png", _PNG_BYTES)},
        }

    def test_multiple_image_files(self) -> None:
        """Collect multiple image[] file parts into the images array."""
        body, content_type = _build_body(
            _txt("prompt", "mashup"),
            _txt("model", "grok-image-edit-beta"),
            _file("image[]", "a.png", _PNG_BYTES),
            _file("image[]", "b.jpeg", _JPEG_BYTES),
            _file("image[]", "c.webp", _WEBP_BYTES),
        )

        result = transform_request_body(body, content_type, None)

        assert json.loads(result) == {
            "prompt": "mashup",
            "model": "grok-image-edit-beta",
            "response_format": "b64_json",
            "images": [
                {"url": _data_uri("image/png", _PNG_BYTES)},
                {"url": _data_uri("image/jpeg", _JPEG_BYTES)},
                {"url": _data_uri("image/webp", _WEBP_BYTES)},
            ],
        }

    def test_mixed_file_and_url_text_inputs(self) -> None:
        """Allow a file and a text URL in the same request."""
        body, content_type = _build_body(
            _txt("prompt", "combine"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "local.png", _PNG_BYTES),
            _txt("image", "https://example.test/remote.jpg"),
        )

        result = transform_request_body(body, content_type, None)

        assert json.loads(result) == {
            "prompt": "combine",
            "model": "grok-image-edit-beta",
            "response_format": "b64_json",
            "images": [
                {"url": _data_uri("image/png", _PNG_BYTES)},
                {"url": "https://example.test/remote.jpg"},
            ],
        }

    def test_model_empty_string_filled_from_settings(self) -> None:
        """Treat an empty model string as absent and use the configured default."""
        settings = GrokImageEditConfig(default_model="grok-image-edit-beta")
        body, content_type = _build_body(
            _txt("prompt", "change color"),
            _txt("model", ""),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, settings)

        assert json.loads(result)["model"] == "grok-image-edit-beta"

    def test_model_missing_without_default_raises_transform_error(self) -> None:
        """Require a model when no default is configured."""
        body, content_type = _build_body(
            _txt("prompt", "change color"),
            _file("image", "x.png", _PNG_BYTES),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "'model' is required when no default model is configured."
        )

    def test_prompt_missing_raises_transform_error(self) -> None:
        """Require a non-empty prompt."""
        body, content_type = _build_body(
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.png", _PNG_BYTES),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate("'prompt' must be a non-empty string.")

    def test_prompt_whitespace_only_raises_transform_error(self) -> None:
        """Reject a whitespace-only prompt."""
        body, content_type = _build_body(
            _txt("prompt", "   "),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.png", _PNG_BYTES),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate("'prompt' must be a non-empty string.")

    def test_repeated_prompt_last_wins(self) -> None:
        """Use the last prompt value when the field is repeated."""
        body, content_type = _build_body(
            _txt("prompt", "first"),
            _txt("prompt", "second"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, None)

        assert json.loads(result)["prompt"] == "second"

    @pytest.mark.parametrize(
        "n",
        [
            pytest.param("0", id="zero"),
            pytest.param("11", id="too-large"),
            pytest.param("not-an-int", id="non-integer"),
            pytest.param("1.5", id="float-string"),
        ],
    )
    def test_invalid_n_raises_transform_error(self, n: str) -> None:
        """Reject n values outside 1-10 or not parseable as integers."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _txt("n", n),
            _file("image", "x.png", _PNG_BYTES),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate("'n' must be an integer between 1 and 10.")

    def test_valid_n_passes_through(self) -> None:
        """Forward a valid n value parsed as an integer."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _txt("n", "3"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, None)

        assert json.loads(result)["n"] == 3

    def test_absent_n_is_omitted(self) -> None:
        """Do not include an n key when the field is absent."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, None)

        assert "n" not in json.loads(result)

    def test_response_format_absent_defaults_to_b64_json(self) -> None:
        """Add response_format=b64_json when the request omits it."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, None)

        assert json.loads(result)["response_format"] == "b64_json"

    def test_response_format_url_passes_through(self) -> None:
        """Forward an explicit response_format=url."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _txt("response_format", "url"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, None)

        assert json.loads(result)["response_format"] == "url"

    def test_invalid_response_format_raises_transform_error(self) -> None:
        """Reject response_format values other than url or b64_json."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _txt("response_format", "png"),
            _file("image", "x.png", _PNG_BYTES),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "'response_format' must be 'url' or 'b64_json'."
        )

    def test_removed_keys_dropped(self) -> None:
        """Silently drop unsupported OpenAI-style generation parameters."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _txt("size", "1024x1024"),
            _txt("quality", "hd"),
            _txt("style", "natural"),
            _txt("seed", "123"),
            _txt("background", "transparent"),
            _txt("moderation", "low"),
            _txt("output_format", "png"),
            _txt("output_compression", "low"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, None)

        parsed = json.loads(result)
        for key in (
            "size",
            "quality",
            "style",
            "seed",
            "background",
            "moderation",
            "output_format",
            "output_compression",
        ):
            assert key not in parsed

    def test_aspect_ratio_request_overrides_settings(self) -> None:
        """Use the request aspect_ratio over the configured default."""
        settings = GrokImageEditConfig(aspect_ratio="1:1")
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _txt("aspect_ratio", "16:9"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, settings)

        assert json.loads(result)["aspect_ratio"] == "16:9"

    def test_aspect_ratio_filled_from_settings(self) -> None:
        """Fill in aspect_ratio from settings when the request omits it."""
        settings = GrokImageEditConfig(aspect_ratio="1:1")
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, settings)

        assert json.loads(result)["aspect_ratio"] == "1:1"

    def test_resolution_request_overrides_settings(self) -> None:
        """Use the request resolution over the configured default."""
        settings = GrokImageEditConfig(resolution="1k")
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _txt("resolution", "2k"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, settings)

        assert json.loads(result)["resolution"] == "2k"

    def test_resolution_filled_from_settings(self) -> None:
        """Fill in resolution from settings when the request omits it."""
        settings = GrokImageEditConfig(resolution="1k")
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, settings)

        assert json.loads(result)["resolution"] == "1k"

    def test_unknown_text_key_passthrough(self) -> None:
        """Forward unknown text fields as top-level string fields."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _txt("custom", "value"),
            _file("image", "x.png", _PNG_BYTES),
        )

        result = transform_request_body(body, content_type, None)

        assert json.loads(result)["custom"] == "value"

    def test_mask_file_part_rejected(self) -> None:
        """Reject any mask file part."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.png", _PNG_BYTES),
            _file("mask", "mask.png", _PNG_BYTES),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "mask is not supported in 'grok-image-edit' mode."
        )

    def test_mask_text_part_rejected(self) -> None:
        """Reject any mask text part."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.png", _PNG_BYTES),
            _txt("mask", "data:image/png;base64,abc"),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "mask is not supported in 'grok-image-edit' mode."
        )

    def test_unsupported_image_type_rejected(self) -> None:
        """Reject image bytes that are not PNG, JPEG or WebP."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.gif", b"GIF89a"),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "Unsupported image type. Only PNG, JPEG and WebP are accepted."
        )

    def test_text_image_invalid_value_rejected(self) -> None:
        """Reject text image values that are not http(s)/data URLs."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _txt("image", "/local/path.jpg"),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "'image' must be an http(s) or data URL when sent as a text field."
        )

    def test_data_url_text_image_passes_through(self) -> None:
        """Accept a data URL in a text image part."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _txt("image", "data:image/png;base64,abc"),
        )

        result = transform_request_body(body, content_type, None)

        assert json.loads(result)["image"] == {
            "url": "data:image/png;base64,abc"
        }

    def test_no_image_raises_transform_error(self) -> None:
        """Require at least one input image."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "At least one input image is required."
        )

    def test_unknown_file_part_rejected(self) -> None:
        """Reject an unexpected file part."""
        body, content_type = _build_body(
            _txt("prompt", "edit"),
            _txt("model", "grok-image-edit-beta"),
            _file("image", "x.png", _PNG_BYTES),
            _file("unknown", "x.txt", b"x"),
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, content_type, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "Unexpected file part '{name}' was rejected.", name="unknown"
        )

    def test_non_multipart_content_type_rejected(self) -> None:
        """Reject bodies that are not multipart/form-data."""
        body = b'{"prompt":"edit"}'

        with pytest.raises(TransformError) as error:
            transform_request_body(body, "application/json", None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "Request body must be multipart/form-data in 'grok-image-edit' mode."
        )

    def test_missing_content_type_rejected(self) -> None:
        """Reject bodies with no Content-Type header."""
        body, _ = _build_body(_txt("prompt", "edit"))

        with pytest.raises(TransformError) as error:
            transform_request_body(body, None, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "Request body must be multipart/form-data in 'grok-image-edit' mode."
        )

    def test_malformed_multipart_rejected(self) -> None:
        """Reject a body that fails multipart parsing."""
        with pytest.raises(TransformError) as error:
            transform_request_body(b"not multipart", "multipart/form-data; boundary=x", None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "Malformed multipart/form-data body."
        )


class TestTransformResponseBody:
    """Response body transformation."""

    def test_adds_created_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Inject a unix epoch timestamp when the response lacks created."""
        monkeypatch.setattr("time.time", lambda: 1234567890.0)
        body = json.dumps({"data": []}).encode("utf-8")

        result = transform_response_body(body)

        assert json.loads(result) == {"created": 1234567890, "data": []}

    def test_preserves_existing_created(self) -> None:
        """Keep an existing created value unchanged."""
        body = json.dumps({"created": 111, "data": []}).encode("utf-8")

        result = transform_response_body(body)

        assert json.loads(result) == {"created": 111, "data": []}


def _grok_image_edit_config(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    *,
    grok_image_edit: dict[str, object] | None = None,
    logging_config: dict[str, object] | None = None,
) -> ListenerRuntimeConfig:
    """Create a grok-image-edit mode proxy configuration for end-to-end tests."""

    monkeypatch.setenv("XAI_API_KEY", "xai-upstream-secret")
    listener: dict[str, object] = {
        "port": 8000,
        "mode": "grok-image-edit",
        "grok_image_edit": grok_image_edit if grok_image_edit is not None else {},
        "upstream": {
            "base_url": "https://upstream.example.test",
            "api_key_env": "XAI_API_KEY",
        },
    }
    if logging_config is not None:
        listener["features"] = [{"name": "logging", "config": logging_config}]
    return load_config(write_config({"listeners": [listener]})).listeners[0]


_UPSTREAM_IMAGE_RESPONSE = (
    b'{"data":[{"b64_json":"aW1hZ2U=","mime_type":"image/jpeg"}],'
    b'"usage":{"cost_in_usd_ticks":400000000}}'
)


@pytest.mark.anyio
async def test_edits_transforms_request_and_adds_created(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Transform an OpenAI-style edits request and return a complete response."""
    config = _grok_image_edit_config(
        monkeypatch,
        write_config,
        grok_image_edit={
            "default_model": "grok-image-edit-beta",
            "aspect_ratio": "1:1",
            "resolution": "1k",
        },
    )
    received: dict[str, object] = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        received["method"] = request.method
        received["path"] = request.url.path
        received["body"] = request.content
        received["content_type"] = request.headers["content-type"]
        received["authorization"] = request.headers["authorization"]
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "X-Upstream": "kept"},
            content=_UPSTREAM_IMAGE_RESPONSE,
            request=request,
        )

    body, content_type = _build_body(
        _txt("prompt", "turn the cat blue"),
        _file("image", "cat.png", _PNG_BYTES, "image/png"),
    )

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/images/edits",
            content=body,
            headers={"Content-Type": content_type},
        )

    assert response.status_code == 200
    assert received["method"] == "POST"
    assert received["path"] == "/v1/images/edits"
    assert received["content_type"] == "application/json"
    assert received["authorization"] == "Bearer xai-upstream-secret"
    assert json.loads(received["body"]) == {
        "prompt": "turn the cat blue",
        "model": "grok-image-edit-beta",
        "response_format": "b64_json",
        "aspect_ratio": "1:1",
        "resolution": "1k",
        "image": {"url": _data_uri("image/png", _PNG_BYTES)},
    }
    payload = response.json()
    assert payload["data"] == [{"b64_json": "aW1hZ2U=", "mime_type": "image/jpeg"}]
    assert payload["usage"] == {"cost_in_usd_ticks": 400000000}
    assert isinstance(payload["created"], int)
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-upstream"] == "kept"


@pytest.mark.anyio
async def test_edits_rejects_invalid_request_before_upstream(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject an invalid multipart request locally without contacting the upstream."""
    config = _grok_image_edit_config(monkeypatch, write_config)
    contacted = False

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(200, request=request)

    body, content_type = _build_body(
        _txt("prompt", "   "),
        _file("image", "x.png", _PNG_BYTES),
    )

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/images/edits",
            content=body,
            headers={"Content-Type": content_type},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "'prompt' must be a non-empty string."}
    assert not contacted


@pytest.mark.anyio
async def test_unsupported_path_returns_404(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject a path outside the grok-image-edit route table."""
    config = _grok_image_edit_config(monkeypatch, write_config)
    contacted = False

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(200, request=request)

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post("/v1/chat/completions", json={"input": "hi"})

    assert response.status_code == 404
    assert response.json() == {
        "detail": "This path is not supported in 'grok-image-edit' mode."
    }
    assert not contacted


@pytest.mark.anyio
async def test_unsupported_method_returns_405(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject a supported path reached with an unsupported method."""
    config = _grok_image_edit_config(monkeypatch, write_config)
    contacted = False

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(200, request=request)

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.get("/v1/images/edits")

    assert response.status_code == 405
    assert response.json() == {
        "detail": "This path is not supported in 'grok-image-edit' mode."
    }
    assert not contacted


@pytest.mark.anyio
async def test_model_listings_are_passed_through(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Relay model listing requests unchanged, including their query strings."""
    config = _grok_image_edit_config(monkeypatch, write_config)
    upstream_body = b'{"object":"list","data":[{"id":"grok-imagine-image-2.0"}]}'
    received: dict[str, object] = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        received[request.url.path] = request.url.query
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_BytesStream(upstream_body),
            request=request,
        )

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        first = await client.get("/v1/models?list=images")
        second = await client.get("/v1/image-generation-models")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == upstream_body
    assert second.content == upstream_body
    assert received == {
        "/v1/models": b"list=images",
        "/v1/image-generation-models": b"",
    }
    assert first.headers["content-type"].startswith("application/json")


@pytest.mark.anyio
async def test_health_does_not_contact_upstream_in_grok_image_edit_mode(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Keep serving health checks without forwarding them."""
    config = _grok_image_edit_config(monkeypatch, write_config)
    contacted = False

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(200, request=request)

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": PROXY_VERSION,
        "mode": "grok-image-edit",
        "features": [],
    }
    assert not contacted


@pytest.mark.anyio
async def test_japanese_error_message_for_unsupported_path(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Return a localized rejection message when Japanese is configured."""
    monkeypatch.setenv("NICHELLM_LANGUAGE", "ja")
    config = _grok_image_edit_config(monkeypatch, write_config)

    app = create_app(config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post("/v1/chat/completions", json={"input": "hi"})

    assert response.status_code == 404
    assert response.json() == {
        "detail": "この経路は 'grok-image-edit' モードではサポートされていません。"
    }
