"""Tests for the featherless mode: model cache and whitelist handling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request

from niche_llm_proxy.config import FeatherlessConfig, ProxyConfig, load_config
from niche_llm_proxy.featherless import (
    ConcurrencyGate,
    FeatherlessRuntime,
    GateRegistry,
    ModelInfoCache,
    QueueWaitTimeoutError,
    RequestKind,
    classify_request,
    extract_model_reference,
    handle_featherless_model_detail,
    handle_featherless_models,
    key_id_from_authorization,
    queue_wait_timeout_response,
)

KIMI = "moonshotai/Kimi-K2.6"
QWEN = "Qwen/Qwen3-32B"
MISSING = "missing/model-7b"
BASE_URL = "https://api.featherless.ai"
AUTH1 = "Bearer credential-one"
AUTH2 = "Bearer credential-two"
AUTH1_ID = hashlib.sha256(b"credential-one").hexdigest()[:16]
AUTH2_ID = hashlib.sha256(b"credential-two").hexdigest()[:16]


def _detail_body(
    model_id: str,
    *,
    concurrency_cost: int | None = 4,
    available_on_current_plan: bool = True,
) -> dict[str, Any]:
    """Return a representative featherless model detail response body."""
    body: dict[str, Any] = {
        "id": model_id,
        "name": model_id,
        "model_class": "example",
        "context_length": 262144,
        "max_completion_tokens": 8192,
        "available_on_current_plan": available_on_current_plan,
        "availability": {"tier": "warm"},
        "pricing": {"prompt": "0.0000015", "completion": "0.000002"},
    }
    if concurrency_cost is not None:
        body["concurrency_cost"] = concurrency_cost
    return body


class _FakeFeatherlessUpstream:
    """Serve model detail responses while recording the received requests."""

    def __init__(
        self,
        details: dict[str, dict[str, Any]] | None = None,
        *,
        plan_concurrency: int = 8,
        used_cost: int = 0,
    ) -> None:
        self.details = details or {}
        self.plan_concurrency = plan_concurrency
        self.used_cost = used_cost
        self.requests: list[tuple[str, str, str | None]] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            (
                request.method,
                request.url.path,
                request.headers.get("authorization"),
            )
        )
        if self.fail:
            return httpx.Response(503, request=request)
        path = request.url.path
        if path == "/v1/plan":
            return httpx.Response(
                200,
                json={
                    "id": "feather_claw_pro",
                    "name": "Feather Agent Pro",
                    "max_context_length": 262144,
                    "concurrency": self.plan_concurrency,
                },
                request=request,
            )
        if path == "/account/concurrency":
            return httpx.Response(
                200,
                json={
                    "limit": self.plan_concurrency,
                    "used_cost": self.used_cost,
                    "request_count": 0,
                    "requests": [],
                },
                request=request,
            )
        if path.startswith("/v1/models/"):
            model_id = path[len("/v1/models/") :]
            if model_id in self.details:
                return httpx.Response(200, json=self.details[model_id], request=request)
            return httpx.Response(404, request=request)
        return httpx.Response(404, request=request)

    @property
    def request_count(self) -> int:
        return len(self.requests)


def _make_cache(
    upstream: _FakeFeatherlessUpstream,
    *,
    settings: FeatherlessConfig | None = None,
    clock: Callable[[], float] | None = None,
) -> ModelInfoCache:
    """Build a model info cache wired to the fake upstream."""
    options: dict[str, Any] = {}
    if clock is not None:
        options["clock"] = clock
    return ModelInfoCache(
        settings=settings
        or FeatherlessConfig(model_whitelist=(KIMI, QWEN, MISSING)),
        base_url=BASE_URL,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handler),
        ),
        **options,
    )


def _request(
    method: str = "GET",
    path: str = "/v1/models",
    headers: dict[str, str] | None = None,
) -> Request:
    """Build a minimal ASGI request for handler tests."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [
            (name.lower().encode("ascii"), value.encode("utf-8"))
            for name, value in (headers or {}).items()
        ],
    }
    return Request(scope)


def _featherless_config(
    write_config: Callable[[dict[str, object] | None], Path],
    *,
    model_whitelist: tuple[str, ...] = (KIMI, QWEN),
    **featherless: object,
) -> ProxyConfig:
    """Return valid featherless mode configuration."""
    listener_featherless: dict[str, object] = {"model_whitelist": list(model_whitelist)}
    listener_featherless.update(featherless)
    return load_config(
        write_config(
            {
                "listener": {
                    "port": 8000,
                    "mode": "featherless",
                    "featherless": listener_featherless,
                },
                "upstream": {"base_url": BASE_URL},
            }
        )
    )


class TestClassifyRequest:
    """Route and method classification for the featherless mode."""

    @pytest.mark.parametrize(
        ("path", "method", "expected"),
        [
            ("/v1/models", "GET", RequestKind.MODELS_LIST),
            ("/v1/models?per_page=100", "GET", RequestKind.MODELS_LIST),
            (f"/v1/models/{KIMI}", "GET", RequestKind.MODEL_DETAIL),
            (f"/v1/models/{KIMI}?x=1", "GET", RequestKind.MODEL_DETAIL),
            ("/v1/chat/completions", "POST", RequestKind.JSON_MODEL_OPERATION),
            ("/v1/models", "POST", RequestKind.JSON_MODEL_OPERATION),
            ("/v1/embeddings", "POST", RequestKind.JSON_MODEL_OPERATION),
            ("/v1/chat/completions", "GET", RequestKind.PROXY_OTHER),
            ("/v1/models", "DELETE", RequestKind.PROXY_OTHER),
            ("/health", "GET", RequestKind.PROXY_OTHER),
        ],
    )
    def test_classify_request(
        self, path: str, method: str, expected: RequestKind
    ) -> None:
        """Classify every supported path/method combination."""
        assert classify_request(path, method) is expected

    def test_classify_request_is_case_insensitive_for_method(self) -> None:
        """Normalize the HTTP method to upper case."""
        assert classify_request("/v1/models", "get") is RequestKind.MODELS_LIST


class TestKeyIdFromAuthorization:
    """Non-secret key identifiers derived from Authorization headers."""

    @pytest.mark.parametrize("value", [None, "", "Bearer", "Bearer   ", "Basic xyz"])
    def test_unusable_authorization_maps_to_none(self, value: str | None) -> None:
        """Missing or non-Bearer credentials do not produce a key id."""
        assert key_id_from_authorization(value) is None

    def test_bearer_token_maps_to_hash_prefix(self) -> None:
        """A Bearer token maps to the first 16 hex digits of its SHA-256."""
        expected = hashlib.sha256(b"secret-key").hexdigest()[:16]
        assert key_id_from_authorization("Bearer secret-key") == expected

    def test_scheme_is_case_insensitive(self) -> None:
        """The Authorization scheme is compared without case sensitivity."""
        expected = hashlib.sha256(b"secret-key").hexdigest()[:16]
        assert key_id_from_authorization("bearer secret-key") == expected

    def test_different_tokens_produce_different_ids(self) -> None:
        """Distinct tokens never collide in their key ids."""
        first = key_id_from_authorization("Bearer one")
        second = key_id_from_authorization("Bearer two")
        assert first is not None
        assert second is not None
        assert first != second


class TestExtractModelReference:
    """Model extraction from JSON request bodies."""

    def test_json_object_with_string_model(self) -> None:
        """A JSON body with a string model field yields the model id."""
        body = json.dumps({"model": KIMI, "messages": []}).encode("utf-8")
        assert extract_model_reference(body) == KIMI

    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"",
            b"[1, 2]",
            b'{"prompt": "a cat"}',
            b'{"model": 123}',
            b'{"model": null}',
            b'{"model": ""}',
        ],
    )
    def test_unusable_bodies_yield_none(self, body: bytes) -> None:
        """Non-JSON bodies or non-string model fields yield no model."""
        assert extract_model_reference(body) is None


class TestModelInfoCache:
    """Two-layer model information cache with TTL handling."""

    @pytest.mark.anyio
    async def test_warm_fetch_populates_both_layers(self) -> None:
        """The first key fetches every whitelisted model with its own credentials."""
        upstream = _FakeFeatherlessUpstream(
            details={
                KIMI: _detail_body(KIMI, concurrency_cost=4, available_on_current_plan=True),
                QWEN: _detail_body(QWEN, concurrency_cost=2, available_on_current_plan=False),
            }
        )
        cache = _make_cache(upstream)

        key = await cache.ensure_warm("Bearer key-a")

        expected_key = hashlib.sha256(b"key-a").hexdigest()[:16]
        assert key == expected_key
        assert upstream.request_count == 3
        assert all(
            authorization == "Bearer key-a"
            for _, _, authorization in upstream.requests
        )
        payload = cache.models_payload(key)
        assert [item["id"] for item in payload["data"]] == [KIMI, QWEN]
        assert payload["total"] == 2
        assert payload["pagination"] == {
            "current_page": 1,
            "per_page": 2,
            "total_items": 2,
            "total_pages": 1,
        }
        availability = {
            item["id"]: item["available_on_current_plan"] for item in payload["data"]
        }
        assert availability == {KIMI: True, QWEN: False}

    @pytest.mark.anyio
    async def test_same_key_within_ttl_does_not_refetch(self) -> None:
        """A warm cache for the same key issues no further upstream requests."""
        upstream = _FakeFeatherlessUpstream(
            details={KIMI: _detail_body(KIMI), QWEN: _detail_body(QWEN)}
        )
        cache = _make_cache(upstream)

        await cache.ensure_warm("Bearer key-a")
        await cache.ensure_warm("Bearer key-a")

        assert upstream.request_count == 3

    @pytest.mark.anyio
    async def test_second_key_refetches_availability_per_key(self) -> None:
        """A new key refreshes only its availability layer while the common layer is fresh."""
        upstream = _FakeFeatherlessUpstream(
            details={KIMI: _detail_body(KIMI), QWEN: _detail_body(QWEN)}
        )
        cache = _make_cache(upstream)

        first = await cache.ensure_warm("Bearer key-a")
        second = await cache.ensure_warm("Bearer key-b")

        assert upstream.request_count == 6
        authorizations = [authorization for _, _, authorization in upstream.requests]
        assert authorizations[:3] == ["Bearer key-a"] * 3
        assert authorizations[3:] == ["Bearer key-b"] * 3
        first_ids = {item["id"] for item in cache.models_payload(first)["data"]}
        second_ids = {item["id"] for item in cache.models_payload(second)["data"]}
        assert first_ids == second_ids == {KIMI, QWEN}

    @pytest.mark.anyio
    async def test_ttl_expiry_refetches(self) -> None:
        """A stale common layer is refreshed with the requesting key's credentials."""
        upstream = _FakeFeatherlessUpstream(
            details={KIMI: _detail_body(KIMI), QWEN: _detail_body(QWEN)}
        )
        now = {"value": 0.0}
        cache = _make_cache(upstream, clock=lambda: now["value"])

        await cache.ensure_warm("Bearer key-a")
        assert upstream.request_count == 3
        now["value"] = 301.0
        await cache.ensure_warm("Bearer key-a")
        assert upstream.request_count == 6

    @pytest.mark.anyio
    async def test_missing_model_is_excluded_and_warned_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A model the upstream does not serve is excluded and logged once."""
        upstream = _FakeFeatherlessUpstream(
            details={KIMI: _detail_body(KIMI), QWEN: _detail_body(QWEN)}
        )
        now = {"value": 0.0}
        cache = _make_cache(upstream, clock=lambda: now["value"])

        key = await cache.ensure_warm("Bearer key-a")
        with caplog.at_level("WARNING"):
            payload = cache.models_payload(key)
        assert [item["id"] for item in payload["data"]] == [KIMI, QWEN]
        warnings = [record for record in caplog.records if MISSING in record.getMessage()]
        assert len(warnings) == 1

        now["value"] = 301.0
        with caplog.at_level("WARNING"):
            await cache.ensure_warm("Bearer key-a")
        warnings = [record for record in caplog.records if MISSING in record.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.anyio
    async def test_missing_model_recovers_after_refresh(self) -> None:
        """A model that appears upstream later returns to the payload."""
        upstream = _FakeFeatherlessUpstream(details={KIMI: _detail_body(KIMI)})
        now = {"value": 0.0}
        cache = _make_cache(upstream, clock=lambda: now["value"])

        key = await cache.ensure_warm("Bearer key-a")
        assert cache.model_payload(QWEN, key) is None
        upstream.details[QWEN] = _detail_body(QWEN)
        now["value"] = 301.0
        await cache.ensure_warm("Bearer key-a")
        assert cache.model_payload(QWEN, key) is not None

    @pytest.mark.anyio
    async def test_fetch_failure_keeps_previous_entries(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing refresh keeps the cached entries instead of clearing them."""
        upstream = _FakeFeatherlessUpstream(
            details={KIMI: _detail_body(KIMI), QWEN: _detail_body(QWEN)}
        )
        now = {"value": 0.0}
        cache = _make_cache(upstream, clock=lambda: now["value"])

        key = await cache.ensure_warm("Bearer key-a")
        upstream.fail = True
        now["value"] = 301.0
        with caplog.at_level("WARNING"):
            await cache.ensure_warm("Bearer key-a")

        payload = cache.models_payload(key)
        assert [item["id"] for item in payload["data"]] == [KIMI, QWEN]

    @pytest.mark.anyio
    async def test_unauthenticated_request_never_fetches(self) -> None:
        """Without credentials no upstream fetch happens and availability is omitted."""
        upstream = _FakeFeatherlessUpstream(details={KIMI: _detail_body(KIMI)})
        cache = _make_cache(upstream)

        key = await cache.ensure_warm(None)

        assert key is None
        assert upstream.request_count == 0
        payload = cache.models_payload(None)
        assert payload["data"] == []
        assert payload["total"] == 0

    @pytest.mark.anyio
    async def test_unauthenticated_payload_omits_availability(self) -> None:
        """Unauthenticated listings show cached models without plan availability."""
        upstream = _FakeFeatherlessUpstream(details={KIMI: _detail_body(KIMI)})
        cache = _make_cache(upstream)

        await cache.ensure_warm("Bearer key-a")
        payload = cache.models_payload(None)

        assert [item["id"] for item in payload["data"]] == [KIMI]
        for item in payload["data"]:
            assert "available_on_current_plan" not in item

    def test_concurrency_cost_uses_upstream_value(self) -> None:
        """Reported costs come from the cached model detail."""
        cache = _make_cache(_FakeFeatherlessUpstream())
        assert cache.concurrency_cost(KIMI) == 4

    def test_concurrency_cost_falls_back_to_maximum(self) -> None:
        """Unknown models fall back to the maximum conservative cost."""
        cache = _make_cache(_FakeFeatherlessUpstream())
        assert cache.concurrency_cost("unknown/model") == 4

    def test_is_whitelisted(self) -> None:
        """Only configured model ids pass the whitelist check."""
        cache = _make_cache(_FakeFeatherlessUpstream())
        assert cache.is_whitelisted(KIMI) is True
        assert cache.is_whitelisted("other/model") is False


class TestFeatherlessModelsHandler:
    """Proxy-assembled GET /v1/models responses."""

    @pytest.mark.anyio
    async def test_returns_assembled_payload_for_requesting_key(
        self, write_config: Callable[[dict[str, object] | None], Path]
    ) -> None:
        """The listing is assembled from the cache for the requesting key."""
        upstream = _FakeFeatherlessUpstream(
            details={
                KIMI: _detail_body(KIMI, available_on_current_plan=True),
                QWEN: _detail_body(QWEN, concurrency_cost=2, available_on_current_plan=False),
            }
        )
        runtime = FeatherlessRuntime(
            _featherless_config(write_config),
            httpx.MockTransport(upstream.handler),
        )
        request = _request(
            "GET",
            "/v1/models?ignored=1",
            headers={"Authorization": "Bearer key-a"},
        )

        response = await handle_featherless_models(runtime, None, request)

        assert response.status_code == 200
        payload = json.loads(response.body.decode("utf-8"))
        assert [item["id"] for item in payload["data"]] == [KIMI, QWEN]
        availability = {
            item["id"]: item["available_on_current_plan"] for item in payload["data"]
        }
        assert availability == {KIMI: True, QWEN: False}
        assert payload["pagination"]["per_page"] == 2
        assert upstream.request_count == 2

    @pytest.mark.anyio
    async def test_unauthenticated_returns_empty_listing(
        self, write_config: Callable[[dict[str, object] | None], Path]
    ) -> None:
        """Without credentials the listing stays empty and no fetch happens."""
        upstream = _FakeFeatherlessUpstream(details={KIMI: _detail_body(KIMI)})
        runtime = FeatherlessRuntime(
            _featherless_config(write_config),
            httpx.MockTransport(upstream.handler),
        )

        response = await handle_featherless_models(runtime, None, _request())

        assert response.status_code == 200
        payload = json.loads(response.body.decode("utf-8"))
        assert payload["data"] == []
        assert payload["total"] == 0
        assert upstream.request_count == 0


class TestFeatherlessModelDetailHandler:
    """Proxy-assembled GET /v1/models/{model_id} responses."""

    @pytest.mark.anyio
    async def test_returns_cached_detail(
        self, write_config: Callable[[dict[str, object] | None], Path]
    ) -> None:
        """A whitelisted model is served from the cache."""
        upstream = _FakeFeatherlessUpstream(
            details={KIMI: _detail_body(KIMI), QWEN: _detail_body(QWEN)}
        )
        runtime = FeatherlessRuntime(
            _featherless_config(write_config),
            httpx.MockTransport(upstream.handler),
        )
        request = _request(
            "GET",
            f"/v1/models/{KIMI}",
            headers={"Authorization": "Bearer key-a"},
        )

        response = await handle_featherless_model_detail(runtime, None, request)

        assert response.status_code == 200
        payload = json.loads(response.body.decode("utf-8"))
        assert payload["id"] == KIMI
        assert payload["available_on_current_plan"] is True

    @pytest.mark.anyio
    async def test_rejects_model_outside_whitelist(
        self, write_config: Callable[[dict[str, object] | None], Path]
    ) -> None:
        """A model outside the whitelist is rejected with an OpenAI-style error."""
        runtime = FeatherlessRuntime(
            _featherless_config(write_config),
            httpx.MockTransport(_FakeFeatherlessUpstream().handler),
        )
        request = _request("GET", "/v1/models/unknown/model")

        response = await handle_featherless_model_detail(runtime, None, request)

        assert response.status_code == 404
        payload = json.loads(response.body.decode("utf-8"))
        assert payload["error"]["code"] == "model_not_found"
        assert payload["error"]["type"] == "invalid_request_error"
        assert payload["error"]["param"] is None

    @pytest.mark.anyio
    async def test_rejects_whitelisted_model_missing_upstream(
        self, write_config: Callable[[dict[str, object] | None], Path]
    ) -> None:
        """A whitelisted model the upstream does not serve is rejected."""
        upstream = _FakeFeatherlessUpstream(details={KIMI: _detail_body(KIMI)})
        runtime = FeatherlessRuntime(
            _featherless_config(write_config, model_whitelist=(KIMI, QWEN)),
            httpx.MockTransport(upstream.handler),
        )
        request = _request(
            "GET",
            f"/v1/models/{QWEN}",
            headers={"Authorization": "Bearer key-a"},
        )

        response = await handle_featherless_model_detail(runtime, None, request)

        assert response.status_code == 404
        payload = json.loads(response.body.decode("utf-8"))
        assert payload["error"]["code"] == "model_not_found"


QUEUE_TIMEOUT_MESSAGE = (
    "The request was rejected because the wait for upstream concurrency "
    "capacity exceeded the limit, in 'featherless' mode."
)


def _gate_settings(**overrides: object) -> FeatherlessConfig:
    """Return featherless settings tuned for gate tests."""

    settings: dict[str, object] = {
        "model_whitelist": (KIMI,),
        "concurrency_limit": 8,
        "max_queue_wait_seconds": 1.0,
    }
    settings.update(overrides)
    return FeatherlessConfig(**settings)  # type: ignore[arg-type]


def _make_gate(
    upstream: _FakeFeatherlessUpstream,
    *,
    settings: FeatherlessConfig | None = None,
    poll_interval_seconds: float = 0.01,
    poll_idle_stop_seconds: float = 0.05,
) -> ConcurrencyGate:
    """Build a concurrency gate wired to the fake upstream."""

    return ConcurrencyGate(
        key_id=AUTH1_ID,
        authorization=AUTH1,
        settings=settings or _gate_settings(),
        base_url=BASE_URL,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handler),
        ),
        poll_interval_seconds=poll_interval_seconds,
        poll_idle_stop_seconds=poll_idle_stop_seconds,
    )


def _make_registry(
    upstream: _FakeFeatherlessUpstream,
    *,
    settings: FeatherlessConfig | None = None,
    **kwargs: object,
) -> GateRegistry:
    """Build a gate registry wired to the fake upstream."""

    defaults: dict[str, object] = {
        "poll_interval_seconds": 0.01,
        "poll_idle_stop_seconds": 0.05,
        "sweep_interval_seconds": 0.02,
        "gate_idle_dispose_seconds": 0.05,
    }
    defaults.update(kwargs)
    return GateRegistry(
        settings=settings or _gate_settings(),
        base_url=BASE_URL,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handler),
        ),
        **defaults,  # type: ignore[arg-type]
    )


async def _wait_for(
    condition: Callable[[], bool],
    *,
    timeout: float = 2.0,
    interval: float = 0.005,
) -> None:
    """Await a condition or fail when it does not become true in time."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition was not met within the timeout")


@pytest.fixture
async def make_gate() -> Any:
    """Provide a gate factory that closes every created gate afterwards."""

    gates: list[ConcurrencyGate] = []
    try:

        def factory(
            upstream: _FakeFeatherlessUpstream,
            **kwargs: object,
        ) -> ConcurrencyGate:
            gate = _make_gate(upstream, **kwargs)  # type: ignore[arg-type]
            gates.append(gate)
            return gate

        yield factory
    finally:
        for gate in gates:
            await gate.close()


class TestQueueWaitTimeoutResponse:
    """The 429 response for an expired queue waiter."""

    def test_returns_openai_compatible_error(self) -> None:
        """The response is an OpenAI-style 429 error payload."""

        response = queue_wait_timeout_response()

        assert response.status_code == 429
        payload = json.loads(response.body.decode("utf-8"))
        assert payload == {
            "error": {
                "message": QUEUE_TIMEOUT_MESSAGE,
                "type": "rate_limit_error",
                "param": None,
                "code": "concurrency_queue_timeout",
            }
        }


class TestConcurrencyGateCap:
    """Cap resolution from config, /v1/plan and the C1 fallback."""

    @pytest.mark.anyio
    async def test_uses_configured_limit_without_plan_lookup(
        self, make_gate: Any
    ) -> None:
        """A configured limit wins and /v1/plan is never called."""

        upstream = _FakeFeatherlessUpstream()
        gate = make_gate(upstream, settings=_gate_settings(concurrency_limit=5))

        reservation = await gate.acquire(2)

        assert gate.cap == 5
        assert gate.reserved == 2
        assert all(
            path == "/account/concurrency" for _, path, _ in upstream.requests
        )
        reservation.release()

    @pytest.mark.anyio
    async def test_resolves_cap_from_plan_lookup(self, make_gate: Any) -> None:
        """Without a configured limit the cap comes from /v1/plan."""

        upstream = _FakeFeatherlessUpstream(plan_concurrency=8)
        gate = make_gate(
            upstream,
            settings=_gate_settings(concurrency_limit=None, max_queue_wait_seconds=0.2),
        )

        reservation = await gate.acquire(4)

        assert gate.cap == 8
        assert gate.reserved == 4
        assert ("GET", "/v1/plan", AUTH1) in upstream.requests
        reservation.release()

    @pytest.mark.anyio
    async def test_falls_back_to_conservative_cap_when_plan_fails(
        self, make_gate: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing plan lookup falls back to cap 2 with one warning (C1)."""

        upstream = _FakeFeatherlessUpstream()
        upstream.fail = True
        gate = make_gate(
            upstream,
            settings=_gate_settings(concurrency_limit=None, max_queue_wait_seconds=0.2),
        )

        reservation = await gate.acquire(1)

        assert gate.cap == 2
        assert gate.reserved == 1
        assert any("falling back" in record.message for record in caplog.records)
        reservation.release()

    @pytest.mark.anyio
    async def test_recovers_plan_cap_after_fallback(self, make_gate: Any) -> None:
        """After a fallback the gate retries the plan lookup and recovers."""

        upstream = _FakeFeatherlessUpstream()
        upstream.fail = True
        gate = make_gate(
            upstream,
            settings=_gate_settings(concurrency_limit=None, max_queue_wait_seconds=0.2),
        )
        first = await gate.acquire(1)
        assert gate.cap == 2
        first.release()

        upstream.fail = False
        second = await gate.acquire(1)

        assert gate.cap == 8
        second.release()


class TestConcurrencyGateAcquire:
    """Reservations, queueing, timeouts and cancellation."""

    @pytest.mark.anyio
    async def test_reserves_units_up_to_cap(self, make_gate: Any) -> None:
        """Reservations accumulate up to the configured cap."""

        gate = make_gate(_FakeFeatherlessUpstream())

        first = await gate.acquire(4)
        second = await gate.acquire(4)

        assert gate.reserved == 8
        assert gate.effective_used() == 8
        assert gate.queue_length == 0
        first.release()
        second.release()
        assert gate.reserved == 0

    @pytest.mark.anyio
    async def test_queues_when_cap_is_exhausted(self, make_gate: Any) -> None:
        """A request beyond the cap waits in the queue."""

        gate = make_gate(_FakeFeatherlessUpstream())
        reservation = await gate.acquire(8)

        task = asyncio.create_task(gate.acquire(1))
        await _wait_for(lambda: gate.queue_length == 1)

        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gate.queue_length == 0
        assert gate.reserved == 8
        reservation.release()

    @pytest.mark.anyio
    async def test_times_out_queued_waiter(self, make_gate: Any) -> None:
        """An expired waiter raises QueueWaitTimeoutError and leaves the queue."""

        gate = make_gate(
            _FakeFeatherlessUpstream(),
            settings=_gate_settings(max_queue_wait_seconds=0.05),
        )
        reservation = await gate.acquire(8)

        with pytest.raises(QueueWaitTimeoutError) as excinfo:
            await gate.acquire(1)

        assert excinfo.value.wait_seconds == 0.05
        assert gate.queue_length == 0
        assert gate.reserved == 8
        reservation.release()

    @pytest.mark.anyio
    async def test_wakes_waiters_in_fifo_order(self, make_gate: Any) -> None:
        """Released capacity goes to the earliest waiter first."""

        gate = make_gate(_FakeFeatherlessUpstream())
        head_reservation = await gate.acquire(4)
        tail_reservation = await gate.acquire(4)
        first = asyncio.create_task(gate.acquire(4))
        await _wait_for(lambda: gate.queue_length == 1)
        second = asyncio.create_task(gate.acquire(2))
        await _wait_for(lambda: gate.queue_length == 2)

        tail_reservation.release()
        first_reservation = await asyncio.wait_for(first, 1.0)
        assert not second.done()
        assert gate.queue_length == 1

        first_reservation.release()
        second_reservation = await asyncio.wait_for(second, 1.0)
        assert gate.reserved == 6
        head_reservation.release()
        second_reservation.release()

    @pytest.mark.anyio
    async def test_head_of_line_blocks_smaller_later_waiters(
        self, make_gate: Any
    ) -> None:
        """A blocked head waiter keeps smaller later waiters queued (C7)."""

        upstream = _FakeFeatherlessUpstream(used_cost=7)
        gate = make_gate(upstream, settings=_gate_settings(max_queue_wait_seconds=5.0))
        reservation = await gate.acquire(8)
        head = asyncio.create_task(gate.acquire(4))
        await _wait_for(lambda: gate.queue_length == 1)
        tail = asyncio.create_task(gate.acquire(1))
        await _wait_for(lambda: gate.queue_length == 2)

        reservation.release()
        await asyncio.sleep(0.05)
        assert gate.queue_length == 2
        assert not head.done()
        assert not tail.done()

        upstream.used_cost = 3
        gate.update_upstream_used(3)
        head_reservation = await asyncio.wait_for(head, 1.0)
        tail_reservation = await asyncio.wait_for(tail, 1.0)
        assert gate.reserved == 5
        head_reservation.release()
        tail_reservation.release()


class TestConcurrencyGateUpstreamUsed:
    """Upstream snapshot polling and the max() effective usage."""

    @pytest.mark.anyio
    async def test_polls_snapshot_while_busy(self, make_gate: Any) -> None:
        """While busy the gate tracks the upstream used_cost."""

        upstream = _FakeFeatherlessUpstream(used_cost=6)
        gate = make_gate(upstream)
        reservation = await gate.acquire(4)

        await _wait_for(lambda: gate.upstream_used == 6)
        assert gate.effective_used() == 6
        reservation.release()

    @pytest.mark.anyio
    async def test_queues_request_for_external_usage(self, make_gate: Any) -> None:
        """Consumption outside the proxy counts against the budget."""

        upstream = _FakeFeatherlessUpstream(used_cost=6)
        gate = make_gate(upstream)
        await gate.acquire(4)
        await _wait_for(lambda: gate.upstream_used == 6)

        task = asyncio.create_task(gate.acquire(3))
        await _wait_for(lambda: gate.queue_length == 1)
        assert not task.done()

        upstream.used_cost = 1
        queued_reservation = await asyncio.wait_for(task, 2.0)
        assert gate.reserved == 7
        queued_reservation.release()

    @pytest.mark.anyio
    async def test_keeps_used_when_snapshot_fails(
        self, make_gate: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing snapshot keeps the last used_cost and warns once."""

        upstream = _FakeFeatherlessUpstream(used_cost=6)
        gate = make_gate(upstream)
        reservation = await gate.acquire(4)
        await _wait_for(lambda: gate.upstream_used == 6)

        upstream.fail = True
        await asyncio.sleep(0.1)
        assert gate.upstream_used == 6
        warnings = [
            record
            for record in caplog.records
            if "concurrency snapshot" in record.message
        ]
        assert len(warnings) == 1
        reservation.release()

    @pytest.mark.anyio
    async def test_stops_polling_when_idle(self, make_gate: Any) -> None:
        """The poll loop exits after an idle period."""

        upstream = _FakeFeatherlessUpstream(used_cost=6)
        gate = make_gate(upstream)
        reservation = await gate.acquire(4)
        await _wait_for(lambda: gate.upstream_used == 6)
        reservation.release()

        await _wait_for(lambda: not gate.poll_running)
        requests_after_stop = upstream.request_count
        await asyncio.sleep(0.15)
        assert upstream.request_count == requests_after_stop


class TestGateRegistry:
    """Per-credential gate separation and idle sweeping."""

    @pytest.mark.anyio
    async def test_returns_none_without_credential(self) -> None:
        """Anonymous requests get no gate."""

        registry = _make_registry(_FakeFeatherlessUpstream())
        try:
            assert registry.gate_for(None) is None
            assert registry.gate_for("") is None
        finally:
            await registry.close()

    @pytest.mark.anyio
    async def test_reuses_gate_for_same_credential(self) -> None:
        """The same credential maps to the same gate instance."""

        registry = _make_registry(_FakeFeatherlessUpstream())
        try:
            first = registry.gate_for(AUTH1)
            second = registry.gate_for(AUTH1)
            assert first is second
        finally:
            await registry.close()

    @pytest.mark.anyio
    async def test_separates_gates_per_credential(self) -> None:
        """Each credential gets its own budget."""

        registry = _make_registry(
            _FakeFeatherlessUpstream(),
            settings=_gate_settings(concurrency_limit=4),
        )
        try:
            first = registry.gate_for(AUTH1)
            second = registry.gate_for(AUTH2)
            assert first is not second
            reservation_one = await first.acquire(4)
            reservation_two = await second.acquire(4)
            assert first.reserved == 4
            assert second.reserved == 4
            reservation_one.release()
            reservation_two.release()
        finally:
            await registry.close()

    @pytest.mark.anyio
    async def test_sweeps_idle_gates(self) -> None:
        """An idle gate is disposed while a busy gate survives (C14/C15)."""

        registry = _make_registry(_FakeFeatherlessUpstream())
        try:
            idle_gate = registry.gate_for(AUTH1)
            reservation = await idle_gate.acquire(4)
            reservation.release()
            busy_gate = registry.gate_for(AUTH2)
            busy_reservation = await busy_gate.acquire(4)

            await registry.start()
            await _wait_for(lambda: AUTH1_ID not in registry.gates)
            assert AUTH2_ID in registry.gates
            busy_reservation.release()
        finally:
            await registry.close()

    @pytest.mark.anyio
    async def test_close_cancels_sweep_and_gates(self) -> None:
        """Closing the registry stops the sweeper and every gate."""

        registry = _make_registry(_FakeFeatherlessUpstream())
        gate = registry.gate_for(AUTH1)
        reservation = await gate.acquire(4)
        await registry.start()
        assert registry.sweep_running
        assert gate.poll_running

        await registry.close()

        assert not registry.sweep_running
        assert not gate.poll_running
        assert registry.gates == {}
        reservation.release()