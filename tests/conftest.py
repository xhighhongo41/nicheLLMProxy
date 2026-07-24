"""Shared test fixtures for nicheLLM Proxy."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from niche_llm_proxy.config import ProxyConfig, load_config


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[dict[str, object]], Path]:
    """Return a function that writes test configuration JSON."""

    def _write_config(overrides: dict[str, object] | None = None) -> Path:
        settings: dict[str, object] = {
            "listener": {"port": 8000, "mode": "passthrough"},
            "upstream": {
                "base_url": "https://upstream.example.test",
                "api_key_env": "UPSTREAM_API_KEY",
            },
            "timeouts": {"connect_seconds": 1, "read_seconds": 2},
        }
        if overrides:
            settings.update(overrides)
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(settings), encoding="utf-8")
        return config_path

    return _write_config


@pytest.fixture
def proxy_config(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object]], Path],
) -> ProxyConfig:
    """Return valid proxy configuration with an upstream key."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret")
    return load_config(write_config())
