"""Tests for structured, redacted, streaming-safe protocol logging."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from niche_llm_proxy.app import create_app
from niche_llm_proxy.config import ProxyConfig, load_config


class _BytesStream(httpx.AsyncByteStream):
    """Provide a response body that remains unread until the proxy streams it."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self.body

    async def aclose(self) -> None:
        pass


def _logging_config(
    write_config: Callable[[dict[str, object] | None], Path],
    log_path: Path,
    *,
    bodies: bool = True,
    max_bytes: int = 1_000_000,
    backup_count: int = 2,
) -> ProxyConfig:
    """Create one file-only logging config suitable for deterministic assertions."""

    return load_config(
        write_config(
            {
                "listener": {
                    "port": 8000,
                    "mode": "passthrough",
                    "features": [
                        {
                            "name": "logging",
                            "config": {
                                "stdout": False,
                                "file": {
                                    "enabled": True,
                                    "path": str(log_path),
                                    "max_bytes": max_bytes,
                                    "backup_count": backup_count,
                                },
                                "capture": {"bodies": bodies, "max_body_bytes": 100},
                                "redaction": {
                                    "additional_header_names": ["X-Customer-Secret"],
                                    "additional_query_parameter_names": ["private"],
                                    "additional_json_field_names": ["customer_secret"],
                                },
                            },
                        }
                    ],
                }
            }
        )
    )


def _read_records(app: FastAPI, log_path: Path) -> list[dict[str, object]]:
    """Stop the queue listener, flush handlers, and parse emitted JSON Lines."""

    app.state.logging_runtime.close()
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.anyio
async def test_logging_records_redacted_json_exchange_without_changing_bytes(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    tmp_path: Path,
) -> None:
    """Capture JSON safely while preserving the upstream request and response bytes."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret")
    log_path = tmp_path / "proxy.jsonl"
    config = _logging_config(write_config, log_path)
    received: dict[str, bytes] = {}
    body = b'{"input":"hello","api_key":"client-key","customer_secret":"customer-value"}'
    response_body = b'{"output":"world","token":"response-token"}'

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        received["body"] = request.content
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Set-Cookie": "session=private"},
            stream=_BytesStream(response_body),
            request=request,
        )

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/responses?private=hidden&trace=kept",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer client-secret",
                "X-Customer-Secret": "private-header",
            },
        )

    records = _read_records(app, log_path)
    assert response.content == response_body
    assert received["body"] == body
    assert [record["event"] for record in records] == [
        "request_received",
        "upstream_response_started",
        "exchange_completed",
    ]
    assert len({record["request_id"] for record in records}) == 1
    serialized = json.dumps(records)
    for secret in ("client-secret", "upstream-secret", "private-header", "customer-value", "response-token"):
        assert secret not in serialized
    assert "private=%5BREDACTED%5D" in serialized
    completed = records[-1]
    assert completed["request"] == {
        "body_bytes": len(body),
        "body_sha256": completed["request"]["body_sha256"],
        "body_captured": True,
        "body": '{"input":"hello","api_key":"[REDACTED]","customer_secret":"[REDACTED]"}',
        "body_truncated": False,
        "captured_body_bytes": len(body),
    }


@pytest.mark.anyio
async def test_logging_omits_binary_body_and_captures_sse_prefix(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    tmp_path: Path,
) -> None:
    """Do not log binary payloads while raw SSE bytes are still forwarded unchanged."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret")
    log_path = tmp_path / "proxy.jsonl"
    config = _logging_config(write_config, log_path)
    binary = b"binary\x00\xffpayload"
    events = b"data: first\n\ndata: second\n\n"
    call_count = 0

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            assert request.content == binary
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=_BytesStream(b"{}"),
                request=request,
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=_BytesStream(events),
            request=request,
        )

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        first = await client.post(
            "/v1/files",
            content=binary,
            headers={"Content-Type": "application/octet-stream"},
        )
        second = await client.post(
            "/v1/responses",
            content=b'{"stream":true}',
            headers={"Content-Type": "application/json"},
        )

    records = _read_records(app, log_path)
    assert first.status_code == 200
    assert second.content == events
    completed = [record for record in records if record["event"] == "exchange_completed"]
    assert completed[0]["request"]["body_omitted_reason"] == "unsupported_binary_media_type"
    assert completed[1]["response"]["body"] == events.decode()


@pytest.mark.anyio
async def test_logging_rotates_file_at_configured_size(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    tmp_path: Path,
) -> None:
    """Use size-based rotation and retain no more than the configured generations."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret")
    log_path = tmp_path / "proxy.jsonl"
    config = _logging_config(
        write_config,
        log_path,
        bodies=False,
        max_bytes=200,
        backup_count=1,
    )

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_BytesStream(b"{}"), request=request)

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        for _ in range(4):
            response = await client.post("/v1/responses", json={"input": "x" * 100})
            assert response.status_code == 200

    app.state.logging_runtime.close()
    assert log_path.exists()
    assert log_path.with_name("proxy.jsonl.1").exists()
    assert not log_path.with_name("proxy.jsonl.2").exists()


@pytest.mark.anyio
async def test_logging_records_safe_upstream_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    tmp_path: Path,
) -> None:
    """Record failure category only, without retaining the upstream error's secret."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret")
    log_path = tmp_path / "proxy.jsonl"
    config = _logging_config(write_config, log_path, bodies=False)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed: provider-secret", request=request)

    app = create_app(config, httpx.MockTransport(upstream_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post("/v1/responses", json={"input": "hello"})

    records = _read_records(app, log_path)
    assert response.status_code == 502
    assert records[-1]["event"] == "exchange_failed"
    assert records[-1]["stage"] == "upstream_send"
    assert records[-1]["error_type"] == "ConnectError"
    assert "provider-secret" not in json.dumps(records)
