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

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
"""Default maximum size of the active protocol log file."""

DEFAULT_LOG_BACKUP_COUNT = 5
"""Default number of rotated protocol log files to retain."""

DEFAULT_CAPTURE_MAX_BODY_BYTES = 1024 * 1024
"""Default maximum captured prefix for a request or response body."""

MAX_CAPTURE_BODY_BYTES = 10 * 1024 * 1024
"""Hard upper bound for a captured request or response body prefix."""


class ConfigError(ValueError):
    """Raised when required startup configuration is invalid or missing."""


@dataclass(frozen=True)
class ListenerConfig:
    """Listening configuration for a single listener."""

    port: int
    mode: str
    features: tuple["LoggingFeatureConfig", ...] = ()


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
class LoggingFileConfig:
    """Optional rotating protocol log file settings."""

    enabled: bool
    path: Path | None = None
    max_bytes: int = DEFAULT_LOG_MAX_BYTES
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT


@dataclass(frozen=True)
class LoggingCaptureConfig:
    """Bounded protocol body capture settings."""

    bodies: bool = False
    max_body_bytes: int = DEFAULT_CAPTURE_MAX_BODY_BYTES


@dataclass(frozen=True)
class LoggingRedactionConfig:
    """Additional case-insensitive names whose values must be redacted."""

    additional_header_names: frozenset[str] = frozenset()
    additional_query_parameter_names: frozenset[str] = frozenset()
    additional_json_field_names: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LoggingFeatureConfig:
    """Configuration for the v1.0 structured protocol logging feature."""

    stdout: bool
    file: LoggingFileConfig
    capture: LoggingCaptureConfig
    redaction: LoggingRedactionConfig


@dataclass(frozen=True)
class ProxyConfig:
    """Validated startup configuration for nicheLLM Proxy."""

    listener: ListenerConfig
    upstream: UpstreamConfig
    timeouts: TimeoutConfig

    @property
    def logging(self) -> LoggingFeatureConfig | None:
        """Return the configured logging feature, if enabled for this listener."""

        return self.listener.features[0] if self.listener.features else None


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
        features=_features(listener_data),
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


def _features(listener: Mapping[str, Any]) -> tuple[LoggingFeatureConfig, ...]:
    """Validate listener features supported by this release."""

    value = listener.get("features", [])
    if not isinstance(value, list):
        raise ConfigError(translate("listener.features must be an array."))

    features: list[LoggingFeatureConfig] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError(translate("Each listener.features item must be an object."))
        _reject_unknown_keys(item, {"name", "config"}, "listener.features item")
        if item.get("name") != "logging":
            raise ConfigError(translate("listener.features supports only 'logging'."))
        if features:
            raise ConfigError(
                translate("listener.features must not contain duplicate 'logging'.")
            )
        config = item.get("config")
        if not isinstance(config, dict):
            raise ConfigError(translate("logging feature config must be an object."))
        features.append(_logging_feature(config))
    return tuple(features)


def _logging_feature(config: Mapping[str, Any]) -> LoggingFeatureConfig:
    """Validate the structured protocol logging feature configuration."""

    _reject_unknown_keys(
        config,
        {"stdout", "file", "capture", "redaction"},
        "logging config",
    )
    stdout = _boolean(config, "stdout", True)
    file_config = _logging_file(_optional_object(config, "file"))
    capture = _logging_capture(_optional_object(config, "capture"))
    redaction = _logging_redaction(_optional_object(config, "redaction"))
    if not stdout and not file_config.enabled:
        raise ConfigError(translate("logging must enable stdout or file output."))
    return LoggingFeatureConfig(
        stdout=stdout,
        file=file_config,
        capture=capture,
        redaction=redaction,
    )


def _logging_file(config: Mapping[str, Any]) -> LoggingFileConfig:
    """Validate optional rotating-file output settings."""

    _reject_unknown_keys(
        config,
        {"enabled", "path", "max_bytes", "backup_count"},
        "logging.file",
    )
    enabled = _boolean(config, "enabled", False)
    if not enabled:
        return LoggingFileConfig(enabled=False)

    raw_path = _non_empty_string(config, "path")
    path = Path(raw_path)
    if not path.is_absolute():
        raise ConfigError(translate("logging.file.path must be an absolute path."))
    return LoggingFileConfig(
        enabled=True,
        path=path,
        max_bytes=_positive_integer(config, "max_bytes", DEFAULT_LOG_MAX_BYTES),
        backup_count=_positive_integer(config, "backup_count", DEFAULT_LOG_BACKUP_COUNT),
    )


def _logging_capture(config: Mapping[str, Any]) -> LoggingCaptureConfig:
    """Validate bounded body-capture settings."""

    _reject_unknown_keys(config, {"bodies", "max_body_bytes"}, "logging.capture")
    bodies = _boolean(config, "bodies", False)
    max_body_bytes = _positive_integer(
        config,
        "max_body_bytes",
        DEFAULT_CAPTURE_MAX_BODY_BYTES,
    )
    if max_body_bytes > MAX_CAPTURE_BODY_BYTES:
        raise ConfigError(
            translate(
                "logging.capture.max_body_bytes must not exceed {maximum}.",
                maximum=MAX_CAPTURE_BODY_BYTES,
            )
        )
    return LoggingCaptureConfig(bodies=bodies, max_body_bytes=max_body_bytes)


def _logging_redaction(config: Mapping[str, Any]) -> LoggingRedactionConfig:
    """Validate additional redaction-name lists without accepting secret values."""

    _reject_unknown_keys(
        config,
        {
            "additional_header_names",
            "additional_query_parameter_names",
            "additional_json_field_names",
        },
        "logging.redaction",
    )
    return LoggingRedactionConfig(
        additional_header_names=_name_set(config, "additional_header_names"),
        additional_query_parameter_names=_name_set(
            config,
            "additional_query_parameter_names",
        ),
        additional_json_field_names=_name_set(config, "additional_json_field_names"),
    )


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


def _boolean(config: Mapping[str, Any], key: str, default: bool) -> bool:
    """Return an optional boolean setting without accepting integer equivalents."""

    if key not in config:
        return default
    value = config[key]
    if not isinstance(value, bool):
        raise ConfigError(
            translate("Configuration item '{key}' must be a boolean.", key=key)
        )
    return value


def _positive_integer(config: Mapping[str, Any], key: str, default: int) -> int:
    """Return an optional positive integer setting."""

    if key not in config:
        return default
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(
            translate(
                "Configuration item '{key}' must be a positive integer.",
                key=key,
            )
        )
    return value


def _name_set(config: Mapping[str, Any], key: str) -> frozenset[str]:
    """Return lower-case non-empty names from an optional configuration list."""

    if key not in config:
        return frozenset()
    value = config[key]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ConfigError(
            translate("Configuration item '{key}' must be an array of names.", key=key)
        )
    return frozenset(item.strip().lower().replace("-", "_") for item in value)


def _reject_unknown_keys(
    config: Mapping[str, Any],
    allowed_keys: set[str],
    context: str,
) -> None:
    """Reject misspelled feature settings instead of silently weakening logging."""

    unknown_keys = set(config) - allowed_keys
    if unknown_keys:
        unknown = ", ".join(sorted(unknown_keys))
        raise ConfigError(
            translate(
                "Configuration item '{context}' has unknown fields: {fields}.",
                context=context,
                fields=unknown,
            )
        )


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
