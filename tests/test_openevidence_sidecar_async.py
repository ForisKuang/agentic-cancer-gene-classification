"""Tests for the OpenEvidence sidecar's "pending + poll" contract
(GET /v1/genes/{gene}/openevidence in main.py).

A cold OpenEvidence call takes ~90-290s — longer than the prod ingress's 300s
request timeout reliably allows — so a cache miss must start the lookup in
the background and answer "pending" right away instead of holding the
request open. These tests run the real OpenEvidenceClient.get_gene_analysis
(and its real cached_call cache write) against conftest's in-memory FakeRedis
with a controllable clock, replacing only the upstream HTTP call
(_post_streaming_analysis), so "exactly one upstream call" means exactly one
paid OpenEvidence request.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Dict, List, Optional, Tuple

import httpx
import pytest
from fastapi import Response

from src import main
from src.config import Settings
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
    # conftest's _reset_openevidence_sidecar cancels leftover lookups and
    # clears the registry/memos/semaphores around every test.
    yield


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
    # No X-OpenEvidence-Poll header = an old cached frontend: HTTP 503 +
    # Retry-After is deliberate (it treats that as a transient, non-memoized
    # fetch error); the body carries the contract.
    assert http.status_code == 503
    assert http.headers["retry-after"] == "10"
    assert "X-OpenEvidence-Poll" in http.headers["vary"]
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


async def test_poll_capable_client_gets_202_pending_and_old_client_gets_503(fake_redis, upstream):
    pending_body = {
        "available": False,
        "distilled": None,
        "error": None,
        "status": "pending",
        "retry_after_seconds": 10,
    }
    async with _asgi_client() as client:
        new_client = await client.get("/v1/genes/ALK/openevidence", headers={"X-OpenEvidence-Poll": "1"})
        old_client = await client.get("/v1/genes/ALK/openevidence")
        other_value = await client.get("/v1/genes/ALK/openevidence", headers={"X-OpenEvidence-Poll": "yes"})
        upstream.release()
        await _wait_for_background_lookups()
        ready_new = await client.get("/v1/genes/ALK/openevidence", headers={"X-OpenEvidence-Poll": "1"})
        ready_old = await client.get("/v1/genes/ALK/openevidence")

    # Normal progress for a client that understands "pending": 202, not a 5xx.
    assert new_client.status_code == 202
    assert new_client.headers["retry-after"] == "10"
    assert "X-OpenEvidence-Poll" in new_client.headers["vary"]
    assert new_client.json() == pending_body
    # Clients without the signal keep the old-frontend-safe 503, same body.
    assert old_client.status_code == 503
    assert old_client.json() == pending_body
    assert other_value.status_code == 503
    # Non-pending answers are unaffected by the header.
    assert ready_new.status_code == ready_old.status_code == 200
    assert ready_new.json() == ready_old.json()
    assert ready_new.json()["status"] == "ready"
    assert len(upstream.calls) == 1


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
    monkeypatch.setattr(main.settings, "openevidence_timeout_seconds", 0.01)
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


def test_lookup_budget_default_is_900s_and_never_below_the_read_timeout(monkeypatch):
    assert Settings.model_fields["openevidence_sidecar_lookup_timeout_seconds"].default == 900.0
    monkeypatch.setattr(main.settings, "openevidence_sidecar_lookup_timeout_seconds", 900.0)
    monkeypatch.setattr(main.settings, "openevidence_timeout_seconds", 600.0)  # prod's per-read timeout
    assert main._openevidence_sidecar_lookup_budget_seconds() == 900.0
    # A budget configured below the read timeout is raised to read timeout x 1.25.
    monkeypatch.setattr(main.settings, "openevidence_sidecar_lookup_timeout_seconds", 540.0)
    assert main._openevidence_sidecar_lookup_budget_seconds() == 750.0
    monkeypatch.setattr(main.settings, "openevidence_timeout_seconds", 1000.0)
    monkeypatch.setattr(main.settings, "openevidence_sidecar_lookup_timeout_seconds", 900.0)
    assert main._openevidence_sidecar_lookup_budget_seconds() == 1250.0


async def test_lookup_slower_than_a_budget_set_below_the_read_timeout_still_succeeds(
    monkeypatch, fake_redis, upstream
):
    """Scaled-down prod shape: read timeout 600s, budget misconfigured at
    540s, call finishing at 550s. The budget is floored at the read timeout
    x 1.25, so the lookup is ready rather than failed."""
    monkeypatch.setattr(main.settings, "openevidence_timeout_seconds", 0.6)
    monkeypatch.setattr(main.settings, "openevidence_sidecar_lookup_timeout_seconds", 0.54)
    first, _ = await _request("ALK")
    assert first.status == "pending"
    await asyncio.sleep(0.55)  # past the configured 0.54s budget, within the read timeout
    upstream.release()
    await _wait_for_background_lookups()

    result, _ = await _request("ALK")
    assert result.status == "ready"
    assert len(upstream.calls) == 1


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


# ---------------------------------------------------------------------------
# In-flight lease ownership across pods
# ---------------------------------------------------------------------------

_POD_STATE = (
    "_openevidence_sidecar_tasks",
    "_openevidence_sidecar_failures",
    "_openevidence_sidecar_results",
    "_openevidence_sidecar_semaphores",
)


@contextlib.contextmanager
def _as_other_pod():
    """Run requests as a second pod: same (fake) Redis, but its own task
    registry, memos and concurrency semaphores."""
    saved = {name: getattr(main, name) for name in _POD_STATE}
    for name in _POD_STATE:
        setattr(main, name, {})
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(main, name, value)


def _genes(questions: List[str]) -> List[str]:
    return [next(gene for gene in ("ALK", "BRAF", "EGFR") if gene in question) for question in questions]


def _inflight_key(gene: str) -> str:
    return "openevidence_inflight:" + openevidence.sidecar_cache_key(gene, None, None)


class PerGeneUpstream:
    """Like FakeUpstream, but each gene's calls are released independently,
    so one pod's call can still be in flight when another pod starts one."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self._gates: Dict[str, asyncio.Event] = {}

    def _gate(self, gene: str) -> asyncio.Event:
        return self._gates.setdefault(gene, asyncio.Event())

    def release(self, gene: str) -> None:
        self._gate(gene).set()

    async def __call__(self, question: str, api_key: str, client: httpx.AsyncClient) -> str:
        self.calls.append(question)
        await self._gate(_genes([question])[0]).wait()
        return _SSE_STREAM


@pytest.fixture
def per_gene_upstream(monkeypatch):
    fake = PerGeneUpstream()
    monkeypatch.setattr(openevidence, "_post_streaming_analysis", fake)
    return fake


async def _queue_braf_behind_alk(monkeypatch, upstream) -> asyncio.Task:
    monkeypatch.setattr(main.settings, "openevidence_sidecar_concurrency", 1)
    main._openevidence_sidecar_semaphores.clear()
    for gene in ("ALK", "BRAF"):
        result, _ = await _request(gene)
        assert result.status == "pending"
    assert _genes(upstream.calls) == ["ALK"]  # BRAF is queued behind the cap
    return main._openevidence_sidecar_tasks[openevidence.sidecar_cache_key("BRAF", None, None)]


async def test_queued_lookup_that_lost_its_lease_does_not_duplicate_another_pods_call(
    monkeypatch, fake_redis, per_gene_upstream
):
    upstream = per_gene_upstream
    pod1_braf = await _queue_braf_behind_alk(monkeypatch, upstream)
    fake_redis.advance(601)  # pod 1's leases lapse while BRAF sits in the queue
    with _as_other_pod():
        result, _ = await _request("BRAF")  # pod 2 claims BRAF and calls upstream
        assert result.status == "pending"
        pod2_braf = main._openevidence_sidecar_tasks[openevidence.sidecar_cache_key("BRAF", None, None)]
    assert _genes(upstream.calls) == ["ALK", "BRAF"]

    upstream.release("ALK")  # frees pod 1's slot while pod 2's BRAF call is still in flight
    await asyncio.wait({pod1_braf}, timeout=1.0)
    assert _genes(upstream.calls) == ["ALK", "BRAF"], "pod 1 must not pay for BRAF a second time"
    assert pod1_braf.done() and pod1_braf.result() is None  # abandoned, not failed
    assert main._openevidence_memo_get(main._openevidence_sidecar_failures, openevidence.sidecar_cache_key("BRAF", None, None)) is None

    upstream.release("BRAF")
    await asyncio.wait_for(pod2_braf, timeout=2.0)
    await _wait_for_background_lookups()
    result, _ = await _request("BRAF")
    assert result.status == "ready"  # pod 2's answer, via the shared cache
    assert _genes(upstream.calls) == ["ALK", "BRAF"]
    assert fake_redis.keys_with_prefix("openevidence_inflight:") == []


async def test_old_owner_cleanup_never_releases_the_new_owners_lease(monkeypatch, fake_redis, per_gene_upstream):
    upstream = per_gene_upstream
    pod1_braf = await _queue_braf_behind_alk(monkeypatch, upstream)
    fake_redis.advance(601)
    with _as_other_pod():
        await _request("BRAF")  # pod 2 claims BRAF's lease and calls upstream
        pod2_braf = main._openevidence_sidecar_tasks[openevidence.sidecar_cache_key("BRAF", None, None)]
    pod2_token = await fake_redis.get(_inflight_key("BRAF"))
    assert pod2_token is not None

    pod1_braf.cancel()  # e.g. pod 1 shutting down
    with pytest.raises(asyncio.CancelledError):
        await pod1_braf

    assert await fake_redis.get(_inflight_key("BRAF")) == pod2_token
    assert await openevidence.claim_inflight_marker(openevidence.sidecar_cache_key("BRAF", None, None), 600) is None

    upstream.release("ALK")
    upstream.release("BRAF")
    await asyncio.wait_for(pod2_braf, timeout=2.0)
    await _wait_for_background_lookups()
    assert _genes(upstream.calls) == ["ALK", "BRAF"]
    assert fake_redis.keys_with_prefix("openevidence_inflight:") == []  # pod 2 released its own lease


async def test_queued_lookup_keeps_its_lease_alive(monkeypatch, fake_redis, upstream):
    monkeypatch.setattr(main.settings, "openevidence_sidecar_inflight_ttl_seconds", 0.3)
    await _queue_braf_behind_alk(monkeypatch, upstream)
    for _ in range(12):  # 1.2 fake seconds queued — four lease TTLs
        fake_redis.advance(0.1)
        await asyncio.sleep(0.25)  # the heartbeat renews every ttl/3 (0.1s)
    assert await fake_redis.get(_inflight_key("BRAF")) is not None
    upstream.release()
    await _wait_for_background_lookups()
    assert _genes(upstream.calls) == ["ALK", "BRAF"]


async def test_lease_scripts_check_ownership_on_real_redis():
    client = cache_module._get_client()
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")
    key = "lease-test"
    owner = await openevidence.claim_inflight_marker(key, 600)
    assert owner is not None
    assert await openevidence.claim_inflight_marker(key, 600) is None
    assert await openevidence.renew_inflight_marker(key, "someone-else", 600) is False
    await openevidence.release_inflight_marker(key, "someone-else")
    assert await client.get(_INFLIGHT_PREFIX + key) == owner.encode()
    assert await openevidence.renew_inflight_marker(key, owner, 600) is True
    assert 0 < await client.pttl(_INFLIGHT_PREFIX + key) <= 600_000
    await openevidence.release_inflight_marker(key, owner)
    assert await client.get(_INFLIGHT_PREFIX + key) is None


_INFLIGHT_PREFIX = "openevidence_inflight:"


class PerCallUpstream:
    """Each upstream call blocks until the test settles it by index, so one
    owner's call can fail while another owner's call is still running."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self._results: List[asyncio.Future] = []

    async def __call__(self, question: str, api_key: str, client: httpx.AsyncClient) -> str:
        future = asyncio.get_running_loop().create_future()
        self.calls.append(question)
        self._results.append(future)
        return await future

    def succeed(self, index: int) -> None:
        self._results[index].set_result(_SSE_STREAM)

    def fail(self, index: int, exc: BaseException) -> None:
        self._results[index].set_exception(exc)


@pytest.fixture
def per_call_upstream(monkeypatch):
    fake = PerCallUpstream()
    monkeypatch.setattr(openevidence, "_post_streaming_analysis", fake)
    return fake


def _live_heartbeats() -> List[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and getattr(task.get_coro(), "__name__", "") == "_keep_openevidence_lease_alive"
    ]


async def _another_pod_takes_the_lost_lease_and_duplicates_the_call(
    fake_redis, upstream
) -> Tuple[asyncio.Task, bytes]:
    """Accepted best-effort behavior, not a guarantee: once pod A has lost
    its lease mid-call, pod B can claim the key and make a SECOND paid call
    while A's is still running (cross-pod dedupe is best-effort; only
    one-call-per-key-per-worker is exact — see main.py's sidecar state
    comment). What IS guaranteed is checked by the callers: A's stale
    outcome can't clobber B's lease or publish a failure for B's lookup."""
    key = openevidence.sidecar_cache_key("ALK", None, None)
    with _as_other_pod():
        result, _ = await _request("ALK")  # pod B claims the (now free) lease and calls upstream
        assert result.status == "pending"
        pod_b = main._openevidence_sidecar_tasks[key]
    assert len(upstream.calls) == 2
    b_token = await fake_redis.get(_inflight_key("ALK"))
    assert b_token is not None
    return pod_b, b_token


async def _assert_stale_failure_discarded(fake_redis, upstream, pod_a, pod_b, b_token) -> None:
    key = openevidence.sidecar_cache_key("ALK", None, None)
    upstream.fail(0, RuntimeError("stale owner failure"))  # A fails while B is still running
    await asyncio.wait_for(pod_a, timeout=2.0)

    assert pod_a.result() is None, "a lookup that lost its lease must not report a failure"
    assert fake_redis.keys_with_prefix("openevidence_failed:") == []
    assert main._openevidence_memo_get(main._openevidence_sidecar_failures, key) is None
    assert await fake_redis.get(_inflight_key("ALK")) == b_token, "B's lease must be untouched"
    assert len(_live_heartbeats()) == 1  # only B's; A's heartbeat ended with A
    with _as_other_pod():
        result, _ = await _request("ALK")  # a third pod polling meanwhile
    assert result.status == "pending"
    result, _ = await _request("ALK")  # pod A's own next poll
    assert result.status == "pending"

    upstream.succeed(1)
    await asyncio.wait_for(pod_b, timeout=2.0)
    await _wait_for_background_lookups()
    assert _live_heartbeats() == []
    result, _ = await _request("ALK")
    assert result.status == "ready"
    assert len(upstream.calls) == 2


async def test_best_effort_lease_lost_mid_call_allows_a_duplicate_but_publishes_no_stale_failure(
    fake_redis, per_call_upstream
):
    result, _ = await _request("ALK")
    assert result.status == "pending"
    pod_a = main._openevidence_sidecar_tasks[openevidence.sidecar_cache_key("ALK", None, None)]
    await fake_redis.delete(_inflight_key("ALK"))  # Redis lost A's lease mid-call

    pod_b, b_token = await _another_pod_takes_the_lost_lease_and_duplicates_the_call(fake_redis, per_call_upstream)
    await _assert_stale_failure_discarded(fake_redis, per_call_upstream, pod_a, pod_b, b_token)


async def test_best_effort_heartbeat_noticing_lease_loss_allows_a_duplicate_but_discards_the_outcome(
    monkeypatch, fake_redis, per_call_upstream
):
    monkeypatch.setattr(main.settings, "openevidence_sidecar_inflight_ttl_seconds", 0.3)
    result, _ = await _request("ALK")
    assert result.status == "pending"
    pod_a = main._openevidence_sidecar_tasks[openevidence.sidecar_cache_key("ALK", None, None)]
    assert len(_live_heartbeats()) == 1

    await fake_redis.delete(_inflight_key("ALK"))
    await asyncio.sleep(0.25)  # A's heartbeat (every 0.1s) notices the loss...
    assert _live_heartbeats() == [], "...and stops rather than leaking"

    pod_b, b_token = await _another_pod_takes_the_lost_lease_and_duplicates_the_call(fake_redis, per_call_upstream)
    await _assert_stale_failure_discarded(fake_redis, per_call_upstream, pod_a, pod_b, b_token)


async def test_failure_publication_checks_ownership_on_real_redis():
    client = cache_module._get_client()
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")
    key = "publish-test"
    owner = await openevidence.claim_inflight_marker(key, 600)
    assert await openevidence.publish_failure_and_release(key, "someone-else", "boom", 300) is False
    assert await client.get("openevidence_failed:" + key) is None
    assert await client.get(_INFLIGHT_PREFIX + key) == owner.encode()

    assert await openevidence.publish_failure_and_release(key, owner, "boom", 300) is True
    assert await client.get("openevidence_failed:" + key) == b"boom"
    assert 0 < await client.pttl("openevidence_failed:" + key) <= 300_000
    assert await client.get(_INFLIGHT_PREFIX + key) is None


async def test_poller_cannot_slip_a_claim_in_after_a_just_published_failure(fake_redis, per_call_upstream):
    upstream = per_call_upstream
    result, _ = await _request("ALK")  # owner A starts the lookup
    assert result.status == "pending"
    pod_a = main._openevidence_sidecar_tasks[openevidence.sidecar_cache_key("ALK", None, None)]

    async def owner_fails_now() -> None:
        # Pod B has done its reads and is about to claim: A fails right
        # here, publishing its failure and releasing the lease.
        upstream.fail(0, RuntimeError("upstream reset"))
        await asyncio.wait_for(pod_a, timeout=2.0)
        assert len(fake_redis.keys_with_prefix("openevidence_failed:")) == 1

    fake_redis.before_next_lease_write = owner_fails_now
    with _as_other_pod():
        result, _ = await _request("ALK")
        assert main._openevidence_sidecar_tasks == {}

    assert fake_redis.before_next_lease_write is None, "the interleaving hook must have run"
    assert result.status == "failed"
    assert "upstream reset" in result.error
    assert len(upstream.calls) == 1, "no second paid call while the failure record is live"
    assert fake_redis.keys_with_prefix("openevidence_inflight:") == []


async def test_claim_script_answers_a_recorded_failure_on_real_redis():
    client = cache_module._get_client()
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")
    key = "claim-test"
    owner = await openevidence.claim_lookup(key, 600)
    assert owner.token is not None and owner.failed is None
    assert await openevidence.claim_lookup(key, 600) == openevidence.LeaseClaim()  # held
    assert await openevidence.publish_failure_and_release(key, owner.token, "boom", 300) is True
    assert await openevidence.claim_lookup(key, 600) == openevidence.LeaseClaim(failed="boom")
    assert await client.get(_INFLIGHT_PREFIX + key) is None, "a recorded failure blocks the claim"


async def test_best_effort_redis_outage_lets_each_pod_call_once_and_warns(monkeypatch, upstream, caplog):
    """Accepted best-effort behavior, not a guarantee: with Redis down,
    cross-pod dedupe is off (failing closed would hide every card during a
    Redis blip), so two pods each make one paid call for the same key. Each
    pod still makes exactly one (the in-process registry), and every lease
    claim that proceeds without Redis is logged as a WARNING."""

    class DownRedis:
        async def get(self, *args, **kwargs):
            raise ConnectionError("redis down")

        set = delete = eval = get

    monkeypatch.setattr(cache_module, "_client", DownRedis())
    caplog.set_level("WARNING", logger="src.pipeline.openevidence")

    for _ in range(3):  # pod A: repeated polls, one call
        result, _ = await _request("ALK")
        assert result.status == "pending"
    with _as_other_pod():
        for _ in range(3):  # pod B: repeated polls, one more call
            result, _ = await _request("ALK")
            assert result.status == "pending"
        pod_b = list(main._openevidence_sidecar_tasks.values())
    assert len(upstream.calls) == 2

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert sum("proceeding WITHOUT a cross-pod lease" in w for w in warnings) == 2
    upstream.release()
    await asyncio.wait_for(asyncio.gather(*pod_b), timeout=2.0)
    await _wait_for_background_lookups()


async def test_lease_renewal_redis_error_is_logged_as_a_warning(monkeypatch, fake_redis, caplog):
    async def broken_eval(*args, **kwargs):
        raise ConnectionError("redis blip")

    monkeypatch.setattr(fake_redis, "eval", broken_eval)
    caplog.set_level("WARNING", logger="src.pipeline.openevidence")
    assert await openevidence.renew_inflight_marker("k", "token", 600) is True  # best-effort: keep going
    assert any(
        "WITHOUT a confirmed cross-pod lease" in r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    )
