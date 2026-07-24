"""Tests for HTTP relaying through the passthrough proxy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from niche_llm_proxy.app import create_app
from niche_llm_proxy.config import ProxyConfig
from niche_llm_proxy.passthrough import stream_response


class _TrackingStream(httpx.AsyncByteStream):
    """An upstream byte stream whose close state is visible to lifecycle tests."""

    def __init__(
        self,
        chunks: tuple[bytes, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


class _CloseRecorder:
    """Minimal HTTPX-client substitute for verifying stream cleanup."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _SendFailureClient(_CloseRecorder):
    """HTTP client substitute that raises while starting an upstream request."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def build_request(self, *args: object, **kwargs: object) -> httpx.Request:
        """Build a request compatible with the application call site."""
        return httpx.Request(*args, **kwargs)  # type: ignore[arg-type]

    async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        """Raise the configured error before an upstream response exists."""
        del request, stream
        raise self.error


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


@pytest.mark.anyio
async def test_passthrough_closes_client_when_send_raises_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
    proxy_config: ProxyConfig,
) -> None:
    """Close the per-request client before propagating a non-HTTPX send error."""
    upstream_client = _SendFailureClient(RuntimeError("request stream failed"))
    monkeypatch.setattr(
        "niche_llm_proxy.app.create_http_client",
        lambda *_args, **_kwargs: upstream_client,
    )
    app = create_app(proxy_config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        with pytest.raises(RuntimeError, match="request stream failed"):
            await client.post("/v1/files", content=b"partial-upload")

    assert upstream_client.closed


@pytest.mark.anyio
async def test_passthrough_closes_client_when_send_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    proxy_config: ProxyConfig,
) -> None:
    """Close the per-request client before propagating upstream send cancellation."""
    upstream_client = _SendFailureClient(asyncio.CancelledError())
    monkeypatch.setattr(
        "niche_llm_proxy.app.create_http_client",
        lambda *_args, **_kwargs: upstream_client,
    )
    app = create_app(proxy_config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.post("/v1/files", content=b"partial-upload")

    assert upstream_client.closed


@pytest.mark.anyio
async def test_responses_json_preserves_raw_body_query_and_unknown_fields(
    proxy_config: ProxyConfig,
) -> None:
    """Relay a Responses request without parsing its JSON representation."""
    received: dict[str, Any] = {}
    body = b'{"model":"gpt-test","input":"hi","provider_extension":{"x":true}}'

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        received["method"] = request.method
        received["url"] = str(request.url)
        received["body"] = request.content
        return httpx.Response(
            201,
            stream=_TrackingStream((b'{"id":"resp_123"}',)),
            request=request,
        )

    app = create_app(proxy_config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/responses?include=reasoning.encrypted_content&include=output_text",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 201
    assert received["method"] == "POST"
    assert received["url"].endswith(
        "/v1/responses?include=reasoning.encrypted_content&include=output_text"
    )
    assert received["body"] == body


@pytest.mark.anyio
async def test_responses_sse_preserves_events_and_end_to_end_headers(
    proxy_config: ProxyConfig,
) -> None:
    """Relay Responses SSE events as received, without assuming a [DONE] event."""
    events = (
        b"event: response.created\n"
        b'data: {"type":"response.created"}\n\n'
        b"event: response.output_text.delta\n"
        b'data: {"delta":"hello"}\n\n'
        b"event: response.completed\n"
        b'data: {"type":"response.completed"}\n\n'
    )

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            stream=_TrackingStream((events,)),
            request=request,
        )

    app = create_app(proxy_config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        async with client.stream("POST", "/v1/responses", content=b'{"stream":true}') as response:
            content = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert content == events


@pytest.mark.parametrize(
    "path",
    ["/v1/files", "/v1/audio/transcriptions", "/v1/images/edits"],
)
@pytest.mark.anyio
async def test_multipart_request_preserves_raw_non_utf8_bytes(
    proxy_config: ProxyConfig,
    path: str,
) -> None:
    """Forward multipart bodies byte-for-byte, including NUL and non-UTF-8 bytes."""
    boundary = "niche-boundary"
    body = (
        b"--niche-boundary\r\n"
        b'Content-Disposition: form-data; name="purpose"\r\n\r\nassistants\r\n'
        b"--niche-boundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="sample.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
        b"prefix\x00\xff\x80suffix\r\n"
        b"--niche-boundary--\r\n"
    )
    received: dict[str, Any] = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        received["content_type"] = request.headers["content-type"]
        received["body"] = request.content
        return httpx.Response(
            200,
            stream=_TrackingStream((b'{"ok":true}',)),
            request=request,
        )

    app = create_app(proxy_config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            path,
            content=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    assert response.status_code == 200
    assert received["content_type"] == f"multipart/form-data; boundary={boundary}"
    assert received["body"] == body


@pytest.mark.anyio
async def test_binary_response_preserves_bytes_and_content_metadata(
    proxy_config: ProxyConfig,
) -> None:
    """Return binary audio bytes and representation metadata unchanged."""
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x15\x00\xff\x00binary-audio"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Disposition": 'attachment; filename="speech.mp3"',
                "Content-Length": str(len(audio)),
                "ETag": '"audio-v1"',
            },
            stream=_TrackingStream((audio,)),
            request=request,
        )

    app = create_app(proxy_config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post("/v1/audio/speech", content=b'{"input":"hello"}')

    assert response.status_code == 200
    assert response.content == audio
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["content-disposition"] == 'attachment; filename="speech.mp3"'
    assert response.headers["content-length"] == str(len(audio))
    assert response.headers["etag"] == '"audio-v1"'


@pytest.mark.anyio
async def test_range_request_and_partial_binary_response_are_preserved(
    proxy_config: ProxyConfig,
) -> None:
    """Forward Range and preserve a 206 file-content response unchanged."""
    partial = b"\x00\xfffile-slice"
    received: dict[str, str] = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        received["range"] = request.headers["range"]
        return httpx.Response(
            206,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": "bytes 10-21/100",
                "Content-Type": "application/octet-stream",
            },
            stream=_TrackingStream((partial,)),
            request=request,
        )

    app = create_app(proxy_config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.get("/v1/files/file_123/content", headers={"Range": "bytes=10-21"})

    assert received["range"] == "bytes=10-21"
    assert response.status_code == 206
    assert response.content == partial
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == "bytes 10-21/100"


@pytest.mark.anyio
async def test_duplicate_headers_and_connection_named_headers_are_handled(
    proxy_config: ProxyConfig,
) -> None:
    """Keep duplicate end-to-end headers while stripping hop-by-hop connection tokens."""
    received: dict[str, list[tuple[bytes, bytes]]] = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        received["headers"] = list(request.headers.raw)
        return httpx.Response(
            200,
            headers=[
                (b"x-upstream-duplicate", b"first"),
                (b"x-upstream-duplicate", b"second"),
                (b"connection", b"x-upstream-remove"),
                (b"x-upstream-remove", b"do-not-return"),
                (b"x-request-id", b"req_123"),
            ],
            stream=_TrackingStream((b"ok",)),
            request=request,
        )

    app = create_app(proxy_config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.get(
            "/v1/models",
            headers=[
                ("Authorization", "Bearer client-secret"),
                ("Connection", "x-client-remove"),
                ("X-Client-Remove", "do-not-forward"),
                ("X-Trace", "first"),
                ("X-Trace", "second"),
            ],
        )

    request_headers = received["headers"]
    assert [value for name, value in request_headers if name.lower() == b"x-trace"] == [
        b"first",
        b"second",
    ]
    # HTTPX may add its own transport-level ``Connection: keep-alive`` header.
    # The client-supplied connection token itself must not be propagated.
    assert not any(
        name.lower() == b"connection" and b"x-client-remove" in value.lower()
        for name, value in request_headers
    )
    assert not any(name.lower() == b"x-client-remove" for name, _ in request_headers)
    assert [value for name, value in request_headers if name.lower() == b"authorization"] == [
        b"Bearer upstream-secret"
    ]
    assert response.headers.get_list("x-upstream-duplicate") == ["first", "second"]
    assert "connection" not in response.headers
    assert "x-upstream-remove" not in response.headers
    assert response.headers["x-request-id"] == "req_123"


@pytest.mark.anyio
async def test_stream_response_closes_response_and_client_after_normal_completion() -> None:
    """Close both resources after an upstream stream completes normally."""
    upstream_stream = _TrackingStream((b"one", b"two"))
    response = httpx.Response(200, stream=upstream_stream)
    client = _CloseRecorder()

    content = b"".join([chunk async for chunk in stream_response(response, client)])  # type: ignore[arg-type]

    assert content == b"onetwo"
    assert upstream_stream.closed
    assert client.closed


@pytest.mark.anyio
async def test_stream_response_closes_response_and_client_after_stream_error() -> None:
    """Close both resources when the upstream iterator raises an exception."""
    upstream_stream = _TrackingStream(error=RuntimeError("upstream stream failed"))
    response = httpx.Response(200, stream=upstream_stream)
    client = _CloseRecorder()

    with pytest.raises(RuntimeError, match="upstream stream failed"):
        async for _chunk in stream_response(response, client):  # type: ignore[arg-type]
            pass

    assert upstream_stream.closed
    assert client.closed


@pytest.mark.anyio
async def test_stream_response_closes_response_and_client_when_consumer_is_cancelled() -> None:
    """Close both resources when cancellation interrupts response consumption."""
    first_chunk_received = asyncio.Event()
    never = asyncio.Event()

    class BlockingStream(_TrackingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"first"
            await never.wait()

    upstream_stream = BlockingStream()
    response = httpx.Response(200, stream=upstream_stream)
    client = _CloseRecorder()

    async def consume() -> None:
        async for _chunk in stream_response(response, client):  # type: ignore[arg-type]
            first_chunk_received.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_chunk_received.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert upstream_stream.closed
    assert client.closed
