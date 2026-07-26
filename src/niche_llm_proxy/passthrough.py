"""Relay HTTP requests unchanged to an OpenAI-compatible upstream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence

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

HeaderPairs = list[tuple[bytes, bytes]]


def build_upstream_url(base_url: str, path: str, query: str) -> str:
    """Join an incoming path and query to the upstream base URL."""
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    return f"{url}?{query}" if query else url


def _connection_tokens(headers: Sequence[tuple[bytes, bytes]]) -> set[bytes]:
    """Return lower-case headers nominated by one or more Connection values."""
    return {
        token.strip().lower()
        for name, value in headers
        if name.lower() == b"connection"
        for token in value.split(b",")
        if token.strip()
    }


def _forwardable_headers(
    headers: Sequence[tuple[bytes, bytes]],
    extra_excluded: set[bytes] | None = None,
) -> HeaderPairs:
    """Keep ordered end-to-end header pairs, including duplicate fields."""
    excluded_headers = {
        name.encode("ascii") for name in _HOP_BY_HOP_HEADERS
    } | _connection_tokens(headers)
    if extra_excluded:
        excluded_headers |= extra_excluded
    return [
        (name, value)
        for name, value in headers
        if name.lower() not in excluded_headers
    ]


def prepare_request_headers(headers: Headers, api_key: str) -> HeaderPairs:
    """Keep end-to-end request headers and replace client credentials."""
    forwarded_headers = _forwardable_headers(
        headers.raw,
        {b"host", b"authorization"},
    )
    forwarded_headers.append((b"authorization", f"Bearer {api_key}".encode("ascii")))
    return forwarded_headers


def prepare_response_headers(headers: httpx.Headers) -> HeaderPairs:
    """Keep ordered upstream end-to-end response headers, including duplicates."""
    return _forwardable_headers(headers.raw)


async def stream_response(
    response: httpx.Response,
    client: httpx.AsyncClient,
    on_chunk: Callable[[bytes], None] | None = None,
    on_complete: Callable[[], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    on_cancel: Callable[[], None] | None = None,
) -> AsyncIterator[bytes]:
    """Yield an upstream response and close connections after completion or disconnect.

    Optional callbacks observe lifecycle events without changing the byte stream. They
    let cross-cutting features such as logging remain outside the HTTP transport.
    """
    try:
        async for chunk in response.aiter_raw():
            if on_chunk is not None:
                on_chunk(chunk)
            yield chunk
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        raise
    except BaseException as error:
        if on_error is not None:
            on_error(error)
        raise
    else:
        if on_complete is not None:
            on_complete()
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
