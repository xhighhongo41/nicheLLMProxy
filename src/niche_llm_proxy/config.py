"""nicheLLM Proxy の起動設定を安全に読み込む。"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_CONFIG_PATH = Path("/app/config/config.json")
"""環境変数で指定されない場合に使う設定ファイルのパス。"""

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
"""上流への接続タイムアウトの既定値（秒）。"""

DEFAULT_READ_TIMEOUT_SECONDS = 120.0
"""上流からの読取タイムアウトの既定値（秒）。"""


class ConfigError(ValueError):
    """起動に必要な設定が不正または不足している場合の例外。"""


@dataclass(frozen=True)
class ListenerConfig:
    """単一 listener の待受設定。"""

    port: int
    mode: str


@dataclass(frozen=True)
class TimeoutConfig:
    """上流通信に使用するタイムアウト設定。"""

    connect_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS


@dataclass(frozen=True)
class UpstreamConfig:
    """上流 OpenAI 互換 API への接続設定。"""

    base_url: str
    api_key_env: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class ProxyConfig:
    """nicheLLM Proxy の検証済み起動設定。"""

    listener: ListenerConfig
    upstream: UpstreamConfig
    timeouts: TimeoutConfig


def get_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """環境変数を反映した設定ファイルのパスを返す。

    Args:
        environ: 参照する環境変数。省略時はプロセス環境を使う。

    Returns:
        `NICHELLM_CONFIG_PATH` または既定の設定ファイルパス。
    """

    environment = os.environ if environ is None else environ
    configured_path = environment.get("NICHELLM_CONFIG_PATH")
    return Path(configured_path) if configured_path else DEFAULT_CONFIG_PATH


def load_config(
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProxyConfig:
    """JSONファイルと環境変数から検証済みのプロキシ設定を読み込む。

    API キーの値は環境変数からのみ読み込み、例外メッセージには含めない。

    Args:
        config_path: 設定ファイルのパス。省略時は環境変数または既定値を使う。
        environ: 参照する環境変数。省略時はプロセス環境を使う。

    Raises:
        ConfigError: 設定ファイル、設定値、または API キーが不正な場合。

    Returns:
        起動に使用できる検証済み設定。
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
            f"上流 API キー環境変数 '{api_key_env}' が設定されていません。"
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
    """設定ファイルを読み込み、最上位の JSON オブジェクトを返す。"""

    try:
        with path.open(encoding="utf-8") as config_file:
            value = json.load(config_file)
    except FileNotFoundError as error:
        raise ConfigError(f"設定ファイルが見つかりません: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError("設定 JSON を読み込めません。") from error

    if not isinstance(value, dict):
        raise ConfigError("設定 JSON の最上位はオブジェクトである必要があります。")
    return value


def _required_object(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """必須のオブジェクト設定を取得する。"""

    value = config.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"設定項目 '{key}' はオブジェクトである必要があります。")
    return value


def _optional_object(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """任意のオブジェクト設定を取得し、未指定時は空の設定を返す。"""

    if key not in config:
        return {}
    return _required_object(config, key)


def _port(listener: Mapping[str, Any]) -> int:
    """listener のポート番号を検証して返す。"""

    value = listener.get("port")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError("listener.port は 1 から 65535 の整数である必要があります。")
    return value


def _passthrough_mode(listener: Mapping[str, Any]) -> str:
    """v0.1 で対応する listener mode を検証して返す。"""

    mode = listener.get("mode")
    if mode != "passthrough":
        raise ConfigError("listener.mode は 'passthrough' である必要があります。")
    return mode


def _base_url(upstream: Mapping[str, Any]) -> str:
    """上流 URL の形式を検証して返す。"""

    value = _non_empty_string(upstream, "base_url")
    try:
        parsed = urlparse(value)
        # 不正なポート番号や IPv6 リテラルは属性参照時に検出される。
        parsed.port
    except ValueError as error:
        raise ConfigError(
            "upstream.base_url は有効な http/https URL である必要があります。"
        ) from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            "upstream.base_url はクエリ・フラグメントを含まない http/https URL である必要があります。"
        )
    return value


def _non_empty_string(config: Mapping[str, Any], key: str) -> str:
    """空でない文字列の設定値を検証して返す。"""

    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"設定項目 '{key}' は空でない文字列である必要があります。")
    return value


def _positive_number(
    config: Mapping[str, Any],
    key: str,
    default: float,
) -> float:
    """正の数値の任意設定を検証して返す。"""

    if key not in config:
        return default

    value = config[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigError(f"timeouts.{key} は 0 より大きい数値である必要があります。")
    return float(value)
