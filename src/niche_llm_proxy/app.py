"""nicheLLM ProxyのASGIアプリケーション。"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from niche_llm_proxy.config import ProxyConfig
from niche_llm_proxy.passthrough import (
    build_upstream_url,
    create_http_client,
    prepare_request_headers,
    prepare_response_headers,
    stream_response,
    upstream_error_detail,
)

_FORWARDED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_app(
    config: ProxyConfig,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """指定設定のパススルー用ASGIアプリケーションを生成する。"""
    app = FastAPI(title="nicheLLM Proxy", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.proxy_config = config

    @app.get("/health")
    async def health() -> dict[str, str]:
        """プロキシ自身の生存状態を返す。"""
        return {"status": "ok"}

    @app.api_route(
        "/{path:path}",
        methods=_FORWARDED_METHODS,
        response_model=None,
    )
    async def passthrough(path: str, request: Request) -> StreamingResponse | JSONResponse:
        """受信HTTPリクエストを上流へ非変換で中継する。"""
        del path  # URLはRequestから取得し、ルートパラメータとの乖離を防ぐ。
        client = create_http_client(config, transport=upstream_transport)
        upstream_url = build_upstream_url(
            config.upstream.base_url,
            request.url.path,
            request.url.query,
        )
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            headers=prepare_request_headers(request.headers, config.upstream.api_key),
            content=request.stream(),
        )

        try:
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.RequestError as error:
            await client.aclose()
            status_code, content = upstream_error_detail(error)
            return JSONResponse(status_code=status_code, content=content)

        return StreamingResponse(
            stream_response(upstream_response, client),
            status_code=upstream_response.status_code,
            headers=prepare_response_headers(upstream_response.headers),
        )

    return app
