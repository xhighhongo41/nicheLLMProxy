"""設定ファイルと秘密情報の検証テスト。"""

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
    """有効な設定と環境変数から設定オブジェクトを作る。"""
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
    """不正な必須設定を起動前に拒否する。"""
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
    """キー未設定のエラーに秘密値を含めない。"""
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
    """設定ファイルパスは専用環境変数で上書きできる。"""
    assert get_config_path({"NICHELLM_CONFIG_PATH": "/tmp/custom.json"}) == Path(
        "/tmp/custom.json"
    )
