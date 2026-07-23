"""OpenAI互換の上流へHTTPリクエストを非変換で中継する処理。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import httpx
from starlette.datastructures import Headers

from niche_llm_proxy.config import ProxyConfig

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
    """上流ベースURLへ受信パスとクエリを結合する。"""
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    return f"{url}?{query}" if query else url


def prepare_request_headers(headers: Headers, api_key: str) -> dict[str, str]:
    """転送可能なヘッダーを選別し、上流用認証情報へ置き換える。"""
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
    """クライアントへ返してよい上流レスポンスヘッダーを抽出する。"""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
    }


async def stream_response(
    response: httpx.Response,
    client: httpx.AsyncClient,
) -> AsyncIterator[bytes]:
    """上流応答を逐次返し、送信完了・切断時にも接続を閉じる。"""
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
    """設定済みの上流通信クライアントを生成する。"""
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
    """上流通信例外を、秘密情報を含まないプロキシのエラー内容へ変換する。"""
    if isinstance(error, httpx.TimeoutException):
        return 504, {"detail": "The upstream provider timed out."}
    return 502, {"detail": "Unable to connect to the upstream provider."}
