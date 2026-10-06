"""Tests for the OpenEvidence sidecar's "pending + poll" contract
(GET /v1/genes/{gene}/openevidence in main.py).

A cold OpenEvidence call takes ~90-290s — longer than the prod ingress's 300s
request timeout reliably allows — so a cache miss must start the lookup in
the background and answer "pending" right away instead of holding the
request open. These tests run the real OpenEvidenceClient.get_gene_analysis
(and its real cached_call cache write) against an in-memory fake Redis with a
controllable clock, replacing only the upstream HTTP call
(_post_streaming_analysis), so "exactly one upstream call" means exactly one
paid OpenEvidence request.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional, Tuple

import httpx
import pytest
from fastapi import Response

from src import main
from src.pipeline import cache as cache_module
from src.pipeline import openevidence
from src.pipeline.openevidence import OpenEvidenceClient, distill_additive_openevidence

_NCCN_CITATION_EVENT = (
    '{"text": "[[1]]", "reference": {"citation_key": 1, '
    '"reference_text": "National Comprehensive Cancer Network. Non-Small Cell Lung Cancer.", '
    '"reference_detail": {"title": "Non-Small Cell Lung Cancer", '
    '"authors_string": "National Comprehensive Cancer Network", '
    '"publication_date": "2026-09-02", '
    '"url": "https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf"}, '
    '"source_texts": []}}'
)
_SSE_STREAM = (
    'data: {"text": "NCCN recommends alectinib first-line for ALK-rearranged NSCLC. "}\n\n'
    f"data: {_NCCN_CITATION_EVENT}\n\n"
)


class FakeRedis:
    """Just enough of redis.asyncio.Redis (get/set with ex+nx/delete) for the
    sidecar's cache and marker keys, with a manually advanced clock so TTL
    expiry is deterministic."""

    def __init__(self) -> None:
        self.now = 0.0
        self._store: Dict[str, Tuple[bytes, Optional[float]]] = {}

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _live(self, key: str) -> Optional[bytes]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and expires_at <= self.now:
            del self._store[key]
            return None
        return value

    def keys_with_prefix(self, prefix: str) -> List[str]:
        return [key for key in list(self._store) if key.startswith(prefix) and self._live(key) is not None]

    async def get(self, key: str) -> Optional[bytes]:
        return self._live(key)

    async def set(self, key: str, value, ex: Optional[int] = None, nx: bool = False):
        if nx and self._live(key) is not None:
            return None
        data = value.encode() if isinstance(value, str) else value
        self._store[key] = (data, self.now + ex if ex else None)
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self._store.pop(key, None) is not None)

    async def flushdb(self) -> None:
        self._store.clear()


class FakeUpstream:
    """Stands in for the paid OpenEvidence HTTP call. Blocks until
    `release()` (so a lookup can be held "in flight"), then returns a real
    SSE stream or raises `fail_with`."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self._gate = asyncio.Event()
        self.fail_with: Optional[BaseException] = None

    def release(self) -> None:
        self._gate.set()

    async def __call__(self, question: str, api_key: str, client: httpx.AsyncClient) -> str:
        self.calls.append(question)
        await self._gate.wait()
        if self.fail_with is not None:
            raise self.fail_with
        return _SSE_STREAM


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(cache_module, "_client", redis)
    return redis


@pytest.fixture
def upstream(monkeypatch):
    fake = FakeUpstream()
    monkeypatch.setattr(openevidence, "_post_streaming_analysis", fake)
    return fake


@pytest.fixture(autouse=True)
def _sidecar_settings(monkeypatch):
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)
    monkeypatch.setattr(main.settings, "openevidence_api_key", "test-key")
    monkeypatch.setattr(main.settings, "openevidence_sidecar_pending_wait_seconds", 0.05)
    monkeypatch.setattr(main.settings, "openevidence_sidecar_retry_after_seconds", 10)
    monkeypatch.setattr(main.settings, "openevidence_sidecar_inflight_ttl_seconds", 600)
    monkeypatch.setattr(main.settings, "openevidence_sidecar_failed_ttl_seconds", 300)
    main._openevidence_sidecar_semaphores.clear()
    main._reset_openevidence_sidecar_state()
    yield
    main._reset_openevidence_sidecar_state()


async def _request(gene: str = "ALK", **params) -> Tuple[main.OpenEvidenceSidecarResponse, Response]:
    response = Response()
    response.status_code = 200
    result = await asyncio.wait_for(
        main.get_gene_openevidence(
            gene,
            response=response,
            tumor_type=params.get("tumor_type"),
            fusion=params.get("fusion"),
            core_pmids=params.get("core_pmids", []),
            core_titles=params.get("core_titles", []),
        ),
        timeout=2.0,
    )
    return result, response


async def _wait_for_background_lookups() -> None:
    tasks = list(main._openevidence_sidecar_tasks.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
    await asyncio.sleep(0)  # let done-callbacks deregister the tasks


def _asgi_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def test_cache_miss_returns_pending_fast_even_when_upstream_is_slow(fake_redis, upstream):
    start = time.monotonic()
    async with _asgi_client() as client:
        http = await asyncio.wait_for(
            client.get("/v1/genes/ALK/openevidence", params={"tumor_type": "NSCLC"}), timeout=2.0
        )
    elapsed = time.monotonic() - start

    assert elapsed < 1.5
    # HTTP 503 + Retry-After is deliberate (old cached frontends treat it as
    # a transient, non-memoized fetch error); the body carries the contract.
    assert http.status_code == 503
    assert http.headers["retry-after"] == "10"
    assert http.json() == {
        "available": False,
        "distilled": None,
        "error": None,
        "status": "pending",
        "retry_after_seconds": 10,
    }
    # The lookup is still running in the background (the request didn't wait for it).
    assert len(upstream.calls) == 1
    assert len(main._openevidence_sidecar_tasks) == 1
    upstream.release()
    await _wait_for_background_lookups()


async def test_concurrent_and_repeated_requests_trigger_exactly_one_upstream_call(fake_redis, upstream):
    concurrent = await asyncio.gather(*[_request("ALK", tumor_type="NSCLC") for _ in range(8)])
    assert [result.status for result, _ in concurrent] == ["pending"] * 8
    for _ in range(5):  # repeated polls while it's still in flight
        result, _ = await _request("ALK", tumor_type="NSCLC")
        assert result.status == "pending"
    # A second worker/pod (empty in-process registry) polling the same key
    # is held off by the Redis in-flight marker rather than calling upstream.
    registry = dict(main._openevidence_sidecar_tasks)
    main._openevidence_sidecar_tasks.clear()
    result, _ = await _request("ALK", tumor_type="NSCLC")
    assert result.status == "pending"
    assert main._openevidence_sidecar_tasks == {}
    main._openevidence_sidecar_tasks.update(registry)

    assert len(upstream.calls) == 1
    upstream.release()
    await _wait_for_background_lookups()

    for _ in range(3):
        result, _ = await _request("ALK", tumor_type="NSCLC")
        assert result.status == "ready"
    assert len(upstream.calls) == 1


async def test_completed_lookup_is_ready_and_matches_get_gene_analysis(fake_redis, upstream):
    params = {"tumor_type": "NSCLC", "fusion": "EML4::ALK"}
    first, _ = await _request("ALK", **params)
    assert first.status == "pending"
    upstream.release()
    await _wait_for_background_lookups()
    # The in-flight marker is cleared once the lookup finishes.
    assert fake_redis.keys_with_prefix("openevidence_inflight:") == []

    ready, response = await _request("ALK", **params)
    assert response.status_code == 200
    assert ready.status == "ready"
    assert ready.available is True
    assert ready.retry_after_seconds is None

    # Same cache slot as get_gene_analysis(gene, tumor_type, fusion): calling
    # it now is a cache hit (no second upstream call) and distills identically.
    analysis = await OpenEvidenceClient().get_gene_analysis("ALK", tumor_type="NSCLC", fusion="EML4::ALK")
    assert len(upstream.calls) == 1
    assert ready.distilled == distill_additive_openevidence(analysis, core_pmids=[], core_titles=[])

    # Without the fusion it's a different key/question, so it needs its own
    # upstream call (which, with upstream now fast, answers inline).
    other, _ = await _request("ALK", tumor_type="NSCLC")
    assert other.status == "ready"
    assert len(upstream.calls) == 2


async def test_ready_after_completion_even_when_redis_is_down(monkeypatch, upstream):
    class DownRedis:
        async def get(self, *args, **kwargs):
            raise ConnectionError("redis down")

        set = delete = get

    monkeypatch.setattr(cache_module, "_client", DownRedis())
    first, _ = await _request("ALK")
    assert first.status == "pending"
    upstream.release()
    await _wait_for_background_lookups()

    ready, _ = await _request("ALK")
    assert ready.status == "ready"
    assert len(upstream.calls) == 1


async def test_warmed_entry_is_served_ready_without_a_background_lookup(fake_redis, upstream):
    # The offline warmup (openevidence_warmup.warm_one) fills the cache via
    # this exact call.
    upstream.release()
    await OpenEvidenceClient().get_gene_analysis("ALK", tumor_type="NSCLC", fusion="EML4::ALK")
    assert len(upstream.calls) == 1

    result, response = await _request("ALK", tumor_type="NSCLC", fusion="EML4::ALK")
    assert response.status_code == 200
    assert result.status == "ready"
    assert result.available is True
    assert main._openevidence_sidecar_tasks == {}
    assert len(upstream.calls) == 1


async def test_failure_is_not_cached_and_answers_failed_without_recalling_upstream(fake_redis, upstream):
    # Not a retryable transport error, so the client's own tenacity retry
    # doesn't multiply upstream calls within the one lookup.
    upstream.fail_with = RuntimeError("upstream reset")
    first, _ = await _request("ALK")
    assert first.status == "pending"
    upstream.release()
    await _wait_for_background_lookups()

    for _ in range(4):
        result, response = await _request("ALK")
        assert response.status_code == 200
        assert result.status == "failed"
        assert result.available is False
        assert "upstream reset" in result.error
    # A different worker/pod (no in-process memo) sees the Redis failed marker too.
    main._reset_openevidence_sidecar_state()
    result, _ = await _request("ALK")
    assert result.status == "failed"

    # Polling a failed key never re-triggers the paid call, and nothing was cached.
    assert len(upstream.calls) == 1
    assert fake_redis.keys_with_prefix("openevidence:") == []
    assert fake_redis.keys_with_prefix("openevidence_inflight:") == []

    # Once the failed marker expires, a new request may retry.
    fake_redis.advance(301)
    main._reset_openevidence_sidecar_state()
    upstream.fail_with = None
    retried, _ = await _request("ALK")
    await _wait_for_background_lookups()
    assert retried.status in {"pending", "ready"}
    assert len(upstream.calls) == 2
    result, _ = await _request("ALK")
    assert result.status == "ready"


async def test_fast_failure_answers_failed_inline(fake_redis, upstream):
    upstream.fail_with = RuntimeError("bad request")
    upstream.release()
    result, response = await _request("ALK")
    assert response.status_code == 200
    assert result.status == "failed"
    assert result.error == "bad request"
    assert len(upstream.calls) == 1


async def test_stale_inflight_marker_expires(fake_redis, upstream):
    key = openevidence.sidecar_cache_key("ALK", None, None)
    # Another pod claimed the lookup and then died mid-call.
    await fake_redis.set("openevidence_inflight:" + key, "1", ex=600)

    result, _ = await _request("ALK")
    assert result.status == "pending"
    assert upstream.calls == []
    assert main._openevidence_sidecar_tasks == {}

    fake_redis.advance(601)
    result, _ = await _request("ALK")
    assert result.status == "pending"
    assert len(upstream.calls) == 1
    upstream.release()
    await _wait_for_background_lookups()
    result, _ = await _request("ALK")
    assert result.status == "ready"


async def test_background_lookup_survives_client_disconnect(fake_redis, upstream):
    request = asyncio.create_task(_request("ALK"))
    await asyncio.sleep(0.01)  # request is now waiting on the background task
    request.cancel()  # the client went away
    with pytest.raises(asyncio.CancelledError):
        await request

    upstream.release()
    await _wait_for_background_lookups()
    assert len(fake_redis.keys_with_prefix("openevidence:")) == 1
    result, _ = await _request("ALK")
    assert result.status == "ready"
    assert len(upstream.calls) == 1


async def test_background_lookup_runs_under_sidecar_concurrency_limit(monkeypatch, fake_redis, upstream):
    monkeypatch.setattr(main.settings, "openevidence_sidecar_concurrency", 1)
    main._openevidence_sidecar_semaphores.clear()
    for gene in ("ALK", "BRAF", "EGFR"):
        result, _ = await _request(gene)
        assert result.status == "pending"
    assert len(upstream.calls) == 1  # the other two are queued behind the cap
    upstream.release()
    await _wait_for_background_lookups()
    assert len(upstream.calls) == 3


async def test_flag_off_starts_no_task_touches_no_client_or_redis(monkeypatch, upstream):
    monkeypatch.setattr(main.settings, "openevidence_enabled", False)

    def no_redis():
        raise AssertionError("Redis must not be touched with the flag off")

    def no_client(self, *args, **kwargs):
        raise AssertionError("OpenEvidenceClient must not be constructed with the flag off")

    def no_task(*args, **kwargs):
        raise AssertionError("no background lookup may start with the flag off")

    monkeypatch.setattr(cache_module, "_get_client", no_redis)
    monkeypatch.setattr(openevidence, "_get_client", no_redis)
    monkeypatch.setattr(main.OpenEvidenceClient, "__init__", no_client)
    monkeypatch.setattr(main, "_start_openevidence_sidecar_lookup", no_task)

    async with _asgi_client() as client:
        http = await client.get(
            "/v1/genes/ALK/openevidence", params={"tumor_type": "NSCLC", "fusion": "EML4::ALK"}
        )

    assert http.status_code == 200
    # Byte-for-byte the pre-change flag-off body.
    assert http.json() == {"available": False, "distilled": None, "error": None}
    assert main._openevidence_sidecar_tasks == {}
    assert upstream.calls == []


async def test_hung_lookup_times_out_as_failed_and_clears_its_marker(monkeypatch, fake_redis, upstream):
    monkeypatch.setattr(main.settings, "openevidence_sidecar_lookup_timeout_seconds", 0.05)
    first, _ = await _request("ALK")  # upstream is never released: the call hangs
    assert first.status == "pending"
    await _wait_for_background_lookups()

    result, response = await _request("ALK")
    assert response.status_code == 200
    assert result.status == "failed"
    assert "timed out" in result.error
    assert len(upstream.calls) == 1
    assert fake_redis.keys_with_prefix("openevidence:") == []
    assert fake_redis.keys_with_prefix("openevidence_inflight:") == []
    assert len(fake_redis.keys_with_prefix("openevidence_failed:")) == 1


async def test_lookup_is_tracked_with_the_shared_background_tasks(fake_redis, upstream):
    await _request("ALK")
    (task,) = main._openevidence_sidecar_tasks.values()
    assert task in main._background_tasks
    upstream.release()
    await _wait_for_background_lookups()
    assert task not in main._background_tasks


async def test_finished_lookup_memos_are_bounded(monkeypatch, fake_redis, upstream):
    monkeypatch.setattr(main, "_OPENEVIDENCE_SIDECAR_MEMO_MAX", 2)
    upstream.fail_with = RuntimeError("boom")
    upstream.release()
    for gene in ("ALK", "BRAF", "EGFR"):
        result, _ = await _request(gene)
        assert result.status == "failed"
    await _wait_for_background_lookups()
    assert list(main._openevidence_sidecar_failures) == [
        openevidence.sidecar_cache_key(gene, None, None) for gene in ("BRAF", "EGFR")
    ]


async def test_shutdown_cancels_inflight_lookups_and_clears_markers(fake_redis, upstream):
    result, _ = await _request("ALK")
    assert result.status == "pending"
    (task,) = main._openevidence_sidecar_tasks.values()

    await main._cancel_openevidence_sidecar_lookups()
    await asyncio.sleep(0)

    assert task.cancelled()
    assert main._openevidence_sidecar_tasks == {}
    assert task not in main._background_tasks
    assert fake_redis.keys_with_prefix("openevidence_inflight:") == []
    # A cancelled lookup is neither cached nor remembered as failed.
    assert fake_redis.keys_with_prefix("openevidence:") == []
    assert fake_redis.keys_with_prefix("openevidence_failed:") == []


async def test_sidecar_requires_auth_when_enabled(monkeypatch, fake_redis, upstream):
    monkeypatch.setattr(main.settings, "auth_enabled", True)
    monkeypatch.setattr(main.settings, "auth_secret_key", "secret-key-for-testing")

    async with _asgi_client() as client:
        http = await client.get("/v1/genes/ALK/openevidence")

    assert http.status_code == 401
    assert main._openevidence_sidecar_tasks == {}
    assert upstream.calls == []


async def test_authenticated_client_can_poll_until_ready(monkeypatch, fake_redis, upstream):
    monkeypatch.setattr(main.settings, "auth_enabled", True)
    user = main.AuthenticatedUser(
        email="curator@mskcc.org", name="Curator", domain="mskcc.org", role="curator", provider="keycloak"
    )
    monkeypatch.setitem(main.app.dependency_overrides, main.require_auth, lambda: user)

    async with _asgi_client() as client:
        pending = await client.get("/v1/genes/ALK/openevidence")
        assert pending.status_code == 503
        assert pending.json()["status"] == "pending"
        upstream.release()
        await _wait_for_background_lookups()
        ready = await client.get("/v1/genes/ALK/openevidence")

    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert len(upstream.calls) == 1
