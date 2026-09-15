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

DEFAULT_CONFIG_PATHS = (
    Path("/app/config/config.jsonc"),
    Path("/app/config/config.json"),
)
"""Default configuration file candidates in resolution order."""

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
"""Default upstream connection timeout in seconds."""

DEFAULT_READ_TIMEOUT_SECONDS = 120.0
"""Default upstream read timeout in seconds."""

DEFAULT_MAX_QUEUE_WAIT_SECONDS = 60.0
"""Default featherless queue wait limit in seconds."""

DEFAULT_FEATHERLESS_CACHE_TTL_SECONDS = 300.0
"""Default featherless model info cache lifetime in seconds."""

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
    grok_image: GrokImageConfig | None = None
    gemini_image: GeminiImageConfig | None = None
    featherless: FeatherlessConfig | None = None
    features: tuple[LoggingFeatureConfig, ...] = ()


@dataclass(frozen=True)
class FeatherlessConfig:
    """Mandatory featherless mode settings for model whitelisting and queueing."""

    model_whitelist: tuple[str, ...]
    concurrency_limit: int | None = None
    max_queue_wait_seconds: float = DEFAULT_MAX_QUEUE_WAIT_SECONDS
    cache_ttl_seconds: float = DEFAULT_FEATHERLESS_CACHE_TTL_SECONDS


@dataclass(frozen=True)
class GrokImageConfig:
    """Optional grok-image mode settings for image generation."""

    default_model: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None


@dataclass(frozen=True)
class GeminiImageConfig:
    """Optional gemini-image mode settings for image generation."""

    default_model: str | None = None
    aspect_ratio: str | None = None


@dataclass(frozen=True)
class TimeoutConfig:
    """Timeout configuration for upstream communication.

    ``read_seconds`` may be ``None`` to wait for upstream responses without a
    read timeout. ``connect_seconds`` must always be a positive number.
    """

    connect_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_seconds: float | None = DEFAULT_READ_TIMEOUT_SECONDS


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
class ListenerRuntimeConfig:
    """Validated startup configuration for a single listener."""

    listener: ListenerConfig
    upstream: UpstreamConfig
    timeouts: TimeoutConfig
    warnings: tuple[str, ...] = ()

    @property
    def logging(self) -> LoggingFeatureConfig | None:
        """Return the configured logging feature, if enabled for this listener."""

        return self.listener.features[0] if self.listener.features else None


@dataclass(frozen=True)
class ProxyConfig:
    """Validated startup configuration for every listener of the proxy."""

    listeners: tuple[ListenerRuntimeConfig, ...]
    warnings: tuple[str, ...] = ()


def get_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the configuration file path after applying an environment override.

    Args:
        environ: Environment variables to read. Uses the process environment by default.

    Returns:
        `NICHELLM_CONFIG_PATH` or the first existing default configuration file.
        When no default file exists, the preferred JSONC default path is returned
        so error messages name it.
    """

    environment = os.environ if environ is None else environ
    configured_path = environment.get("NICHELLM_CONFIG_PATH")
    if configured_path:
        return Path(configured_path)
    for default_path in DEFAULT_CONFIG_PATHS:
        if default_path.is_file():
            return default_path
    return DEFAULT_CONFIG_PATHS[0]


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
        Validated configuration with one runtime entry per configured listener.
    """

    environment = os.environ if environ is None else environ
    path = Path(config_path) if config_path is not None else get_config_path(environment)
    raw_config = _read_json_object(path)

    if "listeners" not in raw_config:
        raise ConfigError(
            translate(
                "Since v1.4 the configuration requires a top-level 'listeners' "
                "array. See the README for the migration guide."
            )
        )

    listeners_data = raw_config["listeners"]
    if not isinstance(listeners_data, list) or not listeners_data:
        raise ConfigError(
            translate("'listeners' must be a non-empty array of listener objects.")
        )
    if any(not isinstance(item, dict) for item in listeners_data):
        raise ConfigError(translate("Each 'listeners' item must be an object."))

    listeners = tuple(_listener_runtime(item, environment) for item in listeners_data)

    ports = [runtime.listener.port for runtime in listeners]
    duplicate_ports = sorted({port for port in ports if ports.count(port) > 1})
    if duplicate_ports:
        raise ConfigError(
            translate(
                "'listeners' contains duplicate ports: {ports}.",
                ports=", ".join(str(port) for port in duplicate_ports),
            )
        )

    global_warnings: list[str] = []
    log_file_paths = [
        str(runtime.logging.file.path)
        for runtime in listeners
        if runtime.logging is not None and runtime.logging.file.enabled
    ]
    duplicate_log_paths = sorted(
        {log_path for log_path in log_file_paths if log_file_paths.count(log_path) > 1}
    )
    if duplicate_log_paths:
        global_warnings.append(
            translate(
                "Multiple listeners write protocol logs to the same file: {paths}.",
                paths=", ".join(duplicate_log_paths),
            )
        )

    unknown_top_level_keys = set(raw_config) - {"listeners"}
    if unknown_top_level_keys:
        global_warnings.append(
            translate(
                "Unknown top-level configuration keys were ignored: {keys}.",
                keys=", ".join(sorted(unknown_top_level_keys)),
            )
        )

    return ProxyConfig(
        listeners=listeners,
        warnings=tuple(global_warnings),
    )


def _listener_runtime(
    listener_data: Mapping[str, Any],
    environment: Mapping[str, str],
) -> ListenerRuntimeConfig:
    """Validate one listener entry and resolve its upstream key."""

    warnings: list[str] = []
    port = _port(listener_data)
    mode = _mode(listener_data)
    listener = ListenerConfig(
        port=port,
        mode=mode,
        grok_image=_grok_image(listener_data, mode, warnings),
        gemini_image=_gemini_image(listener_data, mode, warnings),
        featherless=_featherless(listener_data, mode, warnings),
        features=_features(listener_data, warnings),
    )
    upstream_data = _required_object(listener_data, "upstream")
    if mode == "featherless":
        # The featherless mode relays the client's own Authorization header and
        # must not hold an upstream key.
        if upstream_data.get("api_key_env") is not None:
            raise ConfigError(
                translate(
                    "upstream.api_key_env is not used in 'featherless' mode. "
                    "The proxy relays the client's Authorization header."
                )
            )
        upstream = UpstreamConfig(
            base_url=_base_url(upstream_data),
            api_key_env="",
            api_key="",
        )
    else:
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
    timeout_data = _optional_object(listener_data, "timeouts")
    timeouts = TimeoutConfig(
        connect_seconds=_positive_number(
            timeout_data,
            "connect_seconds",
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
        ),
        read_seconds=_positive_number_or_none(
            timeout_data,
            "read_seconds",
            DEFAULT_READ_TIMEOUT_SECONDS,
        ),
    )
    return ListenerRuntimeConfig(
        listener=listener,
        upstream=upstream,
        timeouts=timeouts,
        warnings=tuple(warnings),
    )


def _read_json_object(path: Path) -> Mapping[str, Any]:
    """Read a JSONC configuration file and return its top-level JSON object."""

    try:
        with path.open(encoding="utf-8") as config_file:
            value = json.loads(_strip_jsonc_comments(config_file.read()))
    except FileNotFoundError as error:
        raise ConfigError(
            translate("Configuration file was not found: {path}", path=path)
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(translate("Unable to read the configuration JSON.")) from error

    if not isinstance(value, dict):
        raise ConfigError(translate("Top-level configuration JSON must be an object."))
    return value


def _strip_jsonc_comments(text: str) -> str:
    """Remove // and /* */ comments outside JSON string literals.

    Newlines are preserved and other comment characters become spaces so that
    JSON error positions keep pointing at the original file.
    """

    output: list[str] = []
    state = "normal"
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if state == "normal":
            if character == '"':
                state = "string"
                output.append(character)
                index += 1
            elif text[index : index + 2] == "//":
                state = "line_comment"
                index += 2
            elif text[index : index + 2] == "/*":
                state = "block_comment"
                index += 2
            else:
                output.append(character)
                index += 1
        elif state == "string":
            if character == "\\":
                # Copy escape sequences verbatim so \" and \\ never end a string.
                if index + 1 < length:
                    output.append(character)
                    output.append(text[index + 1])
                    index += 2
                else:
                    output.append(character)
                    index += 1
            else:
                if character == '"':
                    state = "normal"
                output.append(character)
                index += 1
        elif state == "line_comment":
            if character == "\n":
                state = "normal"
                output.append(character)
            index += 1
        else:
            if text[index : index + 2] == "*/":
                state = "normal"
                output.append(" ")
                index += 2
            else:
                if character == "\n":
                    output.append(character)
                index += 1
    return "".join(output)


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


def _mode(listener: Mapping[str, Any]) -> str:
    """Validate and return the listener mode supported by this release."""

    mode = listener.get("mode")
    if not isinstance(mode, str) or mode not in {
        "passthrough",
        "grok-image",
        "gemini-image",
        "featherless",
    }:
        raise ConfigError(
            translate(
                "listener.mode must be 'passthrough', 'grok-image', "
                "'gemini-image' or 'featherless'."
            )
        )
    return mode


def _grok_image(
    listener: Mapping[str, Any],
    mode: str,
    warnings: list[str],
) -> GrokImageConfig | None:
    """Validate the optional grok_image settings for the grok-image listener."""

    if "grok_image" not in listener:
        return None
    if mode != "grok-image":
        warnings.append(
            translate(
                "listener.grok_image is ignored because "
                "listener.mode is not 'grok-image'."
            )
        )
        return None
    value = _required_object(listener, "grok_image")
    _reject_unknown_keys(
        value,
        {"default_model", "aspect_ratio", "resolution"},
        "listener.grok_image",
    )
    return GrokImageConfig(
        default_model=_grok_image_string(value, "default_model"),
        aspect_ratio=_grok_image_string(value, "aspect_ratio"),
        resolution=_grok_image_resolution(value),
    )


def _grok_image_string(config: Mapping[str, Any], key: str) -> str | None:
    """Validate an optional non-empty grok_image string setting."""

    if key not in config or config[key] is None:
        return None
    value = config[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            translate(
                "listener.grok_image.{key} must be a non-empty string.",
                key=key,
            )
        )
    return value


def _grok_image_resolution(config: Mapping[str, Any]) -> str | None:
    """Validate the optional grok_image resolution setting."""

    key = "resolution"
    if key not in config or config[key] is None:
        return None
    value = config[key]
    if not isinstance(value, str) or value not in {"1k", "2k"}:
        raise ConfigError(
            translate("listener.grok_image.resolution must be '1k' or '2k'.")
        )
    return value


def _gemini_image(
    listener: Mapping[str, Any],
    mode: str,
    warnings: list[str],
) -> GeminiImageConfig | None:
    """Validate the optional gemini_image settings for the gemini-image listener."""

    if "gemini_image" not in listener:
        return None
    if mode != "gemini-image":
        warnings.append(
            translate(
                "listener.gemini_image is ignored because "
                "listener.mode is not 'gemini-image'."
            )
        )
        return None
    value = _required_object(listener, "gemini_image")
    _reject_unknown_keys(
        value,
        {"default_model", "aspect_ratio"},
        "listener.gemini_image",
    )
    return GeminiImageConfig(
        default_model=_gemini_image_string(value, "default_model"),
        aspect_ratio=_gemini_image_string(value, "aspect_ratio"),
    )


def _gemini_image_string(config: Mapping[str, Any], key: str) -> str | None:
    """Validate an optional non-empty gemini_image string setting."""

    if key not in config or config[key] is None:
        return None
    value = config[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            translate(
                "listener.gemini_image.{key} must be a non-empty string.",
                key=key,
            )
        )
    return value


def _featherless(
    listener: Mapping[str, Any],
    mode: str,
    warnings: list[str],
) -> FeatherlessConfig | None:
    """Validate the mandatory featherless settings for the featherless listener."""

    if "featherless" not in listener:
        return None
    if mode != "featherless":
        warnings.append(
            translate(
                "listener.featherless is ignored because "
                "listener.mode is not 'featherless'."
            )
        )
        return None
    value = _required_object(listener, "featherless")
    _reject_unknown_keys(
        value,
        {
            "model_whitelist",
            "concurrency_limit",
            "max_queue_wait_seconds",
            "cache_ttl_seconds",
        },
        "listener.featherless",
    )
    return FeatherlessConfig(
        model_whitelist=_model_whitelist(value),
        concurrency_limit=_featherless_integer(value, "concurrency_limit"),
        max_queue_wait_seconds=_featherless_number(
            value,
            "max_queue_wait_seconds",
            DEFAULT_MAX_QUEUE_WAIT_SECONDS,
        ),
        cache_ttl_seconds=_featherless_number(
            value,
            "cache_ttl_seconds",
            DEFAULT_FEATHERLESS_CACHE_TTL_SECONDS,
        ),
    )


def _model_whitelist(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate the mandatory non-empty whitelist without duplicate model ids."""

    if "model_whitelist" not in config or config["model_whitelist"] is None:
        raise ConfigError(
            translate("listener.featherless.model_whitelist is required.")
        )
    value = config["model_whitelist"]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ConfigError(
            translate(
                "listener.featherless.model_whitelist must be an array of model id strings."
            )
        )
    if not value:
        raise ConfigError(
            translate(
                "listener.featherless.model_whitelist must contain at least one model id."
            )
        )
    if len(set(value)) != len(value):
        raise ConfigError(
            translate(
                "listener.featherless.model_whitelist must not contain duplicate model ids."
            )
        )
    return tuple(value)


def _featherless_integer(
    config: Mapping[str, Any],
    key: str,
) -> int | None:
    """Validate an optional positive integer featherless setting."""

    if key not in config or config[key] is None:
        return None
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(
            translate(
                "listener.featherless.{key} must be a positive integer.",
                key=key,
            )
        )
    return value


def _featherless_number(
    config: Mapping[str, Any],
    key: str,
    default: float,
) -> float:
    """Validate an optional positive numeric featherless setting."""

    if key not in config or config[key] is None:
        return default
    value = config[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigError(
            translate(
                "listener.featherless.{key} must be a positive number.",
                key=key,
            )
        )
    return float(value)


def _features(
    listener: Mapping[str, Any],
    warnings: list[str],
) -> tuple[LoggingFeatureConfig, ...]:
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
        features.append(_logging_feature(config, warnings))
    return tuple(features)


def _logging_feature(
    config: Mapping[str, Any],
    warnings: list[str],
) -> LoggingFeatureConfig:
    """Validate the structured protocol logging feature configuration."""

    _reject_unknown_keys(
        config,
        {"stdout", "file", "capture", "redaction"},
        "logging config",
    )
    stdout = _boolean(config, "stdout", True)
    file_config = _logging_file(_optional_object(config, "file"), warnings)
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


def _logging_file(
    config: Mapping[str, Any],
    warnings: list[str],
) -> LoggingFileConfig:
    """Validate optional rotating-file output settings."""

    _reject_unknown_keys(
        config,
        {"enabled", "path", "max_bytes", "backup_count"},
        "logging.file",
    )
    enabled = _boolean(config, "enabled", False)
    if not enabled:
        if any(key in config for key in ("path", "max_bytes", "backup_count")):
            warnings.append(
                translate(
                    "logging.file settings were ignored because "
                    "logging.file.enabled is false."
                )
            )
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
        parsed.port  # noqa: B018 - intentional attribute access for validation
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


def _positive_number_or_none(
    config: Mapping[str, Any],
    key: str,
    default: float,
) -> float | None:
    """Validate and return an optional positive numeric setting that may be null.

    A null value disables the timeout instead of falling back to the default.
    """

    if key in config and config[key] is None:
        return None
    return _positive_number(config, key, default)
