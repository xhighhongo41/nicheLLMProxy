"""ASGI application for nicheLLM Proxy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from niche_llm_proxy.config import ProxyConfig
from niche_llm_proxy.logging_feature import ExchangeLog, LoggingRuntime
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
    """Create a passthrough ASGI application for the supplied configuration."""
    logging_runtime = LoggingRuntime(config.logging) if config.logging is not None else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Close the background logging listener as the ASGI application stops."""

        try:
            yield
        finally:
            if logging_runtime is not None:
                logging_runtime.close()

    app = FastAPI(
        title="nicheLLM Proxy",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.proxy_config = config
    app.state.logging_runtime = logging_runtime

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Return the proxy's liveness state."""
        return {"status": "ok"}

    @app.api_route(
        "/{path:path}",
        methods=_FORWARDED_METHODS,
        response_model=None,
    )
    async def passthrough(
        path: str,
        request: Request,
    ) -> StreamingResponse | JSONResponse:
        """Relay an incoming HTTP request to the upstream without transformation."""
        del path  # Use the Request URL to avoid divergence from the route parameter.
        exchange = _new_exchange(logging_runtime, request)
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
            content=(exchange.wrap_request(request.stream()) if exchange else request.stream()),
        )

        try:
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.RequestError as error:
            await client.aclose()
            if exchange is not None:
                exchange.fail("upstream_send", error)
            status_code, content = upstream_error_detail(error)
            return JSONResponse(status_code=status_code, content=content)
        except asyncio.CancelledError:
            await client.aclose()
            if exchange is not None:
                exchange.cancel("upstream_send")
            raise
        except BaseException as error:
            await client.aclose()
            if exchange is not None:
                exchange.fail("upstream_send", error)
            raise

        if exchange is not None:
            exchange.response_started(
                upstream_response.status_code,
                upstream_response.headers.raw,
                config.upstream.base_url,
            )

        response = StreamingResponse(
            stream_response(
                upstream_response,
                client,
                on_chunk=exchange.observe_response if exchange else None,
                on_complete=exchange.complete if exchange else None,
                on_error=(
                    lambda error: exchange.fail("response_stream", error)
                )
                if exchange
                else None,
                on_cancel=(lambda: exchange.cancel("response_stream")) if exchange else None,
            ),
            status_code=upstream_response.status_code,
        )
        response.raw_headers = prepare_response_headers(upstream_response.headers)
        return response

    return app


def _new_exchange(
    logging_runtime: LoggingRuntime | None,
    request: Request,
) -> ExchangeLog | None:
    """Create a request logger only when the configured feature is enabled."""

    if logging_runtime is None:
        return None
    return logging_runtime.new_exchange(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        request_headers=request.headers.raw,
    )
