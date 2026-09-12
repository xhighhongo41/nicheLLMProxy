"""Tests for gemini-image request/response transformation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from niche_llm_proxy.app import create_app
from niche_llm_proxy.config import (
    GeminiImageConfig,
    ProxyConfig,
    load_config,
)
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
            pytest.param(-1, id="negative"),
            pytest.param(True, id="bool"),
            pytest.param("1", id="string"),
        ],
    )
    def test_invalid_n_raises_transform_error(self, n: object) -> None:
        """Reject an n value that is not the integer 1."""
        body = _body(
            {"prompt": "a cat", "model": "gemini-3-pro-image-preview", "n": n}
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, None)

        assert error.value.status_code == 400
        assert error.value.message == translate("'n' must be an integer equal to 1.")

    @pytest.mark.parametrize("n", [2, 4, 11])
    def test_n_greater_than_one_raises_transform_error(self, n: int) -> None:
        """Reject n values greater than 1 with an explicit unsupported error."""
        body = _body(
            {"prompt": "a cat", "model": "gemini-3-pro-image-preview", "n": n}
        )

        with pytest.raises(TransformError) as error:
            transform_request_body(body, None)

        assert error.value.status_code == 400
        assert error.value.message == translate(
            "'n' greater than 1 is not supported in 'gemini-image' mode."
        )

    def test_n_of_one_passes_through(self) -> None:
        """Forward an explicit n of 1 unchanged."""
        body = _body(
            {"prompt": "a cat", "model": "gemini-3-pro-image-preview", "n": 1}
        )

        result = transform_request_body(body, None)

        assert _loads(result)["n"] == 1

    def test_omitted_n_is_not_sent(self) -> None:
        """Do not add an n field when the client omits it."""
        body = _body({"prompt": "a cat", "model": "gemini-3-pro-image-preview"})

        result = transform_request_body(body, None)

        assert "n" not in _loads(result)

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


class _BytesStream(httpx.AsyncByteStream):
    """Provide a response body that remains unread until the proxy streams it."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        pass


def _gemini_image_config(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    *,
    gemini_image: dict[str, object] | None = None,
    logging_config: dict[str, object] | None = None,
) -> ProxyConfig:
    """Create a gemini-image mode proxy configuration for end-to-end tests."""

    monkeypatch.setenv("GEMINI_API_KEY", "gemini-upstream-secret")
    listener: dict[str, object] = {"port": 8000, "mode": "gemini-image"}
    if gemini_image is not None:
        listener["gemini_image"] = gemini_image
    if logging_config is not None:
        listener["features"] = [{"name": "logging", "config": logging_config}]
    return load_config(
        write_config(
            {
                "listener": listener,
                "upstream": {
                    "base_url": "https://upstream.example.test",
                    "api_key_env": "GEMINI_API_KEY",
                },
            }
        )
    )


_UPSTREAM_IMAGE_RESPONSE = (
    b'{"data":[{"b64_json":"aW1hZ2U="}],"model":"gemini-3-pro-image-preview"}'
)


@pytest.mark.anyio
async def test_generations_transforms_request_and_adds_created(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Transform an OpenAI-style request and return an OpenAI-style response."""
    config = _gemini_image_config(
        monkeypatch,
        write_config,
        gemini_image={
            "default_model": "gemini-3-pro-image-preview",
            "aspect_ratio": "1:1",
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
                "model": "gemini-3-pro-image-preview",
                "prompt": "a cat",
                "size": "1024x1024",
                "quality": "hd",
            },
        )

    assert response.status_code == 200
    assert received["method"] == "POST"
    assert received["path"] == "/v1beta/openai/images/generations"
    assert received["authorization"] == "Bearer gemini-upstream-secret"
    assert json.loads(received["body"]) == {
        "prompt": "a cat",
        "model": "gemini-3-pro-image-preview",
        "response_format": "b64_json",
        "size": "1024x1024",
        "quality": "hd",
    }
    payload = response.json()
    assert payload["data"] == [{"b64_json": "aW1hZ2U="}]
    assert payload["model"] == "gemini-3-pro-image-preview"
    assert isinstance(payload["created"], int)
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-upstream"] == "kept"


@pytest.mark.anyio
async def test_generations_preserves_existing_created(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Keep an upstream created timestamp unchanged."""
    config = _gemini_image_config(monkeypatch, write_config)
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
            "/v1/images/generations",
            json={"prompt": "a cat", "model": "gemini-3-pro-image-preview"},
        )

    assert response.status_code == 200
    assert response.json() == {"created": 111, "data": []}


@pytest.mark.anyio
async def test_generations_rejects_invalid_request_before_upstream(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject an invalid request locally without contacting the upstream."""
    config = _gemini_image_config(monkeypatch, write_config)
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
    """Reject paths outside the gemini-image route table, including Grok listings."""
    config = _gemini_image_config(monkeypatch, write_config)
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
        chat = await client.post("/v1/chat/completions", json={"input": "hi"})
        image_models = await client.get("/v1/image-generation-models")

    assert chat.status_code == 404
    assert chat.json() == {
        "detail": "This path is not supported in 'gemini-image' mode."
    }
    assert image_models.status_code == 404
    assert image_models.json() == {
        "detail": "This path is not supported in 'gemini-image' mode."
    }
    assert not contacted


@pytest.mark.anyio
async def test_unsupported_method_returns_405(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject a supported path reached with an unsupported method."""
    config = _gemini_image_config(monkeypatch, write_config)
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
        generations = await client.get("/v1/images/generations")
        models = await client.post("/v1/models")

    assert generations.status_code == 405
    assert generations.json() == {
        "detail": "This path is not supported in 'gemini-image' mode."
    }
    assert models.status_code == 405
    assert models.json() == {
        "detail": "This path is not supported in 'gemini-image' mode."
    }
    assert not contacted


@pytest.mark.anyio
async def test_model_listings_are_rewritten_and_passed_through(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Relay model listings to the rewritten path unchanged."""
    config = _gemini_image_config(monkeypatch, write_config)
    upstream_body = b'{"object":"list","data":[{"id":"models/gemini-3-pro-image-preview"}]}'
    received: dict[str, object] = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.path
        received["query"] = request.url.query
        received["authorization"] = request.headers["authorization"]
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
        response = await client.get("/v1/models?list=images")

    assert response.status_code == 200
    assert response.content == upstream_body
    assert received["path"] == "/v1beta/openai/models"
    assert received["query"] == b"list=images"
    assert received["authorization"] == "Bearer gemini-upstream-secret"
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.anyio
async def test_upstream_error_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Relay upstream errors without changing status or body."""
    config = _gemini_image_config(monkeypatch, write_config)
    error_body = b'{"error":{"code":404,"message":"not supported for predict"}}'

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
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
            json={"prompt": "a cat", "model": "gemini-3.1-flash-image"},
        )

    assert response.status_code == 404
    assert response.content == error_body
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.anyio
async def test_upstream_non_json_success_passes_through(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Return an unparseable upstream success body unchanged."""
    config = _gemini_image_config(monkeypatch, write_config)
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
            "/v1/images/generations",
            json={"prompt": "a cat", "model": "gemini-3-pro-image-preview"},
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
    config = _gemini_image_config(monkeypatch, write_config)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            json={"prompt": "a cat", "model": "gemini-3-pro-image-preview"},
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Unable to connect to the upstream provider."}


@pytest.mark.anyio
async def test_health_does_not_contact_upstream_in_gemini_image_mode(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Keep serving health checks without forwarding them in gemini-image mode."""
    config = _gemini_image_config(monkeypatch, write_config)
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
    log_path = tmp_path / "gemini.jsonl"
    config = _gemini_image_config(
        monkeypatch,
        write_config,
        gemini_image={"default_model": "gemini-3-pro-image-preview"},
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
            "model": "gemini-3-pro-image-preview",
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
    assert "gemini-upstream-secret" not in serialized
    sent = records[1]
    transformed = json.dumps(
        {
            "prompt": "a cat",
            "model": "gemini-3-pro-image-preview",
            "response_format": "b64_json",
            "api_key": "client-secret",
            "size": "1024x1024",
        }
    ).encode()
    assert sent["upstream_request_bytes"] == len(transformed)
    assert sent["upstream_request_sha256"] == hashlib.sha256(transformed).hexdigest()
    completed = records[-1]
    assert completed["request"]["body"] == (
        '{"prompt":"a cat","model":"gemini-3-pro-image-preview",'
        '"api_key":"[REDACTED]","size":"1024x1024"}'
    )
    assert completed["response"]["body"] == response_body.decode()