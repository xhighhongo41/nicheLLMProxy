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
        ({"listener": {"port": 8000, "mode": ["grok-image"]}}, "mode"),
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


def test_load_config_accepts_grok_image_mode(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Accept grok-image mode without a grok_image object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(
        write_config({"listener": {"port": 8000, "mode": "grok-image"}})
    )

    assert config.listener.mode == "grok-image"
    assert config.listener.grok_image is None


def test_load_config_reads_grok_image_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Read grok-image mode settings into the configuration object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(
        write_config(
            {
                "listener": {
                    "port": 8000,
                    "mode": "grok-image",
                    "grok_image": {
                        "default_model": "grok-imagine-image-2.0",
                        "aspect_ratio": "1:1",
                        "resolution": "1k",
                    },
                }
            }
        )
    )

    assert config.listener.mode == "grok-image"
    grok_image = config.listener.grok_image
    assert grok_image is not None
    assert grok_image.default_model == "grok-imagine-image-2.0"
    assert grok_image.aspect_ratio == "1:1"
    assert grok_image.resolution == "1k"


def test_load_config_rejects_grok_image_in_passthrough_mode(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject a grok_image object when the listener runs in passthrough mode."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    with pytest.raises(ConfigError, match="grok-image"):
        load_config(
            write_config(
                {
                    "listener": {
                        "port": 8000,
                        "mode": "passthrough",
                        "grok_image": {"default_model": "grok-imagine-image-2.0"},
                    }
                }
            )
        )


@pytest.mark.parametrize(
    "grok_image",
    ["not-an-object", [{"default_model": "grok-imagine-image-2.0"}], 123, True],
)
def test_load_config_rejects_non_object_grok_image(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    grok_image: object,
) -> None:
    """Reject a grok_image value that is not an object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    with pytest.raises(ConfigError, match="must be an object"):
        load_config(
            write_config(
                {
                    "listener": {
                        "port": 8000,
                        "mode": "grok-image",
                        "grok_image": grok_image,
                    }
                }
            )
        )


def test_load_config_rejects_unknown_grok_image_field(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject an unknown field inside the grok_image object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    with pytest.raises(ConfigError, match="unknown"):
        load_config(
            write_config(
                {
                    "listener": {
                        "port": 8000,
                        "mode": "grok-image",
                        "grok_image": {
                            "default_model": "grok-imagine-image-2.0",
                            "size": "1024x1024",
                        },
                    }
                }
            )
        )


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
    field: str,
    value: object,
) -> None:
    """Reject an empty or non-string grok_image string field."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    with pytest.raises(ConfigError, match="non-empty string"):
        load_config(
            write_config(
                {
                    "listener": {
                        "port": 8000,
                        "mode": "grok-image",
                        "grok_image": {field: value},
                    }
                }
            )
        )


def test_load_config_accepts_null_grok_image_fields(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Treat null grok_image fields as unset defaults."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(
        write_config(
            {
                "listener": {
                    "port": 8000,
                    "mode": "grok-image",
                    "grok_image": {
                        "default_model": None,
                        "aspect_ratio": None,
                        "resolution": None,
                    },
                }
            }
        )
    )

    grok_image = config.listener.grok_image
    assert grok_image is not None
    assert grok_image.default_model is None
    assert grok_image.aspect_ratio is None
    assert grok_image.resolution is None


@pytest.mark.parametrize(
    ("resolution", "valid"),
    [
        ("1k", True),
        ("2k", True),
        ("3k", False),
        ("", False),
        (123, False),
        (["1k"], False),
    ],
)
def test_load_config_validates_grok_image_resolution(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    resolution: object,
    valid: bool,
) -> None:
    """Accept only the supported grok-image resolutions."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    settings = {
        "listener": {
            "port": 8000,
            "mode": "grok-image",
            "grok_image": {"resolution": resolution},
        }
    }
    if valid:
        config = load_config(write_config(settings))
        assert config.listener.grok_image.resolution == resolution
    else:
        with pytest.raises(ConfigError, match="resolution"):
            load_config(write_config(settings))


def test_load_config_accepts_gemini_image_mode(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Accept gemini-image mode without a gemini_image object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(
        write_config({"listener": {"port": 8000, "mode": "gemini-image"}})
    )

    assert config.listener.mode == "gemini-image"
    assert config.listener.gemini_image is None


def test_load_config_reads_gemini_image_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Read gemini-image mode settings into the configuration object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(
        write_config(
            {
                "listener": {
                    "port": 8000,
                    "mode": "gemini-image",
                    "gemini_image": {
                        "default_model": "gemini-3-pro-image-preview",
                        "aspect_ratio": "1:1",
                    },
                }
            }
        )
    )

    assert config.listener.mode == "gemini-image"
    gemini_image = config.listener.gemini_image
    assert gemini_image is not None
    assert gemini_image.default_model == "gemini-3-pro-image-preview"
    assert gemini_image.aspect_ratio == "1:1"


@pytest.mark.parametrize("mode", ["passthrough", "grok-image"])
def test_load_config_rejects_gemini_image_in_other_modes(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    mode: str,
) -> None:
    """Reject a gemini_image object when the listener runs in another mode."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    with pytest.raises(ConfigError, match="gemini-image"):
        load_config(
            write_config(
                {
                    "listener": {
                        "port": 8000,
                        "mode": mode,
                        "gemini_image": {
                            "default_model": "gemini-3-pro-image-preview"
                        },
                    }
                }
            )
        )


@pytest.mark.parametrize(
    "gemini_image",
    ["not-an-object", [{"default_model": "gemini-3-pro-image-preview"}], 123, True],
)
def test_load_config_rejects_non_object_gemini_image(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
    gemini_image: object,
) -> None:
    """Reject a gemini_image value that is not an object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    with pytest.raises(ConfigError, match="must be an object"):
        load_config(
            write_config(
                {
                    "listener": {
                        "port": 8000,
                        "mode": "gemini-image",
                        "gemini_image": gemini_image,
                    }
                }
            )
        )


def test_load_config_rejects_unknown_gemini_image_field(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Reject an unknown field inside the gemini_image object."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    with pytest.raises(ConfigError, match="unknown"):
        load_config(
            write_config(
                {
                    "listener": {
                        "port": 8000,
                        "mode": "gemini-image",
                        "gemini_image": {
                            "default_model": "gemini-3-pro-image-preview",
                            "resolution": "1k",
                        },
                    }
                }
            )
        )


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
    field: str,
    value: object,
) -> None:
    """Reject an empty or non-string gemini_image string field."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    with pytest.raises(ConfigError, match="non-empty string"):
        load_config(
            write_config(
                {
                    "listener": {
                        "port": 8000,
                        "mode": "gemini-image",
                        "gemini_image": {field: value},
                    }
                }
            )
        )


def test_load_config_accepts_null_gemini_image_fields(
    monkeypatch: pytest.MonkeyPatch,
    write_config: Callable[[dict[str, object] | None], Path],
) -> None:
    """Treat null gemini_image fields as unset defaults."""
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")
    config = load_config(
        write_config(
            {
                "listener": {
                    "port": 8000,
                    "mode": "gemini-image",
                    "gemini_image": {
                        "default_model": None,
                        "aspect_ratio": None,
                    },
                }
            }
        )
    )

    gemini_image = config.listener.gemini_image
    assert gemini_image is not None
    assert gemini_image.default_model is None
    assert gemini_image.aspect_ratio is None


def test_load_config_reports_supported_modes_in_mode_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report every supported mode in the listener mode validation error."""
    config_path = tmp_path / "invalid.json"
    config_path.write_text(
        json.dumps(
            {
                "listener": {"port": 8000, "mode": "unknown"},
                "upstream": {
                    "base_url": "https://upstream.example.test",
                    "api_key_env": "UPSTREAM_API_KEY",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("UPSTREAM_API_KEY", "secret-value")

    with pytest.raises(
        ConfigError,
        match="must be 'passthrough', 'grok-image' or 'gemini-image'",
    ):
        load_config(config_path)
