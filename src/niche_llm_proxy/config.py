"""Load nicheLLM Proxy startup configuration safely."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from niche_llm_proxy.i18n import translate


DEFAULT_CONFIG_PATH = Path("/app/config/config.json")
"""Default configuration file path when no environment override is set."""

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
"""Default upstream connection timeout in seconds."""

DEFAULT_READ_TIMEOUT_SECONDS = 120.0
"""Default upstream read timeout in seconds."""


class ConfigError(ValueError):
    """Raised when required startup configuration is invalid or missing."""


@dataclass(frozen=True)
class ListenerConfig:
    """Listening configuration for a single listener."""

    port: int
    mode: str


@dataclass(frozen=True)
class TimeoutConfig:
    """Timeout configuration for upstream communication."""

    connect_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS


@dataclass(frozen=True)
class UpstreamConfig:
    """Connection configuration for the upstream OpenAI-compatible API."""

    base_url: str
    api_key_env: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class ProxyConfig:
    """Validated startup configuration for nicheLLM Proxy."""

    listener: ListenerConfig
    upstream: UpstreamConfig
    timeouts: TimeoutConfig


def get_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the configuration file path after applying an environment override.

    Args:
        environ: Environment variables to read. Uses the process environment by default.

    Returns:
        `NICHELLM_CONFIG_PATH` or the default configuration file path.
    """

    environment = os.environ if environ is None else environ
    configured_path = environment.get("NICHELLM_CONFIG_PATH")
    return Path(configured_path) if configured_path else DEFAULT_CONFIG_PATH


def load_config(
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProxyConfig:
    """Load validated proxy configuration from JSON and environment variables.

    API key values are read only from environment variables and are excluded from errors.

    Args:
        config_path: Configuration file path. Uses the environment or default path if omitted.
        environ: Environment variables to read. Uses the process environment by default.

    Raises:
        ConfigError: If the configuration file, settings, or API key are invalid.

    Returns:
        Validated configuration ready for startup.
    """

    environment = os.environ if environ is None else environ
    path = Path(config_path) if config_path is not None else get_config_path(environment)
    raw_config = _read_json_object(path)

    listener_data = _required_object(raw_config, "listener")
    upstream_data = _required_object(raw_config, "upstream")
    timeout_data = _optional_object(raw_config, "timeouts")

    listener = ListenerConfig(
        port=_port(listener_data),
        mode=_passthrough_mode(listener_data),
    )
    api_key_env = _non_empty_string(upstream_data, "api_key_env")
    api_key = environment.get(api_key_env)
    if not api_key:
        raise ConfigError(
            translate(
                "Upstream API key environment variable '{api_key_env}' is not set.",
                api_key_env=api_key_env,
            )
        )

    upstream = UpstreamConfig(
        base_url=_base_url(upstream_data),
        api_key_env=api_key_env,
        api_key=api_key,
    )
    timeouts = TimeoutConfig(
        connect_seconds=_positive_number(
            timeout_data,
            "connect_seconds",
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
        ),
        read_seconds=_positive_number(
            timeout_data,
            "read_seconds",
            DEFAULT_READ_TIMEOUT_SECONDS,
        ),
    )
    return ProxyConfig(listener=listener, upstream=upstream, timeouts=timeouts)


def _read_json_object(path: Path) -> Mapping[str, Any]:
    """Read a configuration file and return its top-level JSON object."""

    try:
        with path.open(encoding="utf-8") as config_file:
            value = json.load(config_file)
    except FileNotFoundError as error:
        raise ConfigError(
            translate("Configuration file was not found: {path}", path=path)
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(translate("Unable to read the configuration JSON.")) from error

    if not isinstance(value, dict):
        raise ConfigError(translate("Top-level configuration JSON must be an object."))
    return value


def _required_object(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return a required object-valued configuration item."""

    value = config.get(key)
    if not isinstance(value, dict):
        raise ConfigError(
            translate("Configuration item '{key}' must be an object.", key=key)
        )
    return value


def _optional_object(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return an optional object-valued item or an empty mapping when absent."""

    if key not in config:
        return {}
    return _required_object(config, key)


def _port(listener: Mapping[str, Any]) -> int:
    """Validate and return the listener port."""

    value = listener.get("port")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError(
            translate("listener.port must be an integer between 1 and 65535.")
        )
    return value


def _passthrough_mode(listener: Mapping[str, Any]) -> str:
    """Validate and return the listener mode supported in v0.1."""

    mode = listener.get("mode")
    if mode != "passthrough":
        raise ConfigError(translate("listener.mode must be 'passthrough'."))
    return mode


def _base_url(upstream: Mapping[str, Any]) -> str:
    """Validate and return the upstream URL."""

    value = _non_empty_string(upstream, "base_url")
    try:
        parsed = urlparse(value)
        # Invalid ports and IPv6 literals are detected when accessing this attribute.
        parsed.port
    except ValueError as error:
        raise ConfigError(
            translate("upstream.base_url must be a valid HTTP(S) URL.")
        ) from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            translate(
                "upstream.base_url must be an HTTP(S) URL without a query or fragment."
            )
        )
    return value


def _non_empty_string(config: Mapping[str, Any], key: str) -> str:
    """Validate and return a non-empty string configuration value."""

    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            translate(
                "Configuration item '{key}' must be a non-empty string.",
                key=key,
            )
        )
    return value


def _positive_number(
    config: Mapping[str, Any],
    key: str,
    default: float,
) -> float:
    """Validate and return an optional positive numeric setting."""

    if key not in config:
        return default

    value = config[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigError(
            translate("timeouts.{key} must be a positive number.", key=key)
        )
    return float(value)
