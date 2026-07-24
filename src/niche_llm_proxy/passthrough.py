"""Relay HTTP requests unchanged to an OpenAI-compatible upstream."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import httpx
from starlette.datastructures import Headers

from niche_llm_proxy.config import ProxyConfig
from niche_llm_proxy.i18n import translate

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def build_upstream_url(base_url: str, path: str, query: str) -> str:
    """Join an incoming path and query to the upstream base URL."""
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    return f"{url}?{query}" if query else url


def prepare_request_headers(headers: Headers, api_key: str) -> dict[str, str]:
    """Select forwardable headers and replace upstream credentials."""
    connection_header = headers.get("connection", "")
    connection_tokens = {
        value.strip().lower()
        for value in connection_header.split(",")
        if value.strip()
    }
    excluded_headers = _HOP_BY_HOP_HEADERS | connection_tokens | {"host", "authorization"}

    forwarded_headers = {
        name: value
        for name, value in headers.items()
        if name.lower() not in excluded_headers
    }
    forwarded_headers["authorization"] = f"Bearer {api_key}"
    return forwarded_headers


def prepare_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Select upstream response headers that may be returned to the client."""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
    }


async def stream_response(
    response: httpx.Response,
    client: httpx.AsyncClient,
) -> AsyncIterator[bytes]:
    """Yield an upstream response and close connections after completion or disconnect."""
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await response.aclose()
        await client.aclose()


def create_http_client(
    config: ProxyConfig,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Create an upstream HTTP client using the configured timeouts."""
    timeout = httpx.Timeout(
        timeout=config.timeouts.read_seconds,
        connect=config.timeouts.connect_seconds,
    )
    return httpx.AsyncClient(
        timeout=timeout,
        transport=transport,
        follow_redirects=False,
    )


def upstream_error_detail(error: httpx.RequestError) -> tuple[int, Mapping[str, str]]:
    """Map an upstream communication error to a proxy error without secrets."""
    if isinstance(error, httpx.TimeoutException):
        return 504, {"detail": translate("The upstream provider timed out.")}
    return 502, {"detail": translate("Unable to connect to the upstream provider.")}
