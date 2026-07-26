"""Streaming-safe structured logging for proxy HTTP exchanges."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import queue
import sys
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

from niche_llm_proxy.config import LoggingFeatureConfig


_REDACTED = "[REDACTED]"
_SENSITIVE_NAME_PARTS = ("token", "secret", "password", "api_key")
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
    }
)
_BINARY_MEDIA_PREFIXES = ("audio/", "image/", "video/")
_BINARY_MEDIA_TYPES = frozenset(
    {"application/octet-stream", "multipart/form-data"}
)


class _DroppingQueueHandler(QueueHandler):
    """Queue handler that never blocks an ASGI request when logging is saturated."""

    def __init__(self, log_queue: queue.Queue[logging.LogRecord]) -> None:
        super().__init__(log_queue)
        self._dropped_records = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        """Drop a new record if the bounded queue is full."""

        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._dropped_records += 1
            if self._dropped_records == 1 or self._dropped_records % 100 == 0:
                print(
                    "nicheLLM Proxy protocol log records were dropped because the log queue is full.",
                    file=sys.stderr,
                )


class _JsonLineFormatter(logging.Formatter):
    """Write a pre-serialized JSON record as exactly one UTF-8 log line."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the JSON message without a logging prefix or traceback."""

        return record.getMessage()


@dataclass
class _BodyCapture:
    """Observe a byte stream without retaining more than its configured prefix."""

    capture_enabled: bool
    max_body_bytes: int
    content_type: str | None = None
    byte_count: int = 0
    _prefix: bytearray = field(default_factory=bytearray)
    _hasher: Any = field(default_factory=hashlib.sha256)

    def observe(self, chunk: bytes) -> None:
        """Count, hash, and boundedly retain a forwarded byte chunk."""

        self.byte_count += len(chunk)
        self._hasher.update(chunk)
        if self.capture_enabled and self.is_textual and len(self._prefix) < self.max_body_bytes:
            remaining = self.max_body_bytes - len(self._prefix)
            self._prefix.extend(chunk[:remaining])

    @property
    def is_textual(self) -> bool:
        """Return whether the declared media type is safe to represent as text."""

        if not self.content_type:
            return False
        media_type = self.content_type.split(";", maxsplit=1)[0].strip().lower()
        return not (
            media_type in _BINARY_MEDIA_TYPES
            or media_type.startswith(_BINARY_MEDIA_PREFIXES)
        )

    def metadata(self, json_field_names: frozenset[str]) -> dict[str, Any]:
        """Return safe body-capture metadata for a completed exchange record."""

        metadata: dict[str, Any] = {
            "body_bytes": self.byte_count,
            "body_sha256": self._hasher.hexdigest(),
        }
        if not self.capture_enabled:
            return metadata | {"body_captured": False, "body_omitted_reason": "disabled"}
        if not self.is_textual:
            return metadata | {
                "body_captured": False,
                "body_omitted_reason": "unsupported_binary_media_type",
            }

        try:
            text = bytes(self._prefix).decode("utf-8")
        except UnicodeDecodeError:
            return metadata | {
                "body_captured": False,
                "body_omitted_reason": "non_utf8_text",
            }

        if _is_json_media_type(self.content_type):
            text = _redact_json_text(text, json_field_names)
        return metadata | {
            "body_captured": True,
            "body": text,
            "body_truncated": self.byte_count > len(self._prefix),
            "captured_body_bytes": len(self._prefix),
        }


class LoggingRuntime:
    """Own the asynchronous handlers for a configured logging feature."""

    def __init__(self, config: LoggingFeatureConfig) -> None:
        self.config = config
        self._queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=10_000)
        self._logger = logging.getLogger(f"niche_llm_proxy.protocol.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.addHandler(_DroppingQueueHandler(self._queue))
        self._listener = QueueListener(self._queue, *self._handlers(config))
        self._listener.start()
        self._closed = False

    def _handlers(self, config: LoggingFeatureConfig) -> list[logging.Handler]:
        """Build only the configured stdout and rotating-file destinations."""

        formatter = _JsonLineFormatter()
        handlers: list[logging.Handler] = []
        if config.stdout:
            stdout_handler = logging.StreamHandler(sys.stdout)
            stdout_handler.setFormatter(formatter)
            handlers.append(stdout_handler)
        if config.file.enabled and config.file.path is not None:
            config.file.path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                config.file.path,
                maxBytes=config.file.max_bytes,
                backupCount=config.file.backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        return handlers

    def new_exchange(
        self,
        *,
        method: str,
        path: str,
        query: str,
        request_headers: Sequence[tuple[bytes, bytes]],
    ) -> "ExchangeLog":
        """Create and immediately announce a request-scoped protocol exchange."""

        exchange = ExchangeLog(
            runtime=self,
            request_id=str(uuid.uuid4()),
            method=method,
            path=path,
            query=_redact_query(query, self.config.redaction.additional_query_parameter_names),
            request_headers=_redact_headers(
                request_headers,
                self.config.redaction.additional_header_names,
            ),
            request_capture=_BodyCapture(
                self.config.capture.bodies,
                self.config.capture.max_body_bytes,
                _header_value(request_headers, b"content-type"),
            ),
        )
        exchange.emit(
            "request_received",
            request_headers=exchange.request_headers,
            request_content_type=exchange.request_capture.content_type,
        )
        return exchange

    def emit(self, event: dict[str, Any]) -> None:
        """Enqueue an already-sanitized structured event without raising to the proxy."""

        if self._closed:
            return
        try:
            self._logger.info(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            print("nicheLLM Proxy could not emit a protocol log record.", file=sys.stderr)

    def close(self) -> None:
        """Flush and close background logging resources once on app shutdown."""

        if self._closed:
            return
        self._closed = True
        while True:
            try:
                self._listener.stop()
                break
            except queue.Full:
                # QueueListener uses a non-blocking sentinel enqueue. Let its worker
                # drain one record before retrying so ASGI shutdown remains reliable.
                time.sleep(0.001)
        for handler in self._listener.handlers:
            handler.close()


@dataclass
class ExchangeLog:
    """Accumulate safe facts about one proxied HTTP exchange."""

    runtime: LoggingRuntime
    request_id: str
    method: str
    path: str
    query: str
    request_headers: list[tuple[str, str]]
    request_capture: _BodyCapture
    started_at: float = field(default_factory=time.perf_counter)
    response_capture: _BodyCapture | None = None
    response_status: int | None = None
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    _finalized: bool = False

    def emit(self, event: str, **values: Any) -> None:
        """Emit a lifecycle event with common correlation fields."""

        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": "INFO" if event not in {"exchange_failed", "exchange_cancelled"} else "WARNING",
            "event": event,
            "request_id": self.request_id,
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "elapsed_ms": round((time.perf_counter() - self.started_at) * 1000, 3),
        }
        record.update(values)
        self.runtime.emit(record)

    async def wrap_request(self, stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Tee request chunks to bounded observation while yielding them unchanged."""

        try:
            async for chunk in stream:
                self.request_capture.observe(chunk)
                yield chunk
        except asyncio.CancelledError:
            self.cancel("request_stream")
            raise
        except BaseException as error:
            self.fail("request_stream", error)
            raise

    def response_started(
        self,
        status_code: int,
        response_headers: Sequence[tuple[bytes, bytes]],
        upstream_origin: str,
    ) -> None:
        """Record response metadata before any potentially long streaming body."""

        self.response_status = status_code
        self.response_headers = _redact_headers(
            response_headers,
            self.runtime.config.redaction.additional_header_names,
        )
        self.response_capture = _BodyCapture(
            self.runtime.config.capture.bodies,
            self.runtime.config.capture.max_body_bytes,
            _header_value(response_headers, b"content-type"),
        )
        self.emit(
            "upstream_response_started",
            upstream_origin=upstream_origin,
            upstream_status=status_code,
            response_headers=self.response_headers,
        )

    def observe_response(self, chunk: bytes) -> None:
        """Observe a response chunk after it arrives and before it is yielded unchanged."""

        if self.response_capture is not None:
            self.response_capture.observe(chunk)

    def complete(self) -> None:
        """Emit one final exchange record after the response stream is exhausted."""

        if self._finalized:
            return
        self._finalized = True
        response_metadata = (
            self.response_capture.metadata(
                self.runtime.config.redaction.additional_json_field_names
            )
            if self.response_capture is not None
            else {"body_captured": False, "body_omitted_reason": "no_upstream_response"}
        )
        self.emit(
            "exchange_completed",
            upstream_status=self.response_status,
            request=self.request_capture.metadata(
                self.runtime.config.redaction.additional_json_field_names
            ),
            response=response_metadata,
        )

    def fail(self, stage: str, error: BaseException) -> None:
        """Emit a safe failure category without error text, body bytes, or headers."""

        if self._finalized:
            return
        self._finalized = True
        self.emit(
            "exchange_failed",
            stage=stage,
            error_type=type(error).__name__,
            upstream_status=self.response_status,
        )

    def cancel(self, stage: str) -> None:
        """Emit a safe cancellation record while preserving cancellation semantics."""

        if self._finalized:
            return
        self._finalized = True
        self.emit(
            "exchange_cancelled",
            stage=stage,
            upstream_status=self.response_status,
            request_bytes=self.request_capture.byte_count,
            response_bytes=(self.response_capture.byte_count if self.response_capture else 0),
        )


def _header_value(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> str | None:
    """Return the last decodable value of a case-insensitive raw header name."""

    for header_name, value in reversed(headers):
        if header_name.lower() == name:
            return value.decode("latin-1")
    return None


def _redact_headers(
    headers: Sequence[tuple[bytes, bytes]],
    additional_names: frozenset[str],
) -> list[tuple[str, str]]:
    """Decode header pairs for logs while redacting credentials by their names."""

    redacted_headers: list[tuple[str, str]] = []
    for name, value in headers:
        decoded_name = name.decode("latin-1")
        decoded_value = value.decode("latin-1")
        redacted_headers.append(
            (
                decoded_name,
                _REDACTED
                if _is_sensitive_name(decoded_name, additional_names)
                else decoded_value,
            )
        )
    return redacted_headers


def _redact_query(query: str, additional_names: frozenset[str]) -> str:
    """Redact sensitive query parameter values while retaining readable structure."""

    return urlencode(
        [
            (name, _REDACTED if _is_sensitive_name(name, additional_names) else value)
            for name, value in parse_qsl(query, keep_blank_values=True)
        ],
        doseq=True,
    )


def _is_sensitive_name(name: str, additional_names: frozenset[str]) -> bool:
    """Determine whether a metadata field's value must be replaced."""

    normalized = name.strip().lower().replace("-", "_")
    return (
        name.strip().lower() in _SENSITIVE_HEADER_NAMES
        or normalized in additional_names
        or any(part in normalized for part in _SENSITIVE_NAME_PARTS)
    )


def _is_json_media_type(content_type: str | None) -> bool:
    """Return whether a media type carries a JSON document suitable for redaction."""

    if not content_type:
        return False
    media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _redact_json_text(text: str, additional_names: frozenset[str]) -> str:
    """Redact configured or credential-like JSON values without parsing proxy traffic."""

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(
        _redact_json_value(value, additional_names),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _redact_json_value(value: Any, additional_names: frozenset[str]) -> Any:
    """Recursively redact object values selected by JSON field name."""

    if isinstance(value, dict):
        return {
            key: (
                _REDACTED
                if isinstance(key, str) and _is_sensitive_name(key, additional_names)
                else _redact_json_value(item, additional_names)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item, additional_names) for item in value]
    return value
