"""Tests for the CLI startup announcement and the multi-listener server."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from niche_llm_proxy.app import PROXY_VERSION
from niche_llm_proxy.config import (
    ListenerConfig,
    ListenerRuntimeConfig,
    LoggingCaptureConfig,
    LoggingFeatureConfig,
    LoggingFileConfig,
    LoggingRedactionConfig,
    ProxyConfig,
    TimeoutConfig,
    UpstreamConfig,
    load_config,
)
from niche_llm_proxy.main import (
    announce_startup,
    build_servers,
    build_startup_message,
    serve_all,
)


def _logging_feature() -> LoggingFeatureConfig:
    """Build a minimal enabled logging feature configuration."""

    return LoggingFeatureConfig(
        stdout=True,
        file=LoggingFileConfig(enabled=False),
        capture=LoggingCaptureConfig(),
        redaction=LoggingRedactionConfig(),
    )


def _listener_runtime(
    *,
    port: int = 8000,
    mode: str = "passthrough",
    features: tuple[LoggingFeatureConfig, ...] = (),
    warnings: tuple[str, ...] = (),
) -> ListenerRuntimeConfig:
    """Build one complete listener configuration for startup tests."""

    return ListenerRuntimeConfig(
        listener=ListenerConfig(port=port, mode=mode, features=features),
        upstream=UpstreamConfig(
            base_url="https://upstream.example.test",
            api_key_env="UPSTREAM_API_KEY",
            api_key="upstream-secret",
        ),
        timeouts=TimeoutConfig(connect_seconds=1, read_seconds=2),
        warnings=warnings,
    )


def _config(
    *,
    listeners: tuple[ListenerRuntimeConfig, ...] = (),
    warnings: tuple[str, ...] = (),
) -> ProxyConfig:
    """Build a whole configuration from listener runtime configs."""

    return ProxyConfig(listeners=listeners, warnings=warnings)


def _free_port() -> int:
    """Reserve an ephemeral port that is free at call time."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _poll_health(client: httpx.AsyncClient, port: int) -> httpx.Response:
    """Poll a listener's health endpoint until it responds."""

    deadline = time.monotonic() + 10.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"http://127.0.0.1:{port}/health")
            if response.status_code == 200:
                return response
            last_error = RuntimeError(f"unexpected status {response.status_code}")
        except httpx.HTTPError as error:
            last_error = error
        await asyncio.sleep(0.05)
    raise AssertionError(f"health endpoint on port {port} did not come up: {last_error}")


def test_build_startup_message_with_single_listener_reports_count_port_and_features() -> None:
    """A single listener is announced with its count, port, mode, and features."""

    config = _config(listeners=(_listener_runtime(features=(_logging_feature(),)),))

    assert build_startup_message(config) == (
        f"nicheLLM Proxy {PROXY_VERSION} starting with 1 listener.\n"
        "  - port 8000: mode 'passthrough' with features: logging."
    )


def test_build_startup_message_without_features_uses_no_features_wording() -> None:
    """A listener without enabled features states that no features are enabled."""

    config = _config(listeners=(_listener_runtime(),))

    assert build_startup_message(config) == (
        f"nicheLLM Proxy {PROXY_VERSION} starting with 1 listener.\n"
        "  - port 8000: mode 'passthrough' with no features."
    )


def test_build_startup_message_lists_each_listener_on_its_own_line() -> None:
    """Multiple listeners are announced once per listener with port and mode."""

    config = _config(
        listeners=(
            _listener_runtime(features=(_logging_feature(),)),
            _listener_runtime(port=8001, mode="grok-image"),
        ),
    )

    assert build_startup_message(config) == (
        f"nicheLLM Proxy {PROXY_VERSION} starting with 2 listeners.\n"
        "  - port 8000: mode 'passthrough' with features: logging.\n"
        "  - port 8001: mode 'grok-image' with no features."
    )


def test_build_startup_message_translates_to_japanese(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Japanese catalog translates the header line and each listener line."""

    monkeypatch.setenv("NICHELLM_LANGUAGE", "ja")
    config = _config(
        listeners=(
            _listener_runtime(features=(_logging_feature(),)),
            _listener_runtime(port=8001, mode="grok-image"),
        ),
    )

    assert build_startup_message(config) == (
        f"nicheLLM Proxy {PROXY_VERSION} を起動します。リスナー数: 2。\n"
        "  - ポート 8000: モード 'passthrough'、フィーチャー: logging。\n"
        "  - ポート 8001: モード 'grok-image'、フィーチャーはありません。"
    )


def test_announce_startup_prints_startup_lines_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The startup announcement goes to stdout and nothing goes to stderr."""

    config = _config(
        listeners=(
            _listener_runtime(features=(_logging_feature(),)),
            _listener_runtime(port=8001, mode="grok-image"),
        ),
    )

    announce_startup(config)

    out, err = capsys.readouterr()
    assert out == (
        f"nicheLLM Proxy {PROXY_VERSION} starting with 2 listeners.\n"
        "  - port 8000: mode 'passthrough' with features: logging.\n"
        "  - port 8001: mode 'grok-image' with no features.\n"
    )
    assert err == ""


def test_announce_startup_prints_global_and_listener_warnings_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Global warnings print unprefixed and listener warnings print with a port prefix."""

    config = _config(
        listeners=(
            _listener_runtime(warnings=("first listener warning",)),
            _listener_runtime(port=8001, warnings=("second listener warning",)),
        ),
        warnings=("global warning",),
    )

    announce_startup(config)

    out, err = capsys.readouterr()
    assert out.endswith("with no features.\n")
    assert err == (
        "global warning\n"
        "[port 8000] first listener warning\n"
        "[port 8001] second listener warning\n"
    )


def test_announce_startup_prints_load_config_warnings_to_stderr(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Warnings collected by load_config reach stderr through announce_startup."""

    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret")
    config = load_config(write_config({"bogus_setting": {"ignored": True}}))
    assert config.warnings == (
        "Unknown top-level configuration keys were ignored: bogus_setting.",
    )

    announce_startup(config)

    out, err = capsys.readouterr()
    assert out.endswith("with no features.\n")
    assert err == "Unknown top-level configuration keys were ignored: bogus_setting.\n"


def test_announce_startup_prints_skipped_listener_warning_to_stderr(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """A warning about a skipped listener reaches stderr through announce_startup."""

    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret")
    config = load_config(
        write_config(
            {
                "listeners": [
                    make_listener(),
                    make_listener({"port": 8001, "mode": "featherless"}),
                ]
            }
        )
    )
    assert len(config.listeners) == 1
    assert len(config.warnings) == 1

    announce_startup(config)

    out, err = capsys.readouterr()
    assert out == (
        f"nicheLLM Proxy {PROXY_VERSION} starting with 1 listener.\n"
        "  - port 8000: mode 'passthrough' with no features.\n"
    )
    assert err == f"{config.warnings[0]}\n"


def test_announce_startup_prints_japanese_startup_line_and_load_config_warnings(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Japanese startup line and already-translated warnings are printed unchanged."""

    monkeypatch.setenv("NICHELLM_LANGUAGE", "ja")
    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret")
    config = load_config(write_config({"bogus_setting": {"ignored": True}}))

    announce_startup(config)

    out, err = capsys.readouterr()
    assert out == (
        f"nicheLLM Proxy {PROXY_VERSION} を起動します。リスナー数: 1。\n"
        "  - ポート 8000: モード 'passthrough'、フィーチャーはありません。\n"
    )
    assert err == "未知の最上位設定キーは無視されました: bogus_setting。\n"


def test_build_servers_creates_one_server_per_listener() -> None:
    """Each listener gets its own uvicorn server bound to its configured port."""

    config = _config(
        listeners=(
            _listener_runtime(port=8000),
            _listener_runtime(port=8001, mode="grok-image"),
        ),
    )

    servers = build_servers(config)

    assert len(servers) == 2
    assert [server.config.port for server in servers] == [8000, 8001]
    assert servers[0].config is not servers[1].config


def test_serve_all_serves_health_on_every_listener_port() -> None:
    """Every configured listener accepts health requests while serve_all runs."""

    ports = (_free_port(), _free_port())
    listeners = tuple(_listener_runtime(port=port) for port in ports)
    servers = build_servers(_config(listeners=listeners))

    async def scenario() -> None:
        serve_task = asyncio.ensure_future(serve_all(servers))
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                for port in ports:
                    response = await _poll_health(client, port)
                    body = response.json()
                    assert body["status"] == "ok"
                    assert body["version"] == PROXY_VERSION
                    assert body["mode"] == "passthrough"
                    assert body["features"] == []
        finally:
            for server in servers:
                server.should_exit = True
            await asyncio.wait_for(serve_task, timeout=15.0)

    asyncio.run(scenario())