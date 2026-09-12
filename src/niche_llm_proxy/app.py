"""ASGI application for nicheLLM Proxy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from niche_llm_proxy.config import ProxyConfig
from niche_llm_proxy.featherless import (
    ConcurrencyGate,
    FeatherlessRuntime,
    QueueWaitTimeoutError,
    Reservation,
    extract_model_reference,
    handle_featherless_model_detail,
    handle_featherless_models,
    openai_error_response,
    queue_wait_timeout_response,
)
from niche_llm_proxy.featherless import (
    RequestKind as FeatherlessRequestKind,
)
from niche_llm_proxy.featherless import (
    classify_request as classify_featherless_request,
)
from niche_llm_proxy.gemini_image import (
    GeminiRequestKind,
    handle_gemini_image_generations,
    handle_gemini_models,
)
from niche_llm_proxy.gemini_image import (
    classify_request as classify_gemini_request,
)
from niche_llm_proxy.grok_image import (
    RequestKind,
    classify_request,
    handle_grok_image_generations,
)
from niche_llm_proxy.i18n import translate
from niche_llm_proxy.image_relay import TransformError
from niche_llm_proxy.logging_feature import ExchangeLog, LoggingRuntime
from niche_llm_proxy.passthrough import (
    HeaderPairs,
    build_upstream_url,
    create_http_client,
    prepare_request_headers,
    prepare_request_headers_preserve_auth,
    prepare_response_headers,
    stream_response,
    upstream_error_detail,
)

_FORWARDED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

_UNSUPPORTED_PATH_MESSAGES = {
    "grok-image": "This path is not supported in 'grok-image' mode.",
    "gemini-image": "This path is not supported in 'gemini-image' mode.",
}

_REJECT_ROUTE_EVENTS = {
    "grok-image": "grok_image_route",
    "gemini-image": "gemini_image_route",
}


def create_app(
    config: ProxyConfig,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create a passthrough ASGI application for the supplied configuration."""
    logging_runtime = LoggingRuntime(config.logging) if config.logging is not None else None
    featherless_runtime = (
        FeatherlessRuntime(config, upstream_transport)
        if config.listener.mode == "featherless"
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Run and stop mode-scoped background tasks and the logging listener."""

        try:
            if featherless_runtime is not None:
                await featherless_runtime.gates.start()
            yield
        finally:
            if featherless_runtime is not None:
                await featherless_runtime.gates.close()
            if logging_runtime is not None:
                logging_runtime.close()

    app = FastAPI(
        title="nicheLLM Proxy",
        version="1.3.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.proxy_config = config
    app.state.logging_runtime = logging_runtime
    app.state.featherless_runtime = featherless_runtime

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
        if config.listener.mode == "grok-image":
            kind = classify_request(request.url.path, request.method)
            if kind is RequestKind.GENERATIONS:
                exchange = _new_exchange(logging_runtime, request)
                return await handle_grok_image_generations(
                    config, exchange, upstream_transport, request
                )
            if kind in (RequestKind.UNSUPPORTED_PATH, RequestKind.METHOD_NOT_ALLOWED):
                return _reject_image_route(logging_runtime, request, kind, "grok-image")
        elif config.listener.mode == "gemini-image":
            kind = classify_gemini_request(request.url.path, request.method)
            if kind is GeminiRequestKind.GENERATIONS:
                exchange = _new_exchange(logging_runtime, request)
                return await handle_gemini_image_generations(
                    config, exchange, upstream_transport, request
                )
            if kind is GeminiRequestKind.MODELS:
                exchange = _new_exchange(logging_runtime, request)
                return await handle_gemini_models(
                    config, exchange, upstream_transport, request
                )
            if kind in (
                GeminiRequestKind.UNSUPPORTED_PATH,
                GeminiRequestKind.METHOD_NOT_ALLOWED,
            ):
                return _reject_image_route(
                    logging_runtime, request, kind, "gemini-image"
                )
        elif config.listener.mode == "featherless":
            kind = classify_featherless_request(request.url.path, request.method)
            if kind is FeatherlessRequestKind.MODELS_LIST:
                exchange = _new_exchange(logging_runtime, request)
                return await handle_featherless_models(
                    featherless_runtime, exchange, request
                )
            if kind is FeatherlessRequestKind.MODEL_DETAIL:
                exchange = _new_exchange(logging_runtime, request)
                return await handle_featherless_model_detail(
                    featherless_runtime, exchange, request
                )
            if kind is FeatherlessRequestKind.JSON_MODEL_OPERATION:
                return await _handle_featherless_operation(
                    config,
                    featherless_runtime,
                    logging_runtime,
                    request,
                    upstream_transport,
                )
        exchange = _new_exchange(logging_runtime, request)
        return await _relay_upstream(
            config,
            request,
            exchange,
            upstream_transport,
            request_headers=prepare_request_headers(
                request.headers, config.upstream.api_key
            ),
            content=(
                exchange.wrap_request(request.stream()) if exchange else request.stream()
            ),
        )

    return app


async def _relay_upstream(
    config: ProxyConfig,
    request: Request,
    exchange: ExchangeLog | None,
    upstream_transport: httpx.AsyncBaseTransport | None,
    *,
    request_headers: HeaderPairs,
    content: AsyncIterator[bytes],
    reservation: Reservation | None = None,
) -> StreamingResponse | JSONResponse:
    """Relay a request upstream unchanged and stream the response back.

    An optional concurrency reservation is released as soon as the relay
    finishes, fails or is cancelled.
    """

    release = reservation.release if reservation is not None else None
    client = create_http_client(config, transport=upstream_transport)
    upstream_url = build_upstream_url(
        config.upstream.base_url,
        request.url.path,
        request.url.query,
    )
    upstream_request = client.build_request(
        request.method,
        upstream_url,
        headers=request_headers,
        content=content,
    )

    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.RequestError as error:
        await client.aclose()
        if release is not None:
            release()
        if exchange is not None:
            exchange.fail("upstream_send", error)
        status_code, error_content = upstream_error_detail(error)
        return JSONResponse(status_code=status_code, content=error_content)
    except asyncio.CancelledError:
        await client.aclose()
        if release is not None:
            release()
        if exchange is not None:
            exchange.cancel("upstream_send")
        raise
    except BaseException as error:
        await client.aclose()
        if release is not None:
            release()
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
            on_complete=_chain(
                exchange.complete if exchange else None,
                release,
            ),
            on_error=_chain_error(
                exchange.fail if exchange else None,
                release,
            ),
            on_cancel=_chain(
                (lambda: exchange.cancel("response_stream")) if exchange else None,
                release,
            ),
        ),
        status_code=upstream_response.status_code,
    )
    response.raw_headers = prepare_response_headers(upstream_response.headers)
    return response


def _chain(*callbacks: Callable[[], None] | None) -> Callable[[], None] | None:
    """Combine optional no-argument callbacks into one, or return ``None``."""

    present = [callback for callback in callbacks if callback is not None]
    if not present:
        return None

    def run() -> None:
        for callback in present:
            callback()

    return run


def _chain_error(
    on_error: Callable[[BaseException], None] | None,
    release: Callable[[], None] | None,
) -> Callable[[BaseException], None] | None:
    """Combine an error callback with an optional release into one callback."""

    if on_error is None and release is None:
        return None

    def run(error: BaseException) -> None:
        if release is not None:
            release()
        if on_error is not None:
            on_error(error)

    return run


async def _acquire_or_disconnect(
    gate: ConcurrencyGate,
    cost: int,
    request: Request,
    *,
    poll_interval_seconds: float = 0.5,
) -> Reservation | None:
    """Acquire concurrency units, giving up when the client disconnects.

    Returns ``None`` when the client disconnected while queued. The gate's
    own cancellation handling removes the waiter and frees any reservation
    granted concurrently with the disconnect.
    """

    acquire_task = asyncio.ensure_future(gate.acquire(cost))
    try:
        while True:
            done, _ = await asyncio.wait(
                {acquire_task}, timeout=poll_interval_seconds
            )
            if done:
                return acquire_task.result()
            if await request.is_disconnected():
                acquire_task.cancel()
                with suppress(asyncio.CancelledError):
                    await acquire_task
                return None
    except asyncio.CancelledError:
        acquire_task.cancel()
        with suppress(asyncio.CancelledError):
            await acquire_task
        raise


async def _handle_featherless_operation(
    config: ProxyConfig,
    runtime: FeatherlessRuntime,
    logging_runtime: LoggingRuntime | None,
    request: Request,
    upstream_transport: httpx.AsyncBaseTransport | None,
) -> StreamingResponse | JSONResponse:
    """Gate a JSON operation on its model cost, then relay it upstream."""

    exchange = _new_exchange(logging_runtime, request)
    body = await request.body()
    model_id = extract_model_reference(body)
    reservation: Reservation | None = None
    if model_id is not None:
        if not runtime.cache.is_whitelisted(model_id):
            message = translate(
                "The requested model is not in the whitelist of 'featherless' mode."
            )
            if exchange is not None:
                exchange.fail(
                    "featherless_whitelist",
                    TransformError(404, message),
                )
            return openai_error_response(404, message)
        gate = runtime.gates.gate_for(request.headers.get("authorization"))
        if gate is not None:
            try:
                reservation = await _acquire_or_disconnect(
                    gate, runtime.cache.concurrency_cost(model_id), request
                )
            except QueueWaitTimeoutError:
                message = translate(
                    "The request was rejected because the wait for upstream "
                    "concurrency capacity exceeded the limit, in 'featherless' mode."
                )
                if exchange is not None:
                    exchange.fail(
                        "featherless_queue_timeout",
                        TransformError(429, message),
                    )
                return queue_wait_timeout_response()
            if reservation is None:
                message = translate(
                    "The client disconnected while waiting for upstream "
                    "concurrency capacity in 'featherless' mode."
                )
                if exchange is not None:
                    exchange.cancel("featherless_queue_wait")
                return JSONResponse(status_code=499, content={"detail": message})
    return await _relay_upstream(
        config,
        request,
        exchange,
        upstream_transport,
        request_headers=prepare_request_headers_preserve_auth(request.headers),
        content=(
            exchange.wrap_request(_single_chunk(body))
            if exchange
            else _single_chunk(body)
        ),
        reservation=reservation,
    )


async def _single_chunk(body: bytes) -> AsyncIterator[bytes]:
    """Yield an already-buffered request body as one observable chunk."""

    if body:
        yield body


def _reject_image_route(
    logging_runtime: LoggingRuntime | None,
    request: Request,
    kind: RequestKind | GeminiRequestKind,
    mode: str,
) -> JSONResponse:
    """Reject a route that an image listener mode does not relay."""

    status_code = 404 if kind.value == "unsupported_path" else 405
    message = translate(_UNSUPPORTED_PATH_MESSAGES[mode])
    exchange = _new_exchange(logging_runtime, request)
    if exchange is not None:
        exchange.fail(
            _REJECT_ROUTE_EVENTS[mode],
            TransformError(status_code, message),
        )
    return JSONResponse(status_code=status_code, content={"detail": message})


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
