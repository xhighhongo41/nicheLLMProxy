"""featherless.ai relay mode: model whitelisting and concurrency control."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from niche_llm_proxy.config import FeatherlessConfig, ProxyConfig
from niche_llm_proxy.i18n import translate
from niche_llm_proxy.image_relay import TransformError
from niche_llm_proxy.logging_feature import ExchangeLog
from niche_llm_proxy.passthrough import build_upstream_url, create_http_client

__all__ = [
    "FeatherlessRuntime",
    "ModelInfoCache",
    "RequestKind",
    "classify_request",
    "extract_model_reference",
    "handle_featherless_model_detail",
    "handle_featherless_models",
    "key_id_from_authorization",
]

logger = logging.getLogger(__name__)

_WARM_FETCH_CONCURRENCY = 4
"""Upper bound of parallel model detail fetches during a warm-up pass."""

_CONCURRENCY_COST_FALLBACK = 4
"""Conservative unit cost when the upstream does not report one (decision C3)."""

_MODELS_PATH = "/v1/models"


class RequestKind(Enum):
    """Classification of an incoming featherless request."""

    MODELS_LIST = "models_list"
    MODEL_DETAIL = "model_detail"
    JSON_MODEL_OPERATION = "json_model_operation"
    PROXY_OTHER = "proxy_other"


def classify_request(path: str, method: str) -> RequestKind:
    """Classify an incoming request by path and HTTP method."""

    clean_path = path.split("?", 1)[0]
    upper_method = method.upper()

    if upper_method == "GET":
        if clean_path == _MODELS_PATH:
            return RequestKind.MODELS_LIST
        if clean_path.startswith(f"{_MODELS_PATH}/") and len(clean_path) > len(
            _MODELS_PATH
        ) + 1:
            return RequestKind.MODEL_DETAIL
        return RequestKind.PROXY_OTHER
    if upper_method == "POST":
        return RequestKind.JSON_MODEL_OPERATION
    return RequestKind.PROXY_OTHER


def key_id_from_authorization(value: str | None) -> str | None:
    """Return a stable non-secret identifier for a Bearer credential."""

    if not value:
        return None
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:16]


def extract_model_reference(body: bytes) -> str | None:
    """Return the ``model`` string of a JSON request body, if any."""

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    model = data.get("model")
    if not isinstance(model, str) or not model:
        return None
    return model


def _openai_error_response(status_code: int, message: str) -> JSONResponse:
    """Build an OpenAI-compatible error JSON body."""

    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": "model_not_found",
            }
        },
    )


@dataclass(frozen=True)
class _FetchResult:
    """Outcome of one model detail fetch."""

    info: dict[str, Any] | None = None
    status: int | None = None
    error: BaseException | None = None


class ModelInfoCache:
    """Two-layer model information cache shared across requests.

    The common layer keeps key-independent model details with a configurable
    TTL, and the per-key layer keeps ``available_on_current_plan`` for each
    requesting credential.
    """

    def __init__(
        self,
        settings: FeatherlessConfig,
        base_url: str,
        client_factory: Callable[[], httpx.AsyncClient],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._base_url = base_url
        self._client_factory = client_factory
        self._clock = clock
        self._warm_lock = asyncio.Lock()
        self._fetched_at: float | None = None
        self._entries: dict[str, dict[str, Any]] = {}
        self._missing: set[str] = set()
        self._availability: dict[str, dict[str, bool]] = {}
        self._warned: set[str] = set()

    @property
    def whitelist(self) -> tuple[str, ...]:
        """Return the configured model whitelist in configuration order."""

        return self._settings.model_whitelist

    def is_whitelisted(self, model_id: str) -> bool:
        """Return whether the model id passes the whitelist check."""

        return model_id in self._settings.model_whitelist

    async def ensure_warm(self, authorization: str | None) -> str | None:
        """Fetch model details as needed and return the requesting key id."""

        key = key_id_from_authorization(authorization)
        if key is None:
            return None
        async with self._warm_lock:
            now = self._clock()
            common_stale = (
                self._fetched_at is None
                or now - self._fetched_at >= self._settings.cache_ttl_seconds
            )
            if not common_stale and key in self._availability:
                return key
            results = await self._fetch_all(authorization)
            if common_stale:
                self._apply_common(results)
            self._apply_availability(key, results)
            self._fetched_at = now
        return key

    async def _fetch_all(self, authorization: str | None) -> dict[str, _FetchResult]:
        semaphore = asyncio.Semaphore(_WARM_FETCH_CONCURRENCY)
        client = self._client_factory()
        try:

            async def fetch_one(model_id: str) -> tuple[str, _FetchResult]:
                async with semaphore:
                    url = build_upstream_url(
                        self._base_url,
                        f"v1/models/{quote(model_id, safe='/')}",
                        "",
                    )
                    request = client.build_request(
                        "GET",
                        url,
                        headers={"Authorization": authorization},
                    )
                    try:
                        response = await client.send(request)
                    except httpx.RequestError as error:
                        return model_id, _FetchResult(error=error)
                    try:
                        body = response.json()
                    except ValueError as error:
                        return model_id, _FetchResult(error=error)
                    if response.status_code == 200 and isinstance(body, dict):
                        return model_id, _FetchResult(info=body, status=200)
                    return model_id, _FetchResult(status=response.status_code)

            pairs = await asyncio.gather(
                *(fetch_one(model_id) for model_id in self._settings.model_whitelist)
            )
        finally:
            await client.aclose()
        return dict(pairs)

    def _apply_common(self, results: dict[str, _FetchResult]) -> None:
        entries: dict[str, dict[str, Any]] = {}
        missing: set[str] = set()
        for model_id, result in results.items():
            if result.info is not None:
                entries[model_id] = result.info
                self._warned.discard(model_id)
            elif result.status == 404:
                missing.add(model_id)
                self._warn_degraded(
                    model_id,
                    f"Featherless model {model_id} is not available upstream.",
                )
            elif model_id in self._entries:
                # Keep the previous entry when a refresh fails (plan §2.2).
                entries[model_id] = self._entries[model_id]
                self._warn_degraded(
                    model_id,
                    f"Featherless model {model_id} refresh failed; "
                    f"keeping the cached entry.",
                )
            else:
                self._warn_degraded(
                    model_id,
                    f"Featherless model {model_id} could not be fetched.",
                )
        self._entries = entries
        self._missing = missing

    def _apply_availability(
        self,
        key: str,
        results: dict[str, _FetchResult],
    ) -> None:
        availability: dict[str, bool] = {}
        for model_id, result in results.items():
            if result.info is None:
                continue
            value = result.info.get("available_on_current_plan")
            if isinstance(value, bool):
                availability[model_id] = value
        self._availability[key] = availability

    def _warn_degraded(self, model_id: str, message: str) -> None:
        # Warn once per degraded episode so refreshes stay quiet (decision C2).
        if model_id in self._warned:
            return
        self._warned.add(model_id)
        logger.warning(message)

    def _with_availability(
        self,
        model_id: str,
        info: dict[str, Any],
        key: str | None,
    ) -> dict[str, Any]:
        item = dict(info)
        if key is None:
            item.pop("available_on_current_plan", None)
            return item
        availability = self._availability.get(key, {}).get(model_id)
        if availability is None:
            item.pop("available_on_current_plan", None)
        else:
            item["available_on_current_plan"] = availability
        return item

    def models_payload(self, key: str | None) -> dict[str, Any]:
        """Build the OpenAI-style listing payload for the requesting key."""

        data = [
            self._with_availability(model_id, self._entries[model_id], key)
            for model_id in self._settings.model_whitelist
            if model_id in self._entries
        ]
        return {
            "data": data,
            "pagination": {
                "current_page": 1,
                "per_page": len(data),
                "total_items": len(data),
                "total_pages": 1,
            },
            "total": len(data),
        }

    def model_payload(
        self,
        model_id: str,
        key: str | None,
    ) -> dict[str, Any] | None:
        """Build the single-model payload, or ``None`` when unavailable."""

        info = self._entries.get(model_id)
        if info is None:
            return None
        return self._with_availability(model_id, info, key)

    def concurrency_cost(self, model_id: str) -> int:
        """Resolve the concurrency unit cost of a whitelisted model."""

        info = self._entries.get(model_id)
        value = info.get("concurrency_cost") if info is not None else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            self._warn_degraded(
                model_id,
                "Featherless model "
                f"{model_id} reports no usable concurrency_cost; "
                f"falling back to {_CONCURRENCY_COST_FALLBACK}.",
            )
            return _CONCURRENCY_COST_FALLBACK
        return value


class FeatherlessRuntime:
    """Application-scoped shared state for the featherless mode."""

    def __init__(
        self,
        config: ProxyConfig,
        upstream_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = config.listener.featherless
        if settings is None:
            raise ValueError(
                "FeatherlessRuntime requires a featherless mode configuration."
            )
        self.config = config
        self.cache = ModelInfoCache(
            settings=settings,
            base_url=config.upstream.base_url,
            client_factory=lambda: create_http_client(
                config, transport=upstream_transport
            ),
        )


async def handle_featherless_models(
    runtime: FeatherlessRuntime,
    exchange: ExchangeLog | None,
    request: Request,
) -> Response:
    """Assemble the model listing from the cache for the requesting key."""

    key = await runtime.cache.ensure_warm(request.headers.get("authorization"))
    payload = runtime.cache.models_payload(key)
    response = JSONResponse(content=payload)
    if exchange is not None:
        exchange.response_started(
            response.status_code,
            [(b"content-type", b"application/json")],
            runtime.config.upstream.base_url,
        )
        exchange.observe_response(response.body)
        exchange.complete()
    return response


async def handle_featherless_model_detail(
    runtime: FeatherlessRuntime,
    exchange: ExchangeLog | None,
    request: Request,
) -> Response:
    """Serve a single whitelisted model from the cache."""

    model_id = request.url.path[len(_MODELS_PATH) + 1 :]
    if not runtime.cache.is_whitelisted(model_id):
        message = translate(
            "The requested model is not in the whitelist of 'featherless' mode."
        )
        if exchange is not None:
            exchange.fail(
                "featherless_whitelist",
                TransformError(404, message),
            )
        return _openai_error_response(404, message)

    key = await runtime.cache.ensure_warm(request.headers.get("authorization"))
    payload = runtime.cache.model_payload(model_id, key)
    if payload is None:
        message = translate(
            "The requested model is not available in 'featherless' mode."
        )
        if exchange is not None:
            exchange.fail(
                "featherless_whitelist",
                TransformError(404, message),
            )
        return _openai_error_response(404, message)

    response = JSONResponse(content=payload)
    if exchange is not None:
        exchange.response_started(
            response.status_code,
            [(b"content-type", b"application/json")],
            runtime.config.upstream.base_url,
        )
        exchange.observe_response(response.body)
        exchange.complete()
    return response