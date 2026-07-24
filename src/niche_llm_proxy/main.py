"""Command-line startup for nicheLLM Proxy."""

from __future__ import annotations

import sys

import uvicorn

from niche_llm_proxy.app import create_app
from niche_llm_proxy.config import ConfigError, load_config
from niche_llm_proxy.i18n import translate


def main() -> None:
    """Validate configuration before starting the ASGI server."""
    try:
        config = load_config()
    except ConfigError as error:
        print(translate("Configuration error: {error}", error=error), file=sys.stderr)
        raise SystemExit(2) from error

    uvicorn.run(create_app(config), host="0.0.0.0", port=config.listener.port)


if __name__ == "__main__":
    main()
