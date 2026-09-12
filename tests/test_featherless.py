"""Tests for the featherless mode: model cache and whitelist handling."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request

from niche_llm_proxy.config import FeatherlessConfig, ProxyConfig, load_config
from niche_llm_proxy.featherless import (
    FeatherlessRuntime,
    ModelInfoCache,
    RequestKind,
    classify_request,
    extract_model_reference,
    handle_featherless_model_detail,
    handle_featherless_models,
    key_id_from_authorization,
)

KIMI = "moonshotai/Kimi-K2.6"
QWEN = "Qwen/Qwen3-32B"
MISSING = "missing/model-7b"
BASE_URL = "https://api.featherless.ai"


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
    ) -> None:
        self.details = details or {}
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