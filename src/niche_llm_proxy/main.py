"""Command-line startup for nicheLLM Proxy."""

from __future__ import annotations

import sys

import uvicorn

from niche_llm_proxy.app import PROXY_VERSION, create_app
from niche_llm_proxy.config import ConfigError, ProxyConfig, load_config
from niche_llm_proxy.i18n import translate


def build_startup_message(config: ProxyConfig) -> str:
    """Return the translated one-line startup announcement for a configuration.

    The line names the proxy version, the listener mode, and the enabled
    feature names. Port information is left to the server log.
    """
    if config.listener.features:
        return translate(
            "nicheLLM Proxy {version} starting in '{mode}' mode with features: {features}.",
            version=PROXY_VERSION,
            mode=config.listener.mode,
            features=", ".join("logging" for _ in config.listener.features),
        )
    return translate(
        "nicheLLM Proxy {version} starting in '{mode}' mode with no features.",
        version=PROXY_VERSION,
        mode=config.listener.mode,
    )


def announce_startup(config: ProxyConfig) -> None:
    """Print the startup line to stdout and each collected warning to stderr.

    Warnings are already translated when the configuration is loaded, so they
    are printed as-is.
    """
    print(build_startup_message(config))
    for warning in config.warnings:
        print(warning, file=sys.stderr)


def main() -> None:
    """Validate configuration, announce startup, and start the ASGI server."""
    try:
        config = load_config()
    except ConfigError as error:
        print(translate("Configuration error: {error}", error=error), file=sys.stderr)
        raise SystemExit(2) from error

    announce_startup(config)
    uvicorn.run(create_app(config), host="0.0.0.0", port=config.listener.port)


if __name__ == "__main__":
    main()
