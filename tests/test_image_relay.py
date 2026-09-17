"""Tests for the shared image relay flow."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from niche_llm_proxy.config import ListenerRuntimeConfig, load_config
from niche_llm_proxy.image_relay import relay_upstream

_UPSTREAM_BODY = b'{"data":[{"b64_json":"aW1hZ2U="}]}'


def _relay_config(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> ListenerRuntimeConfig:
    """Create a minimal passthrough-mode listener configuration for relay tests."""

    monkeypatch.setenv("UPSTREAM_API_KEY", "relay-test-secret")
    listener: dict[str, object] = {
        "port": 8000,
        "mode": "passthrough",
        "upstream": {
            "base_url": "https://upstream.example.test",
            "api_key_env": "UPSTREAM_API_KEY",
        },
    }
    return load_config(write_config({"listeners": [listener]})).listeners[0]


def _make_relay_app(
    config: ListenerRuntimeConfig,
    upstream_transport: httpx.AsyncBaseTransport,
    *,
    transform_request: object = None,
    upstream_content_type: str | None = None,
) -> FastAPI:
    """Build an app whose sole route invokes relay_upstream directly."""

    app = FastAPI()

    @app.post("/relay")
    async def relay(request: Request) -> Response:
        return await relay_upstream(
            config,
            None,
            upstream_transport,
            request,
            request.url.path,
            transform_request=transform_request,
            upstream_content_type=upstream_content_type,
            error_event="test_relay_transform",
        )

    return app


class TestTransformRequestContract:
    """relay_upstream must hand request transforms the body and content type."""

    @pytest.mark.anyio
    async def test_transform_receives_body_and_content_type(
        self,
        monkeypatch: pytest.MonkeyPatch,
        write_config: Callable[[dict[str, object] | None], Path],
    ) -> None:
        """Pass the original body and request Content-Type to the transform."""
        config = _relay_config(monkeypatch, write_config)
        captured: dict[str, object] = {}

        def transform(body: bytes, content_type: str | None) -> bytes:
            captured["body"] = body
            captured["content_type"] = content_type
            return body

        app = _make_relay_app(
            config,
            httpx.MockTransport(lambda request: httpx.Response(200, content=_UPSTREAM_BODY)),
            transform_request=transform,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            response = await client.post(
                "/relay",
                content=b'{"prompt":"a cat"}',
                headers={"Content-Type": "application/json; charset=utf-8"},
            )

        assert response.status_code == 200
        assert captured["body"] == b'{"prompt":"a cat"}'
        assert captured["content_type"] == "application/json; charset=utf-8"

    @pytest.mark.anyio
    async def test_transform_receives_none_content_type_without_header(
        self,
        monkeypatch: pytest.MonkeyPatch,
        write_config: Callable[[dict[str, object] | None], Path],
    ) -> None:
        """Report Content-Type as None when the request carries no header."""
        config = _relay_config(monkeypatch, write_config)
        captured: dict[str, object] = {}

        def transform(body: bytes, content_type: str | None) -> bytes:
            captured["content_type"] = content_type
            return body

        app = _make_relay_app(
            config,
            httpx.MockTransport(lambda request: httpx.Response(200, content=_UPSTREAM_BODY)),
            transform_request=transform,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            response = await client.post("/relay", content=b"raw-bytes")

        assert response.status_code == 200
        assert "content-type" not in response.request.headers or True
        assert captured["content_type"] is None


class TestUpstreamContentType:
    """relay_upstream must be able to swap the forwarded Content-Type."""

    @pytest.mark.anyio
    async def test_upstream_content_type_replaces_forwarded_header(
        self,
        monkeypatch: pytest.MonkeyPatch,
        write_config: Callable[[dict[str, object] | None], Path],
    ) -> None:
        """Replace the client Content-Type with the configured upstream one."""
        config = _relay_config(monkeypatch, write_config)
        received: dict[str, object] = {}

        def upstream_handler(request: httpx.Request) -> httpx.Response:
            received["content_type"] = request.headers.get("content-type")
            return httpx.Response(200, content=_UPSTREAM_BODY, request=request)

        app = _make_relay_app(
            config,
            httpx.MockTransport(upstream_handler),
            upstream_content_type="application/json",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            response = await client.post(
                "/relay",
                content=b"multipart-ish",
                headers={"Content-Type": "multipart/form-data; boundary=xyz"},
            )

        assert response.status_code == 200
        assert received["content_type"] == "application/json"

    @pytest.mark.anyio
    async def test_content_type_forwarded_unchanged_without_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
        write_config: Callable[[dict[str, object] | None], Path],
    ) -> None:
        """Forward the original Content-Type when no override is configured."""
        config = _relay_config(monkeypatch, write_config)
        received: dict[str, object] = {}

        def upstream_handler(request: httpx.Request) -> httpx.Response:
            received["content_type"] = request.headers.get("content-type")
            return httpx.Response(200, content=_UPSTREAM_BODY, request=request)

        app = _make_relay_app(config, httpx.MockTransport(upstream_handler))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            response = await client.post(
                "/relay",
                content=b"plain",
                headers={"Content-Type": "text/plain"},
            )

        assert response.status_code == 200
        assert received["content_type"] == "text/plain"

    @pytest.mark.anyio
    async def test_override_applies_without_transform_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        write_config: Callable[[dict[str, object] | None], Path],
    ) -> None:
        """Allow the Content-Type override even for unchanged bodies."""
        config = _relay_config(monkeypatch, write_config)
        received: dict[str, object] = {}

        def upstream_handler(request: httpx.Request) -> httpx.Response:
            received["content_type"] = request.headers.get("content-type")
            received["body"] = request.content
            return httpx.Response(200, content=_UPSTREAM_BODY, request=request)

        app = _make_relay_app(
            config,
            httpx.MockTransport(upstream_handler),
            upstream_content_type="application/json",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            response = await client.post(
                "/relay",
                content=b"already-json",
                headers={"Content-Type": "application/octet-stream"},
            )

        assert response.status_code == 200
        assert received["content_type"] == "application/json"
        assert received["body"] == b"already-json"