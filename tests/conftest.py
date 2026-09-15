"""Shared test fixtures for nicheLLM Proxy."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from niche_llm_proxy.config import ListenerRuntimeConfig, load_config


def default_listener() -> dict[str, object]:
    """Return the default single-listener settings used by tests."""
    return {
        "port": 8000,
        "mode": "passthrough",
        "upstream": {
            "base_url": "https://upstream.example.test",
            "api_key_env": "UPSTREAM_API_KEY",
        },
        "timeouts": {"connect_seconds": 1, "read_seconds": 2},
    }


@pytest.fixture
def make_listener() -> Callable[[dict[str, object]], dict[str, object]]:
    """Return a function that builds a listener settings dictionary."""

    def _make_listener(overrides: dict[str, object] | None = None) -> dict[str, object]:
        listener = default_listener()
        if overrides:
            listener.update(overrides)
        return listener

    return _make_listener


@pytest.fixture
def write_config(
    tmp_path: Path, make_listener: Callable[[dict[str, object]], dict[str, object]]
) -> Callable[[dict[str, object]], Path]:
    """Return a function that writes test configuration JSON."""

    def _write_config(overrides: dict[str, object] | None = None) -> Path:
        settings: dict[str, object] = {"listeners": [make_listener()]}
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
) -> ListenerRuntimeConfig:
    """Return valid configuration for the first listener."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret")
    return load_config(write_config()).listeners[0]