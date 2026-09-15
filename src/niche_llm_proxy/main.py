"""Command-line startup for nicheLLM Proxy."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import threading
from collections.abc import Iterator, Sequence
from types import FrameType
from typing import cast

import uvicorn

from niche_llm_proxy.app import PROXY_VERSION, create_app
from niche_llm_proxy.config import ConfigError, ProxyConfig, load_config
from niche_llm_proxy.i18n import translate

_HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def build_startup_message(config: ProxyConfig) -> str:
    """Return the translated startup announcement for a configuration.

    The first line names the proxy version and the listener count; every
    listener then gets one line naming its port, mode, and enabled feature
    names.
    """
    count = len(config.listeners)
    if count == 1:
        header = translate(
            "nicheLLM Proxy {version} starting with {count} listener.",
            version=PROXY_VERSION,
            count=count,
        )
    else:
        header = translate(
            "nicheLLM Proxy {version} starting with {count} listeners.",
            version=PROXY_VERSION,
            count=count,
        )
    lines = [header]
    for listener_config in config.listeners:
        listener = listener_config.listener
        if listener.features:
            lines.append(
                translate(
                    "  - port {port}: mode '{mode}' with features: {features}.",
                    port=listener.port,
                    mode=listener.mode,
                    features=", ".join("logging" for _ in listener.features),
                )
            )
        else:
            lines.append(
                translate(
                    "  - port {port}: mode '{mode}' with no features.",
                    port=listener.port,
                    mode=listener.mode,
                )
            )
    return "\n".join(lines)


def announce_startup(config: ProxyConfig) -> None:
    """Print the startup announcement to stdout and each warning to stderr.

    Global warnings print unprefixed; per-listener warnings print with a
    ``[port N]`` prefix so the affected listener is identifiable. Warnings are
    already translated when the configuration is loaded, so they are printed
    as-is.
    """
    print(build_startup_message(config))
    for warning in config.warnings:
        print(warning, file=sys.stderr)
    for listener_config in config.listeners:
        for warning in listener_config.warnings:
            print(
                f"[port {listener_config.listener.port}] {warning}",
                file=sys.stderr,
            )


def build_servers(config: ProxyConfig) -> list[uvicorn.Server]:
    """Build one uvicorn server per listener, bound to the listener's port."""

    servers: list[uvicorn.Server] = []
    for listener_config in config.listeners:
        uvicorn_config = uvicorn.Config(
            create_app(listener_config),
            host="0.0.0.0",
            port=listener_config.listener.port,
        )
        servers.append(uvicorn.Server(uvicorn_config))
    return servers


@contextlib.contextmanager
def _no_signal_capture() -> Iterator[None]:
    """Replace uvicorn's per-server signal capture with a no-op."""

    yield


def _install_shutdown_handlers(
    servers: Sequence[uvicorn.Server],
) -> dict[int, object]:
    """Install one shutdown handler for all servers and keep the originals.

    Mirrors uvicorn's signal semantics: the first signal requests a graceful
    shutdown of every server and a second SIGINT escalates to a forced exit.
    """

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        for server in servers:
            if server.should_exit and signum == signal.SIGINT:
                server.force_exit = True
            server.should_exit = True

    if threading.current_thread() is not threading.main_thread():
        return {}
    return {
        signum: signal.signal(signum, handle_signal)
        for signum in _HANDLED_SIGNALS
    }


async def serve_all(servers: Sequence[uvicorn.Server]) -> None:
    """Serve all configured servers concurrently until they all shut down."""

    for server in servers:
        # Each uvicorn server would otherwise install its own signal handler
        # and overwrite the others, so only the combined handler above stays.
        server.capture_signals = cast("uvicorn.Server.capture_signals", _no_signal_capture)
    original_handlers = _install_shutdown_handlers(servers)
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        for signum, handler in original_handlers.items():
            signal.signal(signum, cast("signal._HANDLER", handler))


def main() -> None:
    """Validate configuration, announce startup, and start the ASGI servers."""

    try:
        config = load_config()
    except ConfigError as error:
        print(translate("Configuration error: {error}", error=error), file=sys.stderr)
        raise SystemExit(2) from error

    announce_startup(config)
    asyncio.run(serve_all(build_servers(config)))


if __name__ == "__main__":
    main()