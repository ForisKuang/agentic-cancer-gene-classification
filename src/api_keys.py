"""ACGC API keys for scripted (machine-to-machine) access.

A key looks like ``acgc_<43 urlsafe chars>`` and is sent as
``Authorization: Bearer acgc_...``. Only its SHA-256 hash is persisted (in the
MySQL ``api_keys`` table, see ``RunStore``); the plaintext is returned exactly
once at creation. Keys are looked up by that full hash (unique index), so no
part of the secret is stored, logged, or used to narrow the lookup; keys are
identified in listings and logs by their random, non-secret ``id``.

Everything in this module is pure logic (no DB/HTTP), so it is unit-testable
without MySQL or Redis. Request-time resolution lives in ``src.auth``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from pydantic import BaseModel

logger = logging.getLogger(__name__)

API_KEY_PREFIX = "acgc_"
# secrets.token_urlsafe(32) -> 43 chars of [A-Za-z0-9_-]
_SECRET_BYTES = 32
_SECRET_LEN = 43
_API_KEY_RE = re.compile(rf"^{API_KEY_PREFIX}[A-Za-z0-9_-]{{{_SECRET_LEN}}}$")


class ApiKeyRecord(BaseModel):
    """A stored API key row. Never carries the plaintext secret."""

    id: str
    key_hash: str
    owner_email: str
    name: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


def generate_api_key() -> str:
    """Return a new random plaintext key (``acgc_`` + 32 random bytes, urlsafe)."""
    return API_KEY_PREFIX + secrets.token_urlsafe(_SECRET_BYTES)


def hash_api_key(plaintext: str) -> str:
    """SHA-256 hex digest of the full plaintext key — the only form persisted.

    A plain (unsalted) hash is appropriate here because the secret is 256 bits
    of CSPRNG output, not a guessable password.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def looks_like_api_key(token: Optional[str]) -> bool:
    """True for anything claiming to be an ACGC API key (vs. a session token).

    Session tokens are ``<b64 payload>.<b64 sig>`` and never start with
    ``acgc_``, so the prefix alone routes the token to the right verifier.
    """
    return bool(token) and token.startswith(API_KEY_PREFIX)


def parse_api_key(token: Optional[str]) -> Optional[str]:
    """Return the stripped key if it is well-formed, else None."""
    if not token:
        return None
    candidate = token.strip()
    if not _API_KEY_RE.match(candidate):
        return None
    return candidate


def bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    if authorization_header and authorization_header.startswith("Bearer "):
        token = authorization_header[7:].strip()
        return token or None
    return None


def api_key_matches(plaintext: str, record: ApiKeyRecord) -> bool:
    """Constant-time comparison of the presented key's hash with the stored hash."""
    return hmac.compare_digest(hash_api_key(plaintext), record.key_hash)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def api_key_inactive_reason(record: ApiKeyRecord, now: Optional[datetime] = None) -> Optional[str]:
    """Why a key can't be used ("revoked"/"expired"), or None if usable."""
    now = now or datetime.now(timezone.utc)
    if record.revoked_at is not None:
        return "revoked"
    expires_at = _aware(record.expires_at)
    if expires_at is not None and expires_at <= now:
        return "expired"
    return None


def compute_expires_at(expires_in_days: Optional[int], now: Optional[datetime] = None) -> Optional[datetime]:
    if expires_in_days is None:
        return None
    now = now or datetime.now(timezone.utc)
    return now + timedelta(days=expires_in_days)


def should_touch_last_used(
    last_used_at: Optional[datetime], interval_seconds: int, now: Optional[datetime] = None
) -> bool:
    """Throttle last_used_at writes to at most one per ``interval_seconds`` per key."""
    if last_used_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - _aware(last_used_at)).total_seconds() >= interval_seconds


# ---------------------------------------------------------------------------
# Per-key rate limiting (fixed one-minute windows)
# ---------------------------------------------------------------------------

RATE_LIMIT_WINDOW_SECONDS = 60


def rate_limit_window(now: float, window_seconds: int = RATE_LIMIT_WINDOW_SECONDS) -> Tuple[int, int]:
    """Return (window index, seconds until the window resets — at least 1)."""
    window = int(now // window_seconds)
    retry_after = max(1, int((window + 1) * window_seconds - now + 0.999))
    return window, retry_after


class InMemoryRateLimiter:
    """Process-local fixed-window counter, used when Redis is unreachable.

    Counts are per worker process and reset on restart, so the effective limit
    is ``limit * workers`` — acceptable as a degraded fallback; Redis (shared
    across workers) is the primary backend.
    """

    def __init__(self, window_seconds: int = RATE_LIMIT_WINDOW_SECONDS):
        self._window_seconds = window_seconds
        self._counts: Dict[str, Tuple[int, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, now: Optional[float] = None) -> Tuple[bool, int]:
        """Record one request. Returns (allowed, retry_after_seconds)."""
        now = time.time() if now is None else now
        window, retry_after = rate_limit_window(now, self._window_seconds)
        with self._lock:
            # Drop counters from past windows so the dict stays bounded.
            for stale_key in [k for k, (w, _) in self._counts.items() if w != window]:
                del self._counts[stale_key]
            _, count = self._counts.get(key, (window, 0))
            count += 1
            self._counts[key] = (window, count)
        return count <= limit, retry_after

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


_memory_limiter = InMemoryRateLimiter()


async def check_api_key_rate_limit(
    key_id: str, limit: int, redis_client=None, now: Optional[float] = None
) -> Tuple[bool, int]:
    """Count one request for ``key_id``. Returns (allowed, retry_after_seconds).

    Uses a shared Redis counter (INCR + EXPIRE per window) when available so
    the limit holds across workers; falls back to the in-memory limiter if
    Redis is not configured or errors.
    """
    now = time.time() if now is None else now
    if redis_client is not None:
        window, retry_after = rate_limit_window(now)
        redis_key = f"acgc:apikey:rl:{key_id}:{window}"
        try:
            pipe = redis_client.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, RATE_LIMIT_WINDOW_SECONDS + 5)
            count, _ = await pipe.execute()
            return int(count) <= limit, retry_after
        except Exception as exc:
            logger.warning("Redis API-key rate limiter unavailable, using in-memory fallback: %s", exc)
    return _memory_limiter.hit(key_id, limit, now)
