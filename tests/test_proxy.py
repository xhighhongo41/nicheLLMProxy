"""Tests for HTTP relaying through the passthrough proxy."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from niche_llm_proxy.app import create_app
from niche_llm_proxy.config import ProxyConfig


@pytest.mark.anyio
async def test_health_does_not_contact_upstream(proxy_config: ProxyConfig) -> None:
    """Do not forward health checks to the upstream."""
    contacted = False

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(200, request=request)

    app = create_app(proxy_config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert not contacted


@pytest.mark.anyio
async def test_passthrough_forwards_request_and_replaces_authorization(
    proxy_config: ProxyConfig,
) -> None:
    """Relay path, query, body, and allowed headers while replacing authorization."""
    received: dict[str, Any] = {}
    upstream = FastAPI()

    @upstream.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        received["method"] = request.method
        received["query"] = request.url.query
        received["body"] = await request.body()
        received["headers"] = dict(request.headers)
        return JSONResponse({"ok": True}, headers={"X-Upstream": "kept"})

    app = create_app(proxy_config, httpx.ASGITransport(app=upstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/chat/completions?trace=on",
            content=b'{"message":"hello"}',
            headers={
                "Authorization": "Bearer client-secret",
                "Connection": "keep-alive, x-remove",
                "X-Remove": "do-not-forward",
                "X-Forward": "yes",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["x-upstream"] == "kept"
    assert received["method"] == "POST"
    assert received["query"] == "trace=on"
    assert received["body"] == b'{"message":"hello"}'
    assert received["headers"]["authorization"] == "Bearer upstream-secret"
    assert received["headers"]["x-forward"] == "yes"
    assert "x-remove" not in received["headers"]


@pytest.mark.anyio
async def test_passthrough_preserves_upstream_http_error(
    proxy_config: ProxyConfig,
) -> None:
    """Return upstream HTTP errors without changing status or body."""
    upstream = FastAPI()

    @upstream.get("/v1/models")
    async def models() -> Response:
        return Response(
            content=b'{"error":"rate limited"}',
            status_code=429,
            media_type="application/json",
        )

    app = create_app(proxy_config, httpx.ASGITransport(app=upstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.get("/v1/models")

    assert response.status_code == 429
    assert response.content == b'{"error":"rate limited"}'
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.anyio
async def test_passthrough_relays_sse_chunks_in_order(
    proxy_config: ProxyConfig,
) -> None:
    """Relay SSE response contents and order unchanged to the client."""
    upstream = FastAPI()

    @upstream.post("/v1/chat/completions")
    async def stream_chat() -> StreamingResponse:
        async def events() -> AsyncIterator[bytes]:
            yield b"data: one\n\n"
            yield b"data: two\n\n"
            yield b"data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    app = create_app(proxy_config, httpx.ASGITransport(app=upstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        async with client.stream("POST", "/v1/chat/completions") as response:
            content = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert content == b"data: one\n\ndata: two\n\ndata: [DONE]\n\n"


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (httpx.ConnectError("unreachable"), 502, "Unable to connect"),
        (httpx.ConnectTimeout("slow connection"), 504, "timed out"),
        (httpx.ReadTimeout("slow response"), 504, "timed out"),
    ],
)
@pytest.mark.anyio
async def test_passthrough_returns_safe_errors_for_upstream_failures(
    proxy_config: ProxyConfig,
    error: httpx.RequestError,
    status_code: int,
    detail: str,
) -> None:
    """Map connection failures and timeouts to safe 5xx responses."""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        raise type(error)(str(error), request=request)

    app = create_app(proxy_config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post("/v1/chat/completions", json={"hello": "world"})

    assert response.status_code == status_code
    assert detail in response.json()["detail"]
    assert "upstream-secret" not in response.text


@pytest.mark.anyio
async def test_passthrough_localizes_proxy_generated_error_only(
    monkeypatch: pytest.MonkeyPatch,
    proxy_config: ProxyConfig,
) -> None:
    """Japanese changes only the proxy-generated error detail, not upstream payloads."""
    monkeypatch.setenv("NICHELLM_LANGUAGE", "ja")

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    app = create_app(proxy_config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post("/v1/chat/completions", json={"hello": "world"})

    assert response.status_code == 502
    assert response.json() == {"detail": "上流プロバイダーへ接続できません。"}
    assert "upstream-secret" not in response.text
