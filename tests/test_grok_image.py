"""Tests for grok-image request/response transformation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from niche_llm_proxy.app import create_app
from niche_llm_proxy.config import (
    GrokImageConfig,
    ProxyConfig,
    load_config,
)
from niche_llm_proxy.grok_image import (
    RequestKind,
    TransformError,
    classify_request,
    transform_request_body,
    transform_response_body,
)
from niche_llm_proxy.i18n import translate


class _BytesStream(httpx.AsyncByteStream):
    """Provide a response body that remains unread until the proxy streams it."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        pass


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


def _grok_image_config(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    *,
    grok_image: dict[str, object] | None = None,
    logging_config: dict[str, object] | None = None,
) -> ProxyConfig:
    """Create a grok-image mode proxy configuration for end-to-end tests."""

    monkeypatch.setenv("XAI_API_KEY", "xai-upstream-secret")
    listener: dict[str, object] = {"port": 8000, "mode": "grok-image"}
    if grok_image is not None:
        listener["grok_image"] = grok_image
    if logging_config is not None:
        listener["features"] = [{"name": "logging", "config": logging_config}]
    return load_config(
        write_config(
            {
                "listener": listener,
                "upstream": {
                    "base_url": "https://upstream.example.test",
                    "api_key_env": "XAI_API_KEY",
                },
            }
        )
    )


_UPSTREAM_IMAGE_RESPONSE = (
    b'{"data":[{"b64_json":"aW1hZ2U=","mime_type":"image/jpeg"}],'
    b'"usage":{"cost_in_usd_ticks":400000000}}'
)


@pytest.mark.anyio
async def test_generations_transforms_request_and_adds_created(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Transform an OpenAI-style request and return an OpenAI-style response."""
    config = _grok_image_config(
        monkeypatch,
        write_config,
        grok_image={
            "default_model": "grok-imagine-image-2.0",
            "aspect_ratio": "1:1",
            "resolution": "1k",
        },
    )
    received: dict[str, object] = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        received["method"] = request.method
        received["path"] = request.url.path
        received["body"] = request.content
        received["authorization"] = request.headers["authorization"]
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "X-Upstream": "kept"},
            content=_UPSTREAM_IMAGE_RESPONSE,
            request=request,
        )

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            json={
                "model": "grok-imagine-image-2.0",
                "prompt": "a cat",
                "size": "1024x1024",
                "quality": "hd",
                "storage_options": {"ttl": 3600},
            },
        )

    assert response.status_code == 200
    assert received["method"] == "POST"
    assert received["path"] == "/v1/images/generations"
    assert received["authorization"] == "Bearer xai-upstream-secret"
    assert json.loads(received["body"]) == {
        "prompt": "a cat",
        "model": "grok-imagine-image-2.0",
        "response_format": "b64_json",
        "storage_options": {"ttl": 3600},
        "aspect_ratio": "1:1",
        "resolution": "1k",
    }
    payload = response.json()
    assert payload["data"] == [{"b64_json": "aW1hZ2U=", "mime_type": "image/jpeg"}]
    assert payload["usage"] == {"cost_in_usd_ticks": 400000000}
    assert isinstance(payload["created"], int)
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-upstream"] == "kept"


@pytest.mark.anyio
async def test_generations_preserves_existing_created(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Keep an upstream created timestamp unchanged."""
    config = _grok_image_config(monkeypatch, write_config)
    upstream_body = b'{"created":111,"data":[]}'

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=upstream_body,
            request=request,
        )

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/images/generations", json={"prompt": "a cat", "model": "grok-imagine-image-2.0"}
        )

    assert response.status_code == 200
    assert response.json() == {"created": 111, "data": []}


@pytest.mark.anyio
async def test_generations_rejects_invalid_request_before_upstream(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject an invalid request locally without contacting the upstream."""
    config = _grok_image_config(monkeypatch, write_config)
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
        response = await client.post("/v1/images/generations", json={"prompt": "   "})

    assert response.status_code == 400
    assert response.json() == {"detail": "'prompt' must be a non-empty string."}
    assert not contacted


@pytest.mark.anyio
async def test_unsupported_path_returns_404(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject a path outside the grok-image route table."""
    config = _grok_image_config(monkeypatch, write_config)
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
        "detail": "This path is not supported in 'grok-image' mode."
    }
    assert not contacted


@pytest.mark.anyio
async def test_unsupported_method_returns_405(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject a supported path reached with an unsupported method."""
    config = _grok_image_config(monkeypatch, write_config)
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
        response = await client.get("/v1/images/generations")

    assert response.status_code == 405
    assert response.json() == {
        "detail": "This path is not supported in 'grok-image' mode."
    }
    assert not contacted


@pytest.mark.anyio
async def test_model_listings_are_passed_through(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Relay model listing requests unchanged, including their query strings."""
    config = _grok_image_config(monkeypatch, write_config)
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
async def test_upstream_error_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Relay upstream errors without changing status or body."""
    config = _grok_image_config(monkeypatch, write_config)
    error_body = b'{"error":{"message":"size is not supported"}}'

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"Content-Type": "application/json"},
            content=error_body,
            request=request,
        )

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            json={
                "prompt": "a cat",
                "model": "grok-imagine-image-2.0",
                "size": "1024x1024",
            },
        )

    assert response.status_code == 400
    assert response.content == error_body
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.anyio
async def test_upstream_non_json_success_passes_through(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Return an unparseable upstream success body unchanged."""
    config = _grok_image_config(monkeypatch, write_config)
    upstream_body = b"not json"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=upstream_body,
            request=request,
        )

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/images/generations", json={"prompt": "a cat", "model": "grok-imagine-image-2.0"}
        )

    assert response.status_code == 200
    assert response.content == upstream_body
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.anyio
async def test_upstream_connect_error_returns_502(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Map an upstream connection failure to the proxy error contract."""
    config = _grok_image_config(monkeypatch, write_config)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/images/generations", json={"prompt": "a cat", "model": "grok-imagine-image-2.0"}
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Unable to connect to the upstream provider."}


@pytest.mark.anyio
async def test_health_does_not_contact_upstream_in_grok_image_mode(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Keep serving health checks without forwarding them in grok-image mode."""
    config = _grok_image_config(monkeypatch, write_config)
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
    assert response.json() == {"status": "ok"}
    assert not contacted


@pytest.mark.anyio
async def test_logging_correlates_transformed_exchange(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    tmp_path: Path,
) -> None:
    """Log the original request, the transformed upstream request, and the response."""
    log_path = tmp_path / "grok.jsonl"
    config = _grok_image_config(
        monkeypatch,
        write_config,
        grok_image={"default_model": "grok-imagine-image-2.0"},
        logging_config={
            "stdout": False,
            "file": {
                "enabled": True,
                "path": str(log_path),
                "max_bytes": 1_000_000,
                "backup_count": 2,
            },
            "capture": {"bodies": True, "max_body_bytes": 1_000},
        },
    )
    original_body = json.dumps(
        {
            "prompt": "a cat",
            "model": "grok-imagine-image-2.0",
            "api_key": "client-secret",
            "size": "1024x1024",
        }
    ).encode()
    response_body = b'{"data":[]}'

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=response_body,
            request=request,
        )

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            content=original_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer client-secret",
            },
        )

    app.state.logging_runtime.close()
    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]

    assert response.status_code == 200
    assert [record["event"] for record in records] == [
        "request_received",
        "upstream_request_sent",
        "upstream_response_started",
        "exchange_completed",
    ]
    assert len({record["request_id"] for record in records}) == 1
    serialized = json.dumps(records)
    assert "client-secret" not in serialized
    assert "xai-upstream-secret" not in serialized
    sent = records[1]
    transformed = json.dumps(
        {
            "prompt": "a cat",
            "model": "grok-imagine-image-2.0",
            "response_format": "b64_json",
            "api_key": "client-secret",
        }
    ).encode()
    assert sent["upstream_request_bytes"] == len(transformed)
    assert sent["upstream_request_sha256"] == hashlib.sha256(transformed).hexdigest()
    completed = records[-1]
    assert completed["request"]["body"] == (
        '{"prompt":"a cat","model":"grok-imagine-image-2.0",'
        '"api_key":"[REDACTED]","size":"1024x1024"}'
    )
    assert completed["response"]["body"] == response_body.decode()
