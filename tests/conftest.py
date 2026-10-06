"""Shared test fixtures."""

import sys
from typing import Dict, List, Optional, Tuple

import pytest

from src.pipeline import cache as cache_module
from src.pipeline import literature as literature_module


@pytest.fixture(autouse=True)
async def _reset_cache_client():
    """Reset the cache module's Redis client singleton before each test.

    redis.asyncio.Redis binds its connection pool to the event loop active
    at creation time. pytest-asyncio creates a fresh event loop per test
    function by default, so a client cached from an earlier test becomes
    unusable in a later test's loop — reset it so each test gets its own.
    """
    cache_module._client = None
    client = cache_module._get_client()
    try:
        await client.flushdb()
    except Exception:
        pass  # Redis not reachable — tests relying on it will skip themselves
    yield
    cache_module._client = None


@pytest.fixture(autouse=True)
async def _reset_ncbi_client():
    """Reset the pooled NCBI httpx client singleton before each test.

    Same event-loop-binding issue as the Redis client above — httpx.AsyncClient's
    connection pool is bound to the loop active at creation time.
    """
    literature_module._ncbi_client = None
    yield
    client = literature_module._ncbi_client
    literature_module._ncbi_client = None
    if client is not None:
        await client.aclose()


@pytest.fixture(autouse=True)
def _reset_ncbi_rate_limiter():
    """Reset the NCBI rate limiter singleton before each test.

    Same event-loop-binding issue as the Redis and httpx clients above —
    AsyncLimiter binds internal asyncio primitives to the loop active at
    creation time.
    """
    literature_module._ncbi_rate_limiter = None
    yield
    literature_module._ncbi_rate_limiter = None


@pytest.fixture(autouse=True)
def _default_auth_disabled(monkeypatch):
    """Ensure tests run with auth_enabled=False by default (individual auth tests monkeypatch it to True)."""
    from src.config import settings
    monkeypatch.setattr(settings, "auth_enabled", False)


@pytest.fixture(autouse=True)
async def _reset_openevidence_sidecar(monkeypatch):
    """Cancel any OpenEvidence sidecar lookup a test left running and clear
    the sidecar's module-level state (task registry, result/failure memos,
    per-limit semaphores) after every test.

    The sidecar's background lookups outlive the request that started them
    by design, so a test that returns "pending" leaves a live task behind; a
    lookup stranded on a closed event loop (e.g. a TestClient portal loop)
    can also hold a semaphore slot forever. Requesting `monkeypatch` makes
    this teardown run before the test's monkeypatches (fake Redis, fake
    upstream) are undone, so cancelled lookups clean up against the fakes.
    Only touches src.main if a test already imported it.
    """
    yield
    main = sys.modules.get("src.main")
    if main is not None:
        await main._cancel_openevidence_sidecar_lookups()
        main._reset_openevidence_sidecar_state()


class FakeRedis:
    """Just enough of redis.asyncio.Redis (get/set with ex|px+nx/delete, and
    eval of the sidecar's two lease scripts) for the sidecar's cache and
    marker keys, with a manually advanced clock so TTL expiry is
    deterministic."""

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

    async def set(self, key: str, value, ex: Optional[float] = None, px: Optional[int] = None, nx: bool = False):
        if nx and self._live(key) is not None:
            return None
        data = value.encode() if isinstance(value, str) else value
        ttl = px / 1000 if px else ex
        self._store[key] = (data, self.now + ttl if ttl else None)
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self._store.pop(key, None) is not None)

    async def eval(self, script: str, numkeys: int, *keys_and_args):
        """The sidecar's compare-and-renew / compare-and-delete lease
        scripts (src.pipeline.openevidence), with the same semantics."""
        from src.pipeline import openevidence

        key, token, *rest = keys_and_args
        current = self._live(key)
        if current is None or current.decode() != str(token):
            return 0
        if script == openevidence._RENEW_LEASE_SCRIPT:
            self._store[key] = (current, self.now + int(rest[0]) / 1000)
            return 1
        if script == openevidence._RELEASE_LEASE_SCRIPT:
            del self._store[key]
            return 1
        raise NotImplementedError("FakeRedis.eval only knows the sidecar lease scripts")

    async def flushdb(self) -> None:
        self._store.clear()


@pytest.fixture
def fake_redis(monkeypatch):
    """Install an in-memory FakeRedis as the cache module's client, so a test
    never touches the shared real Redis (which other test runs may be
    flushing) or uses a client bound to another event loop."""
    from src.pipeline import cache as cache_module

    redis = FakeRedis()
    monkeypatch.setattr(cache_module, "_client", redis)
    return redis
