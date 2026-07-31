"""Tests for configuration and secret validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from niche_llm_proxy.config import ConfigError, get_config_path, load_config


def test_load_config_reads_valid_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Create a configuration object from valid settings and environment variables."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(write_config())

    assert config.listener.port == 8000
    assert config.listener.mode == "passthrough"
    assert config.upstream.base_url == "https://upstream.example.test"
    assert config.upstream.api_key == "secret-value"
    assert "secret-value" not in repr(config)


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"listener": {"port": 0, "mode": "passthrough"}}, "port"),
        ({"listener": {"port": 8000, "mode": "unknown"}}, "mode"),
        ({"upstream": {"base_url": "not-a-url", "api_key_env": "UPSTREAM_API_KEY"}}, "base_url"),
    ],
)
def test_load_config_rejects_invalid_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, object],
    message: str,
) -> None:
    """Reject invalid required settings before startup."""
    complete_settings: dict[str, object] = {
        "listener": {"port": 8000, "mode": "passthrough"},
        "upstream": {
            "base_url": "https://upstream.example.test",
            "api_key_env": "UPSTREAM_API_KEY",
        },
    }
    complete_settings.update(settings)
    config_path = tmp_path / "invalid.json"
    config_path.write_text(json.dumps(complete_settings), encoding="utf-8")
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")

    with pytest.raises(ConfigError, match=message):
        load_config(config_path)


def test_load_config_does_not_expose_missing_key_value(
    tmp_path: Path,
) -> None:
    """Do not expose a secret value in an error about a missing key."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "listener": {"port": 8000, "mode": "passthrough"},
                "upstream": {
                    "base_url": "https://upstream.example.test",
                    "api_key_env": "MISSING_UPSTREAM_KEY",
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_config(config_path, environ={})

    assert "MISSING_UPSTREAM_KEY" in str(error.value)
    assert "secret" not in str(error.value).lower()


def test_get_config_path_honors_environment_override() -> None:
    """Allow the dedicated environment variable to override the config path."""
    assert get_config_path({"NICHELLM_CONFIG_PATH": "/tmp/custom.json"}) == Path(
        "/tmp/custom.json"
    )


def test_load_config_accepts_logging_feature(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    tmp_path: Path,
) -> None:
    """Load bounded stdout and rotating-file logging without storing a secret."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(
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
                                    "path": str(tmp_path / "proxy.jsonl"),
                                    "max_bytes": 100,
                                    "backup_count": 2,
                                },
                                "capture": {"bodies": True, "max_body_bytes": 100},
                                "redaction": {
                                    "additional_header_names": ["X-Customer-Secret"],
                                },
                            },
                        }
                    ],
                }
            }
        )
    )

    assert config.logging is not None
    assert config.logging.file.path == tmp_path / "proxy.jsonl"
    assert config.logging.capture.bodies
    assert config.logging.redaction.additional_header_names == {"x_customer_secret"}


@pytest.mark.parametrize(
    ("features", "message"),
    [
        ({}, "array"),
        ([{"name": "unknown", "config": {}}], "only 'logging'"),
        (
            [
                {"name": "logging", "config": {}},
                {"name": "logging", "config": {}},
            ],
            "duplicate",
        ),
        ([{"name": "logging", "config": {"stdout": False}}], "stdout or file"),
        (
            [
                {
                    "name": "logging",
                    "config": {"file": {"enabled": True, "path": "relative.log"}},
                }
            ],
            "absolute",
        ),
    ],
)
def test_load_config_rejects_invalid_logging_feature(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    features: object,
    message: str,
) -> None:
    """Reject ambiguous logging settings before the service starts."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    with pytest.raises(ConfigError, match=message):
        load_config(
            write_config(
                {
                    "listener": {
                        "port": 8000,
                        "mode": "passthrough",
                        "features": features,
                    }
                }
            )
        )
