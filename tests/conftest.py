"""Shared test fixtures."""

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
