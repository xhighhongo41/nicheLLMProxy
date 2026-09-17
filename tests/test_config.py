"""Tests for configuration and secret validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from niche_llm_proxy import config as config_module
from niche_llm_proxy.config import (
    ConfigError,
    _strip_jsonc_comments,
    get_config_path,
    load_config,
)


def test_load_config_reads_valid_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Create a configuration object from valid settings and environment variables."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(write_config())

    assert len(config.listeners) == 1
    runtime = config.listeners[0]
    assert runtime.listener.port == 8000
    assert runtime.listener.mode == "passthrough"
    assert runtime.upstream.base_url == "https://upstream.example.test"
    assert runtime.upstream.api_key == "secret-value"
    assert "secret-value" not in repr(runtime)
    assert "secret-value" not in repr(config)


@pytest.mark.parametrize(
    ("listener_overrides", "message"),
    [
        ({"port": 0}, "port"),
        ({"mode": "unknown"}, "mode"),
        ({"mode": ["grok-image"]}, "mode"),
        (
            {
                "upstream": {
                    "base_url": "not-a-url",
                    "api_key_env": "UPSTREAM_API_KEY",
                }
            },
            "base_url",
        ),
    ],
)
def test_load_config_rejects_invalid_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    listener_overrides: dict[str, object],
    message: str,
) -> None:
    """Reject invalid required settings before startup."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")

    with pytest.raises(ConfigError, match=message):
        load_config(write_config({"listeners": [make_listener(listener_overrides)]}))


def test_load_config_does_not_expose_missing_key_value(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Do not expose a secret value in an error about a missing key."""
    listener = make_listener(
        {
            "upstream": {
                "base_url": "https://upstream.example.test",
                "api_key_env": "MISSING_UPSTREAM_KEY",
            }
        }
    )

    with pytest.raises(ConfigError) as error:
        load_config(write_config({"listeners": [listener]}), environ={})

    assert "MISSING_UPSTREAM_KEY" in str(error.value)
    assert "secret" not in str(error.value).lower()


def test_get_config_path_honors_environment_override() -> None:
    """Allow the dedicated environment variable to override the config path."""
    assert get_config_path({"NICHELLM_CONFIG_PATH": "/tmp/custom.json"}) == Path(
        "/tmp/custom.json"
    )


def test_get_config_path_prefers_existing_jsonc_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prefer config.jsonc when both default configuration files exist."""
    monkeypatch.setattr(
        config_module,
        "DEFAULT_CONFIG_PATHS",
        (tmp_path / "config.jsonc", tmp_path / "config.json"),
    )
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config.jsonc").write_text("{}", encoding="utf-8")

    assert get_config_path({}) == tmp_path / "config.jsonc"


def test_get_config_path_falls_back_to_json_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fall back to config.json when only that default file exists."""
    monkeypatch.setattr(
        config_module,
        "DEFAULT_CONFIG_PATHS",
        (tmp_path / "config.jsonc", tmp_path / "config.json"),
    )
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    assert get_config_path({}) == tmp_path / "config.json"


def test_get_config_path_returns_jsonc_when_no_default_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Report the config.jsonc path when neither default file exists."""
    monkeypatch.setattr(
        config_module,
        "DEFAULT_CONFIG_PATHS",
        (tmp_path / "config.jsonc", tmp_path / "config.json"),
    )

    assert get_config_path({}) == tmp_path / "config.jsonc"


def test_get_config_path_environment_override_beats_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Honor the explicit environment path even when default files exist."""
    monkeypatch.setattr(
        config_module,
        "DEFAULT_CONFIG_PATHS",
        (tmp_path / "config.jsonc", tmp_path / "config.json"),
    )
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    assert get_config_path({"NICHELLM_CONFIG_PATH": "/tmp/custom.json"}) == Path(
        "/tmp/custom.json"
    )


def test_load_config_accepts_logging_feature(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    tmp_path: Path,
) -> None:
    """Load bounded stdout and rotating-file logging without storing a secret."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    logging_config = {
        "stdout": False,
        "file": {
            "enabled": True,
            "path": str(tmp_path / "proxy.jsonl"),
            "max_bytes": 100,
            "backup_count": 2,
        },
        "capture": {"bodies": True, "max_body_bytes": 100},
        "redaction": {"additional_header_names": ["X-Customer-Secret"]},
    }
    listener = make_listener({"features": [{"name": "logging", "config": logging_config}]})
    config = load_config(write_config({"listeners": [listener]}))
    logging = config.listeners[0].logging

    assert logging is not None
    assert logging.file.path == tmp_path / "proxy.jsonl"
    assert logging.capture.bodies
    assert logging.redaction.additional_header_names == {"x_customer_secret"}


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
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    features: object,
    message: str,
) -> None:
    """Reject ambiguous logging settings before the service starts."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")

    with pytest.raises(ConfigError, match=message):
        load_config(write_config({"listeners": [make_listener({"features": features})]}))


@pytest.mark.parametrize(
    ("mode", "section"),
    [
        ("grok-image", "grok_image"),
        ("grok-image-edit", "grok_image_edit"),
        ("gemini-image", "gemini_image"),
        ("featherless", "featherless"),
    ],
)
def test_load_config_skips_listener_when_mode_section_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    mode: str,
    section: str,
) -> None:
    """Skip a listener before other validation when its mode section is absent."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(
        write_config(
            {
                "listeners": [
                    make_listener(),
                    make_listener({"port": 8001, "mode": mode}),
                ]
            }
        )
    )

    expected_warning = (
        f"Skipped the listener on port 8001 because mode '{mode}' requires a "
        f"'{section}' section, which is missing from the configuration."
    )
    assert [runtime.listener.port for runtime in config.listeners] == [8000]
    assert config.warnings == (expected_warning,)


@pytest.mark.parametrize(
    "mode", ["grok-image", "grok-image-edit", "gemini-image", "featherless"]
)
def test_load_config_rejects_config_with_only_skipped_listeners(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    mode: str,
) -> None:
    """Reject a configuration whose listeners are all skipped."""
    with pytest.raises(ConfigError) as error:
        load_config(
            write_config(
                {
                    "listeners": [
                        make_listener({"mode": mode}),
                        make_listener({"port": 8001, "mode": mode}),
                    ]
                }
            )
        )

    assert "No listeners can be started" in str(error.value)
    assert "8000" in str(error.value)
    assert "8001" in str(error.value)


def test_load_config_keeps_remaining_listeners_when_others_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Keep startable listeners and warn once per skipped listener."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(
        write_config(
            {
                "listeners": [
                    make_listener(),
                    make_listener({"port": 8001, "mode": "grok-image"}),
                    make_listener(
                        {
                            "port": 8002,
                            "mode": "featherless",
                            "upstream": {"base_url": "https://api.featherless.ai"},
                        }
                    ),
                ]
            }
        )
    )

    assert [runtime.listener.port for runtime in config.listeners] == [8000]
    assert [runtime.listener.mode for runtime in config.listeners] == ["passthrough"]
    assert len(config.warnings) == 2
    assert "8001" in config.warnings[0]
    assert "grok-image" in config.warnings[0]
    assert "8002" in config.warnings[1]
    assert "featherless" in config.warnings[1]
    assert config.listeners[0].warnings == ()


def test_load_config_allows_kept_listener_to_reuse_skipped_listener_port(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Duplicate ports only count listeners that actually start."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(
        write_config(
            {
                "listeners": [
                    make_listener({"mode": "grok-image"}),
                    make_listener(),
                ]
            }
        )
    )

    assert [runtime.listener.port for runtime in config.listeners] == [8000]
    assert len(config.warnings) == 1


def test_load_config_reads_grok_image_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Read explicit grok-image settings into the configuration object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {
            "mode": "grok-image",
            "grok_image": {
                "default_model": "grok-imagine-image-2.0",
                "aspect_ratio": "1:1",
                "resolution": "1k",
            },
        }
    )
    config = load_config(write_config({"listeners": [listener]}))
    grok_image = config.listeners[0].listener.grok_image

    assert grok_image is not None
    assert grok_image.default_model == "grok-imagine-image-2.0"
    assert grok_image.aspect_ratio == "1:1"
    assert grok_image.resolution == "1k"


def test_load_config_warns_on_grok_image_in_passthrough_mode(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Warn when grok-image settings appear in another listener mode."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener({"grok_image": {"resolution": "1k"}})
    config = load_config(write_config({"listeners": [listener]}))

    expected_warning = (
        "listener.grok_image is ignored because listener.mode is not 'grok-image'."
    )
    assert config.listeners[0].listener.grok_image is None
    assert config.listeners[0].warnings == (expected_warning,)


@pytest.mark.parametrize("grok_image", ["x", ["x"], 123, True, None])
def test_load_config_rejects_non_object_grok_image(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    grok_image: object,
) -> None:
    """Reject grok-image settings that are not JSON objects."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")

    with pytest.raises(ConfigError, match="must be an object"):
        load_config(
            write_config({"listeners": [make_listener({"mode": "grok-image", "grok_image": grok_image})]})
        )


def test_load_config_rejects_unknown_grok_image_field(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Reject grok-image settings with an unknown field."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {"mode": "grok-image", "grok_image": {"size": "1k"}}
    )

    with pytest.raises(ConfigError, match="unknown"):
        load_config(write_config({"listeners": [listener]}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_model", ""),
        ("default_model", 123),
        ("aspect_ratio", ""),
        ("aspect_ratio", 123),
    ],
)
def test_load_config_rejects_invalid_grok_image_string_field(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    field: str,
    value: object,
) -> None:
    """Reject non-string and empty grok-image string settings."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {"mode": "grok-image", "grok_image": {field: value}}
    )

    with pytest.raises(ConfigError, match="non-empty string"):
        load_config(write_config({"listeners": [listener]}))


def test_load_config_accepts_null_grok_image_fields(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Accept explicit null values for every optional grok-image setting."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {
            "mode": "grok-image",
            "grok_image": {
                "default_model": None,
                "aspect_ratio": None,
                "resolution": None,
            },
        }
    )
    config = load_config(write_config({"listeners": [listener]}))
    grok_image = config.listeners[0].listener.grok_image

    assert grok_image is not None
    assert grok_image.default_model is None
    assert grok_image.aspect_ratio is None
    assert grok_image.resolution is None


@pytest.mark.parametrize(("resolution", "should_load"), [("1k", True), ("2k", True), ("3k", False), ("", False), (123, False), (["1k"], False)])
def test_load_config_validates_grok_image_resolution(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    resolution: object,
    should_load: bool,
) -> None:
    """Only allow the documented grok-image resolutions."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {"mode": "grok-image", "grok_image": {"resolution": resolution}}
    )
    config_path = write_config({"listeners": [listener]})

    if should_load:
        config = load_config(config_path)
        assert config.listeners[0].listener.grok_image is not None
        assert config.listeners[0].listener.grok_image.resolution == resolution
    else:
        with pytest.raises(ConfigError, match="resolution"):
            load_config(config_path)


def test_load_config_reads_gemini_image_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Read explicit gemini-image settings into the configuration object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {
            "mode": "gemini-image",
            "gemini_image": {
                "default_model": "gemini-3-pro-image-preview",
                "aspect_ratio": "1:1",
            },
        }
    )
    config = load_config(write_config({"listeners": [listener]}))
    gemini_image = config.listeners[0].listener.gemini_image

    assert gemini_image is not None
    assert gemini_image.default_model == "gemini-3-pro-image-preview"
    assert gemini_image.aspect_ratio == "1:1"


@pytest.mark.parametrize("mode", ["passthrough", "grok-image"])
def test_load_config_warns_on_gemini_image_in_other_modes(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    mode: str,
) -> None:
    """Warn when gemini-image settings appear in another listener mode."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener_overrides: dict[str, object] = {
        "mode": mode,
        "gemini_image": {"aspect_ratio": "1:1"},
    }
    if mode == "grok-image":
        listener_overrides["grok_image"] = {}
    listener = make_listener(listener_overrides)
    config = load_config(write_config({"listeners": [listener]}))

    expected_warning = (
        "listener.gemini_image is ignored because listener.mode is not 'gemini-image'."
    )
    assert config.listeners[0].listener.gemini_image is None
    assert config.listeners[0].warnings == (expected_warning,)


@pytest.mark.parametrize("gemini_image", ["x", ["x"], 123, True, None])
def test_load_config_rejects_non_object_gemini_image(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    gemini_image: object,
) -> None:
    """Reject gemini-image settings that are not JSON objects."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")

    with pytest.raises(ConfigError, match="must be an object"):
        load_config(
            write_config(
                {"listeners": [make_listener({"mode": "gemini-image", "gemini_image": gemini_image})]}
            )
        )


def test_load_config_rejects_unknown_gemini_image_field(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Reject gemini-image settings with an unknown field."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {"mode": "gemini-image", "gemini_image": {"resolution": "1k"}}
    )

    with pytest.raises(ConfigError, match="unknown"):
        load_config(write_config({"listeners": [listener]}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_model", ""),
        ("default_model", 123),
        ("aspect_ratio", ""),
        ("aspect_ratio", 123),
    ],
)
def test_load_config_rejects_invalid_gemini_image_string_field(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    field: str,
    value: object,
) -> None:
    """Reject non-string and empty gemini-image string settings."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {"mode": "gemini-image", "gemini_image": {field: value}}
    )

    with pytest.raises(ConfigError, match="non-empty string"):
        load_config(write_config({"listeners": [listener]}))


def test_load_config_accepts_null_gemini_image_fields(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Accept explicit null values for every optional gemini-image setting."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {
            "mode": "gemini-image",
            "gemini_image": {"default_model": None, "aspect_ratio": None},
        }
    )
    config = load_config(write_config({"listeners": [listener]}))
    gemini_image = config.listeners[0].listener.gemini_image

    assert gemini_image is not None
    assert gemini_image.default_model is None
    assert gemini_image.aspect_ratio is None


def test_load_config_reports_supported_modes_in_mode_error(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Report every supported mode in the listener mode validation error."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config_path = write_config({"listeners": [make_listener({"mode": "unknown"})]})

    with pytest.raises(
        ConfigError,
        match="must be 'passthrough', 'grok-image', 'grok-image-edit', 'gemini-image' or 'featherless'",
    ):
        load_config(config_path)


def test_load_config_accepts_featherless_mode(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Accept featherless mode without an upstream API key environment variable."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {"model_whitelist": ["moonshotai/Kimi-K2.6"]},
            "upstream": {"base_url": "https://api.featherless.ai"},
        }
    )
    config = load_config(write_config({"listeners": [listener]}))

    runtime = config.listeners[0]
    assert runtime.listener.mode == "featherless"
    featherless = runtime.listener.featherless
    assert featherless is not None
    assert featherless.model_whitelist == ("moonshotai/Kimi-K2.6",)
    assert runtime.upstream.api_key == ""


def test_load_config_reads_featherless_settings(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Read explicit featherless settings into the configuration object."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {
                "model_whitelist": [
                    "moonshotai/Kimi-K2.6",
                    "Qwen/Qwen2.5-7B-Instruct",
                ],
                "concurrency_limit": 8,
                "max_queue_wait_seconds": 30.0,
                "cache_ttl_seconds": 600.0,
            },
            "upstream": {"base_url": "https://api.featherless.ai"},
        }
    )
    config = load_config(write_config({"listeners": [listener]}))
    featherless = config.listeners[0].listener.featherless

    assert featherless is not None
    assert featherless.model_whitelist == (
        "moonshotai/Kimi-K2.6",
        "Qwen/Qwen2.5-7B-Instruct",
    )
    assert featherless.concurrency_limit == 8
    assert featherless.max_queue_wait_seconds == 30.0
    assert featherless.cache_ttl_seconds == 600.0


def test_load_config_applies_featherless_defaults(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Apply documented featherless defaults when optional settings are absent."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {"model_whitelist": ["moonshotai/Kimi-K2.6"]},
            "upstream": {"base_url": "https://api.featherless.ai"},
        }
    )
    config = load_config(write_config({"listeners": [listener]}))
    featherless = config.listeners[0].listener.featherless

    assert featherless is not None
    assert featherless.concurrency_limit is None
    assert featherless.max_queue_wait_seconds == 60.0
    assert featherless.cache_ttl_seconds == 300.0


def test_load_config_accepts_null_featherless_options(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Accept explicit null values for every optional featherless setting."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {
                "model_whitelist": ["moonshotai/Kimi-K2.6"],
                "concurrency_limit": None,
                "max_queue_wait_seconds": None,
                "cache_ttl_seconds": None,
            },
            "upstream": {"base_url": "https://api.featherless.ai"},
        }
    )
    config = load_config(write_config({"listeners": [listener]}))
    featherless = config.listeners[0].listener.featherless

    assert featherless is not None
    assert featherless.concurrency_limit is None
    assert featherless.max_queue_wait_seconds == 60.0
    assert featherless.cache_ttl_seconds == 300.0


def test_load_config_accepts_null_api_key_env_in_featherless_mode(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Accept a null api_key_env in featherless mode."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {"model_whitelist": ["moonshotai/Kimi-K2.6"]},
            "upstream": {"base_url": "https://api.featherless.ai", "api_key_env": None},
        }
    )
    config = load_config(write_config({"listeners": [listener]}))

    assert config.listeners[0].upstream.api_key == ""


@pytest.mark.parametrize("mode", ["passthrough", "grok-image", "gemini-image"])
def test_load_config_warns_on_featherless_in_other_modes(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    mode: str,
) -> None:
    """Warn when featherless settings appear in another listener mode."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener_overrides: dict[str, object] = {
        "mode": mode,
        "featherless": {"model_whitelist": ["moonshotai/Kimi-K2.6"]},
    }
    if mode == "grok-image":
        listener_overrides["grok_image"] = {}
    if mode == "gemini-image":
        listener_overrides["gemini_image"] = {}
    listener = make_listener(listener_overrides)
    config = load_config(write_config({"listeners": [listener]}))

    expected_warning = (
        "listener.featherless is ignored because listener.mode is not 'featherless'."
    )
    assert config.listeners[0].listener.featherless is None
    assert config.listeners[0].warnings == (expected_warning,)


@pytest.mark.parametrize("featherless", ["x", ["x"], 123, True, None])
def test_load_config_rejects_non_object_featherless(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    featherless: object,
) -> None:
    """Reject featherless settings that are not JSON objects."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")

    with pytest.raises(ConfigError, match="must be an object"):
        load_config(
            write_config(
                {"listeners": [make_listener({"mode": "featherless", "featherless": featherless})]}
            )
        )


def test_load_config_rejects_unknown_featherless_field(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Reject featherless settings with an unknown field."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {
                "model_whitelist": ["moonshotai/Kimi-K2.6"],
                "models_cache_ttl_seconds": 600.0,
            },
            "upstream": {"base_url": "https://api.featherless.ai"},
        }
    )

    with pytest.raises(ConfigError, match="unknown"):
        load_config(write_config({"listeners": [listener]}))


def test_load_config_rejects_api_key_env_in_featherless_mode(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Reject upstream API key settings in featherless mode."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {"model_whitelist": ["moonshotai/Kimi-K2.6"]},
            "upstream": {
                "base_url": "https://api.featherless.ai",
                "api_key_env": "UPSTREAM_API_KEY",
            },
        }
    )

    with pytest.raises(ConfigError, match="not used in 'featherless' mode"):
        load_config(write_config({"listeners": [listener]}))


def test_load_config_rejects_featherless_missing_model_whitelist(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Require a model whitelist in featherless mode."""
    listener = make_listener(
        {"mode": "featherless", "featherless": {}, "upstream": {"base_url": "https://api.featherless.ai"}}
    )

    with pytest.raises(ConfigError, match="required"):
        load_config(write_config({"listeners": [listener]}))


def test_load_config_rejects_featherless_empty_model_whitelist(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Reject an empty model whitelist in featherless mode."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {"model_whitelist": []},
            "upstream": {"base_url": "https://api.featherless.ai"},
        }
    )

    with pytest.raises(ConfigError, match="at least one"):
        load_config(write_config({"listeners": [listener]}))


@pytest.mark.parametrize(
    "model_whitelist",
    [[""], ["   "], [123], [None], [[]], ["moonshotai/Kimi-K2.6", 123]],
)
def test_load_config_rejects_featherless_invalid_model_whitelist_items(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    model_whitelist: object,
) -> None:
    """Reject non-string and blank model whitelist entries."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {"model_whitelist": model_whitelist},
            "upstream": {"base_url": "https://api.featherless.ai"},
        }
    )

    with pytest.raises(ConfigError, match="array of model id strings"):
        load_config(write_config({"listeners": [listener]}))


def test_load_config_rejects_featherless_duplicate_model_whitelist(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Reject duplicate model whitelist entries."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {
                "model_whitelist": ["moonshotai/Kimi-K2.6", "moonshotai/Kimi-K2.6"]
            },
            "upstream": {"base_url": "https://api.featherless.ai"},
        }
    )

    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write_config({"listeners": [listener]}))


@pytest.mark.parametrize("concurrency_limit", [0, -1, 2.5, "8", True])
def test_load_config_rejects_featherless_invalid_concurrency_limit(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    concurrency_limit: object,
) -> None:
    """Reject concurrency limits that are not positive integers."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {
                "model_whitelist": ["moonshotai/Kimi-K2.6"],
                "concurrency_limit": concurrency_limit,
            },
            "upstream": {"base_url": "https://api.featherless.ai"},
        }
    )

    with pytest.raises(ConfigError, match="positive integer"):
        load_config(write_config({"listeners": [listener]}))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        *(("max_queue_wait_seconds", value) for value in (0, -1, "60", True, [], {})),
        *(("cache_ttl_seconds", value) for value in (0, -1, "60", True, [], {})),
    ],
)
def test_load_config_rejects_invalid_featherless_numbers(
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    key: str,
    value: object,
) -> None:
    """Reject featherless queue and cache settings that are not positive numbers."""
    listener = make_listener(
        {
            "mode": "featherless",
            "featherless": {
                "model_whitelist": ["moonshotai/Kimi-K2.6"],
                key: value,
            },
            "upstream": {"base_url": "https://api.featherless.ai"},
        }
    )

    with pytest.raises(ConfigError, match="positive number"):
        load_config(write_config({"listeners": [listener]}))


def test_load_config_accepts_null_read_seconds(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Allow waiting for upstream responses without a read timeout."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener({"timeouts": {"connect_seconds": 1, "read_seconds": None}})
    config = load_config(write_config({"listeners": [listener]}))

    assert config.listeners[0].timeouts.connect_seconds == 1.0
    assert config.listeners[0].timeouts.read_seconds is None


@pytest.mark.parametrize("read_seconds", [0, -1, -2.5, "120", True, [], {}])
def test_load_config_rejects_invalid_read_seconds(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    read_seconds: object,
) -> None:
    """Reject read timeouts that are not positive numbers or null."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {"timeouts": {"connect_seconds": 1, "read_seconds": read_seconds}}
    )

    with pytest.raises(ConfigError, match="read_seconds"):
        load_config(write_config({"listeners": [listener]}))


def test_load_config_rejects_null_connect_seconds(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Require a positive connect timeout."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {"timeouts": {"connect_seconds": None, "read_seconds": 2}}
    )

    with pytest.raises(ConfigError, match="connect_seconds"):
        load_config(write_config({"listeners": [listener]}))


def test_load_config_applies_timeout_defaults_per_listener(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Apply default timeouts to listeners that omit the timeouts block."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    default_listener = make_listener()
    del default_listener["timeouts"]
    tuned_listener = make_listener(
        {"port": 8001, "timeouts": {"connect_seconds": 2, "read_seconds": None}}
    )
    config = load_config(write_config({"listeners": [default_listener, tuned_listener]}))

    assert config.listeners[0].timeouts.connect_seconds == 10.0
    assert config.listeners[0].timeouts.read_seconds == 120.0
    assert config.listeners[1].timeouts.connect_seconds == 2.0
    assert config.listeners[1].timeouts.read_seconds is None


def test_load_config_accepts_multiple_listeners(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Load several listeners with distinct modes and upstream settings."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "upstream-secret")
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    first = make_listener()
    second = make_listener(
        {
            "port": 8001,
            "mode": "grok-image",
            "grok_image": {"resolution": "1k"},
            "upstream": {"base_url": "https://api.x.ai", "api_key_env": "XAI_API_KEY"},
        }
    )
    config = load_config(write_config({"listeners": [first, second]}))

    assert [runtime.listener.port for runtime in config.listeners] == [8000, 8001]
    assert [runtime.listener.mode for runtime in config.listeners] == [
        "passthrough",
        "grok-image",
    ]
    assert config.listeners[0].upstream.api_key == "upstream-secret"
    assert config.listeners[1].upstream.api_key == "xai-secret"
    assert config.warnings == ()
    assert config.listeners[0].warnings == ()
    assert config.listeners[1].warnings == ()


@pytest.mark.parametrize(
    ("ports", "duplicates"),
    [
        ([8000, 8000], "8000"),
        ([8000, 8001, 8000, 8001], "8000, 8001"),
    ],
)
def test_load_config_rejects_duplicate_ports(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    ports: list[int],
    duplicates: str,
) -> None:
    """Reject listeners configured for the same port."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listeners = [make_listener({"port": port}) for port in ports]

    with pytest.raises(ConfigError, match=f"duplicate ports: {duplicates}"):
        load_config(write_config({"listeners": listeners}))


def test_load_config_rejects_legacy_format(tmp_path: Path) -> None:
    """Reject the pre-v1.4 single-listener configuration format."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "listener": {"port": 8000, "mode": "passthrough"},
                "upstream": {
                    "base_url": "https://upstream.example.test",
                    "api_key_env": "UPSTREAM_API_KEY",
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="'listeners' array"):
        load_config(config_path)


def test_load_config_rejects_missing_listeners(tmp_path: Path) -> None:
    """Reject a configuration without a 'listeners' key."""
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigError, match="'listeners' array"):
        load_config(config_path)


@pytest.mark.parametrize("listeners", [{}, "listeners", 123, True, []])
def test_load_config_rejects_non_array_listeners(
    tmp_path: Path, listeners: object
) -> None:
    """Reject 'listeners' values that are not non-empty arrays."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"listeners": listeners}), encoding="utf-8")

    with pytest.raises(ConfigError, match="'listeners' must be a non-empty array"):
        load_config(config_path)


@pytest.mark.parametrize("listener_item", ["x", ["x"], 123, True])
def test_load_config_rejects_non_object_listener_item(
    tmp_path: Path, listener_item: object
) -> None:
    """Reject listener entries that are not JSON objects."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"listeners": [listener_item]}), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="Each 'listeners' item must be an object"):
        load_config(config_path)


def test_load_config_reads_jsonc_with_comments(
    monkeypatch: pytest.MonkeyPatch,
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    tmp_path: Path,
) -> None:
    """Load a commented JSONC configuration file."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config_path = tmp_path / "config.jsonc"
    config_path.write_text(
        "// nicheLLM Proxy configuration\n"
        "{\n"
        '  "listeners": [ /* one listener */\n'
        "    "
        + json.dumps(make_listener())
        + "\n"
        "  ]\n"
        "} /* trailing block comment */\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert config.listeners[0].listener.port == 8000
    assert config.listeners[0].upstream.api_key == "secret-value"


def test_load_config_reads_comments_in_json_extension(
    monkeypatch: pytest.MonkeyPatch,
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    tmp_path: Path,
) -> None:
    """Accept commented JSONC content in a .json file."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        "{\n // comment inside the object\n "
        '"listeners": [' + json.dumps(make_listener()) + "]\n}",
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert config.listeners[0].listener.mode == "passthrough"


def test_load_config_rejects_comment_only_file(tmp_path: Path) -> None:
    """Reject a configuration file that only contains comments."""
    config_path = tmp_path / "config.jsonc"
    config_path.write_text("// nothing but a comment\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Unable to read the configuration JSON"):
        load_config(config_path)


def test_strip_jsonc_comments_keeps_strings_with_urls() -> None:
    """Keep // inside JSON string values such as URLs."""
    text = '{"url": "https://example.test/a"}'
    assert _strip_jsonc_comments(text) == text


def test_strip_jsonc_comments_keeps_escaped_quotes() -> None:
    """Keep // that follows an escaped quote inside a JSON string."""
    text = '{"a": "b\\"// c"}'
    assert _strip_jsonc_comments(text) == text


def test_strip_jsonc_comments_keeps_escaped_backslash_before_quote() -> None:
    """Treat a backslash-escaped quote as string content, not a terminator."""
    text = '{"a": "b\\\\"'
    assert _strip_jsonc_comments(text) == text


def test_strip_jsonc_comments_removes_line_comment_and_keeps_newline() -> None:
    """Remove line comments while preserving the terminating newline."""
    assert (
        _strip_jsonc_comments('{"a": 1} // note\n{"b": 2}')
        == '{"a": 1} \n{"b": 2}'
    )


def test_strip_jsonc_comments_handles_line_comment_at_eof() -> None:
    """Remove a line comment that is not terminated by a newline."""
    assert _strip_jsonc_comments('{"a": 1} // eof') == '{"a": 1} '


def test_strip_jsonc_comments_replaces_block_comment_with_spaces() -> None:
    """Replace block comments with spaces so tokens do not merge."""
    assert _strip_jsonc_comments('{"a": 1}/*x*/{"b": 2}') == '{"a": 1} {"b": 2}'


def test_strip_jsonc_comments_preserves_newlines_in_block_comment() -> None:
    """Preserve newlines inside block comments to keep error line numbers."""
    assert _strip_jsonc_comments('{"a":/*multi\nline*/1}') == '{"a":\n 1}'


def test_strip_jsonc_comments_keeps_block_comment_start_inside_string() -> None:
    """Keep /* inside JSON string values."""
    text = '{"a": "/* not a comment */"}'
    assert _strip_jsonc_comments(text) == text


def test_strip_jsonc_comments_handles_unterminated_block_comment() -> None:
    """Drop the remainder of an unterminated block comment."""
    assert _strip_jsonc_comments('{"a": 1} /* never closed') == '{"a": 1} '


def test_load_config_warns_on_unknown_top_level_key(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Warn on a misspelled top-level key while still loading the configuration."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(write_config({"timeout": 5}))

    assert config.listeners[0].listener.port == 8000
    assert config.warnings == (
        "Unknown top-level configuration keys were ignored: timeout.",
    )


def test_load_config_warns_on_multiple_unknown_top_level_keys_sorted(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Warn on every unknown top-level key listed in sorted order."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(write_config({"zeta_key": True, "alpha_key": True}))

    assert config.warnings == (
        "Unknown top-level configuration keys were ignored: alpha_key, zeta_key.",
    )


def test_load_config_has_no_warnings_for_valid_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Keep the warnings tuples empty when the configuration needs no warnings."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(write_config())

    assert config.warnings == ()
    assert config.listeners[0].warnings == ()


@pytest.mark.parametrize("file_setting", ["path", "max_bytes", "backup_count"])
def test_load_config_warns_on_disabled_file_with_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    tmp_path: Path,
    file_setting: str,
) -> None:
    """Warn when disabled file output still carries file settings."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    file_config: dict[str, object] = {"enabled": False}
    if file_setting == "path":
        file_config["path"] = str(tmp_path / "proxy.jsonl")
    elif file_setting == "max_bytes":
        file_config["max_bytes"] = 100
    else:
        file_config["backup_count"] = 2
    listener = make_listener(
        {
            "features": [
                {"name": "logging", "config": {"stdout": True, "file": file_config}}
            ]
        }
    )
    config = load_config(write_config({"listeners": [listener]}))
    logging = config.listeners[0].logging

    assert logging is not None
    assert logging.file.enabled is False
    assert logging.file.path is None
    expected_warning = (
        "logging.file settings were ignored because "
        "logging.file.enabled is false."
    )
    assert config.listeners[0].warnings == (expected_warning,)


def test_load_config_has_no_warning_for_bare_disabled_file(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Do not warn when the disabled file section carries no settings."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {
            "features": [
                {
                    "name": "logging",
                    "config": {"stdout": True, "file": {"enabled": False}},
                }
            ]
        }
    )
    config = load_config(write_config({"listeners": [listener]}))
    logging = config.listeners[0].logging

    assert logging is not None
    assert logging.file.enabled is False
    assert config.listeners[0].warnings == ()


def test_load_config_still_raises_on_invalid_settings_with_warnings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Keep rejecting invalid settings even when warnings would also apply."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")

    with pytest.raises(ConfigError, match="port"):
        load_config(
            write_config({"listeners": [make_listener({"port": 0})], "timeout": 5})
        )


def test_load_config_warns_on_duplicate_log_file_paths(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    tmp_path: Path,
) -> None:
    """Warn when several listeners write protocol logs to the same file."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    log_path = str(tmp_path / "proxy.jsonl")
    logging_config = {"stdout": False, "file": {"enabled": True, "path": log_path}}
    listeners = [
        make_listener(
            {"port": 8000, "features": [{"name": "logging", "config": logging_config}]}
        ),
        make_listener(
            {"port": 8001, "features": [{"name": "logging", "config": logging_config}]}
        ),
    ]
    config = load_config(write_config({"listeners": listeners}))

    expected_warning = (
        f"Multiple listeners write protocol logs to the same file: {log_path}."
    )
    assert config.warnings == (expected_warning,)


def test_load_config_has_no_warning_for_distinct_log_file_paths(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    tmp_path: Path,
) -> None:
    """Do not warn when listeners write protocol logs to distinct files."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listeners = [
        make_listener(
            {
                "port": 8000,
                "features": [
                    {
                        "name": "logging",
                        "config": {
                            "stdout": False,
                            "file": {"enabled": True, "path": str(tmp_path / "a.jsonl")},
                        },
                    }
                ],
            }
        ),
        make_listener(
            {
                "port": 8001,
                "features": [
                    {
                        "name": "logging",
                        "config": {
                            "stdout": False,
                            "file": {"enabled": True, "path": str(tmp_path / "b.jsonl")},
                        },
                    }
                ],
            }
        ),
    ]
    config = load_config(write_config({"listeners": listeners}))

    assert config.warnings == ()


def test_load_config_reads_grok_image_edit_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Read explicit grok-image-edit settings into the configuration object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {
            "mode": "grok-image-edit",
            "grok_image_edit": {
                "default_model": "grok-image-edit-beta",
                "aspect_ratio": "1:1",
                "resolution": "2k",
            },
        }
    )
    config = load_config(write_config({"listeners": [listener]}))
    grok_image_edit = config.listeners[0].listener.grok_image_edit

    assert grok_image_edit is not None
    assert grok_image_edit.default_model == "grok-image-edit-beta"
    assert grok_image_edit.aspect_ratio == "1:1"
    assert grok_image_edit.resolution == "2k"


def test_load_config_warns_on_grok_image_edit_in_passthrough_mode(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Warn when grok-image-edit settings appear in another listener mode."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener({"grok_image_edit": {"resolution": "1k"}})
    config = load_config(write_config({"listeners": [listener]}))

    expected_warning = (
        "listener.grok_image_edit is ignored because listener.mode is not 'grok-image-edit'."
    )
    assert config.listeners[0].listener.grok_image_edit is None
    assert config.listeners[0].warnings == (expected_warning,)


@pytest.mark.parametrize("grok_image_edit", ["x", ["x"], 123, True, None])
def test_load_config_rejects_non_object_grok_image_edit(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    grok_image_edit: object,
) -> None:
    """Reject grok-image-edit settings that are not JSON objects."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")

    with pytest.raises(ConfigError, match="must be an object"):
        load_config(
            write_config(
                {"listeners": [make_listener({"mode": "grok-image-edit", "grok_image_edit": grok_image_edit})]}
            )
        )


def test_load_config_rejects_unknown_grok_image_edit_field(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Reject grok-image-edit settings with an unknown field."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {"mode": "grok-image-edit", "grok_image_edit": {"size": "1k"}}
    )

    with pytest.raises(ConfigError, match="unknown"):
        load_config(write_config({"listeners": [listener]}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_model", ""),
        ("default_model", 123),
        ("aspect_ratio", ""),
        ("aspect_ratio", 123),
    ],
)
def test_load_config_rejects_invalid_grok_image_edit_string_field(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    field: str,
    value: object,
) -> None:
    """Reject non-string and empty grok-image-edit string settings."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {"mode": "grok-image-edit", "grok_image_edit": {field: value}}
    )

    with pytest.raises(ConfigError, match="non-empty string"):
        load_config(write_config({"listeners": [listener]}))


@pytest.mark.parametrize(
    ("resolution", "should_load"),
    [("1k", True), ("2k", True), ("3k", False), ("", False), (123, False), (["1k"], False)],
)
def test_load_config_validates_grok_image_edit_resolution(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    make_listener: Callable[[dict[str, object]], dict[str, object]],
    resolution: object,
    should_load: bool,
) -> None:
    """Only allow the documented grok-image-edit resolutions."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    listener = make_listener(
        {"mode": "grok-image-edit", "grok_image_edit": {"resolution": resolution}}
    )
    config_path = write_config({"listeners": [listener]})

    if should_load:
        config = load_config(config_path)
        assert config.listeners[0].listener.grok_image_edit is not None
        assert config.listeners[0].listener.grok_image_edit.resolution == resolution
    else:
        with pytest.raises(ConfigError, match="resolution"):
            load_config(config_path)