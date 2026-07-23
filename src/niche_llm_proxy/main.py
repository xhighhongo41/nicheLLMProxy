"""nicheLLM Proxyのコマンドライン起動処理。"""

from __future__ import annotations

import sys

import uvicorn

from niche_llm_proxy.app import create_app
from niche_llm_proxy.config import ConfigError, load_config


def main() -> None:
    """設定を検証してからASGIサーバーを起動する。"""
    try:
        config = load_config()
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error

    uvicorn.run(create_app(config), host="0.0.0.0", port=config.listener.port)


if __name__ == "__main__":
    main()
