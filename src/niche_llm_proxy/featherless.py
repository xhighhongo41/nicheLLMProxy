"""featherless.ai relay mode: model whitelisting and concurrency control."""

from __future__ import annotations

import asyncio
import contextlib
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
    "ConcurrencyGate",
    "FeatherlessRuntime",
    "GateRegistry",
    "ModelInfoCache",
    "QueueWaitTimeoutError",
    "RequestKind",
    "Reservation",
    "classify_request",
    "extract_model_reference",
    "handle_featherless_model_detail",
    "handle_featherless_models",
    "key_id_from_authorization",
    "openai_error_response",
    "queue_wait_timeout_response",
]

logger = logging.getLogger(__name__)

_WARM_FETCH_CONCURRENCY = 4
"""Upper bound of parallel model detail fetches during a warm-up pass."""

_CONCURRENCY_COST_FALLBACK = 4
"""Conservative unit cost when the upstream does not report one (decision C3)."""

_PLAN_FALLBACK_CAP = 2
"""Conservative concurrency cap while /v1/plan cannot be fetched (decision C1)."""

_MODELS_PATH = "/v1/models"

_PLAN_PATH = "v1/plan"

_CONCURRENCY_SNAPSHOT_PATH = "account/concurrency"


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


def openai_error_response(status_code: int, message: str) -> JSONResponse:
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


class QueueWaitTimeoutError(Exception):
    """Raised when a queued request exceeds its wait budget (plan §3.3)."""

    def __init__(self, wait_seconds: float) -> None:
        self.wait_seconds = wait_seconds
        super().__init__(
            f"queued request exceeded the {wait_seconds} seconds wait budget"
        )


def queue_wait_timeout_response() -> JSONResponse:
    """Build the OpenAI-compatible 429 response for an expired queue waiter."""

    message = translate(
        "The request was rejected because the wait for upstream concurrency "
        "capacity exceeded the limit, in 'featherless' mode."
    )
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "message": message,
                "type": "rate_limit_error",
                "param": None,
                "code": "concurrency_queue_timeout",
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


class Reservation:
    """An in-flight concurrency reservation granted by a gate."""

    __slots__ = ("_cost", "_gate", "_released")

    def __init__(self, gate: ConcurrencyGate, cost: int) -> None:
        self._gate = gate
        self._cost = cost
        self._released = False

    @property
    def cost(self) -> int:
        """Return the reserved concurrency units."""

        return self._cost

    @property
    def released(self) -> bool:
        """Return whether this reservation was already released."""

        return self._released

    def release(self) -> None:
        """Release the reserved units and wake queued waiters."""

        if self._released:
            return
        self._released = True
        self._gate._release(self)


class _Waiter:
    """A queued acquire request waiting for capacity."""

    __slots__ = ("cost", "deadline", "event", "reservation")

    def __init__(self, cost: int, deadline: float) -> None:
        self.cost = cost
        self.deadline = deadline
        self.event = asyncio.Event()
        self.reservation: Reservation | None = None


class ConcurrencyGate:
    """Per-credential concurrency gate with a FIFO wait queue (plan §3.3).

    The effective usage is ``max(local_reserved, upstream_used)``: the local
    reservations cover the reporting lag of the upstream snapshot, and the
    upstream snapshot covers consumption that bypasses this proxy.
    """

    def __init__(
        self,
        *,
        key_id: str,
        authorization: str,
        settings: FeatherlessConfig,
        base_url: str,
        client_factory: Callable[[], httpx.AsyncClient],
        clock: Callable[[], float] = time.monotonic,
        poll_interval_seconds: float = 1.0,
        poll_idle_stop_seconds: float = 30.0,
    ) -> None:
        self._key_id = key_id
        self._authorization = authorization
        self._settings = settings
        self._base_url = base_url
        self._client_factory = client_factory
        self._clock = clock
        self._poll_interval = poll_interval_seconds
        self._poll_idle_stop = poll_idle_stop_seconds
        self._cap: int | None = None
        self._cap_source: str | None = None
        self._reserved = 0
        self._upstream_used: int | None = None
        self._queue: list[_Waiter] = []
        self._poll_task: asyncio.Task[None] | None = None
        self._last_activity = clock()
        self._plan_warned = False
        self._poll_warned = False

    @property
    def key_id(self) -> str:
        """Return the credential identifier this gate belongs to."""

        return self._key_id

    @property
    def cap(self) -> int | None:
        """Return the resolved concurrency cap."""

        return self._cap

    @property
    def reserved(self) -> int:
        """Return the locally reserved concurrency units."""

        return self._reserved

    @property
    def upstream_used(self) -> int | None:
        """Return the last upstream used_cost snapshot."""

        return self._upstream_used

    @property
    def queue_length(self) -> int:
        """Return the number of queued waiters."""

        return len(self._queue)

    @property
    def poll_running(self) -> bool:
        """Return whether the upstream snapshot poll loop is running."""

        return self._poll_task is not None and not self._poll_task.done()

    def effective_used(self) -> int:
        """Return the conservative effective usage (plan §2.2)."""

        return max(self._reserved, self._upstream_used or 0)

    def note_activity(self) -> None:
        """Record activity for the idle detection."""

        self._last_activity = self._clock()

    def is_disposable(self, now: float, idle_seconds: float) -> bool:
        """Return whether the gate may be swept (decisions C14/C15)."""

        return (
            self._reserved == 0
            and not self._queue
            and not self.poll_running
            and now - self._last_activity >= idle_seconds
        )

    async def acquire(self, cost: int) -> Reservation:
        """Reserve ``cost`` units, queueing until capacity or timeout."""

        self.note_activity()
        await self._ensure_cap()
        deadline = self._clock() + self._settings.max_queue_wait_seconds
        while True:
            reservation = self._try_reserve(cost)
            if reservation is not None:
                return reservation
            waiter = _Waiter(cost, deadline)
            self._queue.append(waiter)
            remaining = deadline - self._clock()
            if remaining <= 0:
                self._remove_waiter(waiter)
                raise QueueWaitTimeoutError(self._settings.max_queue_wait_seconds)
            try:
                await asyncio.wait_for(waiter.event.wait(), timeout=remaining)
            except TimeoutError:
                self._handle_waiter_exit(waiter)
                raise QueueWaitTimeoutError(self._settings.max_queue_wait_seconds)
            except asyncio.CancelledError:
                self._handle_waiter_exit(waiter)
                raise
            if waiter.reservation is not None:
                self._remove_waiter(waiter)
                return waiter.reservation
            # Defensive: a wake without a reservation retries the loop.
            self._remove_waiter(waiter)

    async def close(self) -> None:
        """Stop the snapshot poll loop."""

        task, self._poll_task = self._poll_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _try_reserve(self, cost: int) -> Reservation | None:
        cap = self._cap
        if cap is None or self.effective_used() + cost > cap:
            return None
        self._reserved += cost
        self.note_activity()
        self._ensure_polling()
        return Reservation(self, cost)

    def _release(self, reservation: Reservation) -> None:
        self._reserved -= reservation.cost
        self.note_activity()
        self._wake_queue()

    def _wake_queue(self) -> None:
        cap = self._cap
        if cap is None:
            return
        for waiter in list(self._queue):
            if waiter.reservation is not None:
                # Already woken and holding a reservation.
                continue
            if self._clock() >= waiter.deadline:
                # Expiring waiters leave on their own timeout.
                continue
            if self.effective_used() + waiter.cost <= cap:
                self._reserved += waiter.cost
                waiter.reservation = Reservation(self, waiter.cost)
                waiter.event.set()
            # Skip queueing: a blocked waiter does not stop later waiters
            # that fit the remaining budget. Starvation of skipped waiters
            # is bounded by their own deadline (429 QueueWaitTimeoutError).

    def _remove_waiter(self, waiter: _Waiter) -> None:
        if waiter in self._queue:
            self._queue.remove(waiter)

    def _handle_waiter_exit(self, waiter: _Waiter) -> None:
        self._remove_waiter(waiter)
        if waiter.reservation is not None:
            waiter.reservation.release()

    def update_upstream_used(self, used_cost: int) -> None:
        """Apply a fresh upstream used_cost snapshot and wake the queue."""

        self._upstream_used = used_cost
        self._wake_queue()

    def _ensure_polling(self) -> None:
        if not self.poll_running:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            if self._reserved > 0 or self._queue:
                self.note_activity()
                await self._poll_once()
            elif self._clock() - self._last_activity >= self._poll_idle_stop:
                return

    async def _poll_once(self) -> None:
        body = await self._fetch_json(_CONCURRENCY_SNAPSHOT_PATH)
        if isinstance(body, dict):
            used_cost = body.get("used_cost")
            if (
                isinstance(used_cost, int)
                and not isinstance(used_cost, bool)
                and used_cost >= 0
            ):
                self._poll_warned = False
                self.update_upstream_used(used_cost)
                return
        # Keep the previous snapshot and warn once per degraded episode.
        if not self._poll_warned:
            self._poll_warned = True
            logger.warning(
                "Featherless concurrency snapshot for key %s failed; "
                "keeping used_cost=%s.",
                self._key_id,
                self._upstream_used,
            )

    async def _ensure_cap(self) -> None:
        if self._settings.concurrency_limit is not None:
            self._cap = self._settings.concurrency_limit
            self._cap_source = "config"
            return
        if self._cap is not None and self._cap_source == "plan":
            return
        body = await self._fetch_json(_PLAN_PATH)
        concurrency = body.get("concurrency") if isinstance(body, dict) else None
        if (
            isinstance(concurrency, int)
            and not isinstance(concurrency, bool)
            and concurrency >= 1
        ):
            self._cap = concurrency
            self._cap_source = "plan"
            self._plan_warned = False
            return
        # Decision C1: conservative cap, retried on the next request.
        self._cap = _PLAN_FALLBACK_CAP
        self._cap_source = "fallback"
        if not self._plan_warned:
            self._plan_warned = True
            logger.warning(
                "Featherless plan lookup for key %s failed; "
                "falling back to concurrency cap %d.",
                self._key_id,
                _PLAN_FALLBACK_CAP,
            )

    async def _fetch_json(self, path: str) -> Any:
        client = self._client_factory()
        try:
            url = build_upstream_url(self._base_url, path, "")
            request = client.build_request(
                "GET",
                url,
                headers={"Authorization": self._authorization},
            )
            response = await client.send(request)
            try:
                return response.json()
            except ValueError:
                return None
        except httpx.RequestError:
            return None
        finally:
            await client.aclose()


class GateRegistry:
    """Per-credential gate registry with idle sweeping (decisions C14/C15)."""

    def __init__(
        self,
        *,
        settings: FeatherlessConfig,
        base_url: str,
        client_factory: Callable[[], httpx.AsyncClient],
        clock: Callable[[], float] = time.monotonic,
        poll_interval_seconds: float = 1.0,
        poll_idle_stop_seconds: float = 30.0,
        sweep_interval_seconds: float = 300.0,
        gate_idle_dispose_seconds: float = 600.0,
    ) -> None:
        self._settings = settings
        self._base_url = base_url
        self._client_factory = client_factory
        self._clock = clock
        self._poll_interval = poll_interval_seconds
        self._poll_idle_stop = poll_idle_stop_seconds
        self._sweep_interval = sweep_interval_seconds
        self._gate_idle_dispose = gate_idle_dispose_seconds
        self._gates: dict[str, ConcurrencyGate] = {}
        self._sweep_task: asyncio.Task[None] | None = None

    @property
    def gates(self) -> dict[str, ConcurrencyGate]:
        """Return a copy of the live gates keyed by credential id."""

        return dict(self._gates)

    @property
    def sweep_running(self) -> bool:
        """Return whether the idle sweeper is running."""

        return self._sweep_task is not None and not self._sweep_task.done()

    def gate_for(self, authorization: str | None) -> ConcurrencyGate | None:
        """Return the gate for a credential, creating it on first sight."""

        key = key_id_from_authorization(authorization)
        if key is None:
            return None
        gate = self._gates.get(key)
        if gate is None:
            gate = ConcurrencyGate(
                key_id=key,
                authorization=authorization,
                settings=self._settings,
                base_url=self._base_url,
                client_factory=self._client_factory,
                clock=self._clock,
                poll_interval_seconds=self._poll_interval,
                poll_idle_stop_seconds=self._poll_idle_stop,
            )
            self._gates[key] = gate
        gate.note_activity()
        return gate

    async def start(self) -> None:
        """Start the idle sweeper."""

        if not self.sweep_running:
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def close(self) -> None:
        """Stop the sweeper and every gate."""

        task, self._sweep_task = self._sweep_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for gate in list(self._gates.values()):
            await gate.close()
        self._gates.clear()

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval)
            await self._sweep(self._clock())

    async def _sweep(self, now: float) -> None:
        for key, gate in list(self._gates.items()):
            if gate.is_disposable(now, self._gate_idle_dispose):
                await gate.close()
                del self._gates[key]


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
        self.gates = GateRegistry(
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
        return openai_error_response(404, message)

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
        return openai_error_response(404, message)

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