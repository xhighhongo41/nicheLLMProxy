"""Tests for the CLI startup announcement and configuration warning display."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from niche_llm_proxy.app import PROXY_VERSION
from niche_llm_proxy.config import (
    ListenerConfig,
    LoggingCaptureConfig,
    LoggingFeatureConfig,
    LoggingFileConfig,
    LoggingRedactionConfig,
    ProxyConfig,
    TimeoutConfig,
    UpstreamConfig,
    load_config,
)
from niche_llm_proxy.main import announce_startup, build_startup_message


def _config(
    *,
    mode: str = "passthrough",
    features: tuple[LoggingFeatureConfig, ...] = (),
    warnings: tuple[str, ...] = (),
) -> ProxyConfig:
    """Build a configuration whose listener mode, features, and warnings vary."""

    return ProxyConfig(
        listener=ListenerConfig(port=8000, mode=mode, features=features),
        upstream=UpstreamConfig(
            base_url="https://upstream.example.test",
            api_key_env="UPSTREAM_API_KEY",
            api_key="upstream-secret",
        ),
        timeouts=TimeoutConfig(connect_seconds=1, read_seconds=2),
        warnings=warnings,
    )


def _logging_feature() -> LoggingFeatureConfig:
    """Build a minimal enabled logging feature configuration."""

    return LoggingFeatureConfig(
        stdout=True,
        file=LoggingFileConfig(enabled=False),
        capture=LoggingCaptureConfig(),
        redaction=LoggingRedactionConfig(),
    )


def test_build_startup_message_with_features_includes_version_mode_and_feature_name() -> None:
    """The startup line names the version, mode, and comma-joined feature names."""

    config = _config(features=(_logging_feature(),))

    assert (
        build_startup_message(config)
        == f"nicheLLM Proxy {PROXY_VERSION} starting in 'passthrough' mode with features: logging."
    )


def test_build_startup_message_without_features_uses_no_features_wording() -> None:
    """The startup line states that no features are enabled when none are configured."""

    config = _config()

    assert (
        build_startup_message(config)
        == f"nicheLLM Proxy {PROXY_VERSION} starting in 'passthrough' mode with no features."
    )


def test_build_startup_message_uses_configured_mode() -> None:
    """The startup line reflects the configured listener mode."""

    config = _config(mode="gemini-image")

    assert (
        build_startup_message(config)
        == f"nicheLLM Proxy {PROXY_VERSION} starting in 'gemini-image' mode with no features."
    )


def test_build_startup_message_omits_port_information() -> None:
    """The startup line leaves port information to the server log."""

    message = build_startup_message(_config(features=(_logging_feature(),)))

    assert "8000" not in message


def test_build_startup_message_translates_to_japanese_with_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Japanese catalog translates the features startup line."""

    monkeypatch.setenv("NICHELLM_LANGUAGE", "ja")
    config = _config(features=(_logging_feature(),))

    assert (
        build_startup_message(config)
        == f"nicheLLM Proxy {PROXY_VERSION} を 'passthrough' モードで起動します。フィーチャー: logging。"
    )


def test_build_startup_message_translates_to_japanese_without_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Japanese catalog translates the no-features startup line."""

    monkeypatch.setenv("NICHELLM_LANGUAGE", "ja")
    config = _config()

    assert (
        build_startup_message(config)
        == f"nicheLLM Proxy {PROXY_VERSION} を 'passthrough' モードで起動します。フィーチャーはありません。"
    )


def test_announce_startup_prints_startup_line_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The startup line goes to stdout and nothing goes to stderr without warnings."""

    config = _config(features=(_logging_feature(),))

    announce_startup(config)

    out, err = capsys.readouterr()
    assert out == (
        f"nicheLLM Proxy {PROXY_VERSION} starting in 'passthrough' "
        "mode with features: logging.\n"
    )
    assert err == ""


def test_announce_startup_prints_each_warning_to_stderr_in_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pre-translated configuration warnings are printed once each to stderr."""

    config = _config(warnings=("warning: first", "warning: second"))

    announce_startup(config)

    out, err = capsys.readouterr()
    assert out.endswith("with no features.\n")
    assert err == "warning: first\nwarning: second\n"


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
        f"nicheLLM Proxy {PROXY_VERSION} を 'passthrough' モードで起動します。"
        "フィーチャーはありません。\n"
    )
    assert err == "未知の最上位設定キーは無視されました: bogus_setting。\n"