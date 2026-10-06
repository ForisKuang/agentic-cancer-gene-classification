"""Tests for ACGC API keys: pure key/rate-limit logic, the /v1/api-keys
endpoints, and `Authorization: Bearer acgc_...` auth on protected routes.

The HTTP tests swap app.state.run_store for an in-memory fake implementing the
api-key store methods, so they run without MySQL; the real SQL round-trip is
covered by a MySQL test that skips when no server is reachable.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

from src import api_keys as api_keys_module
from src import auth as auth_module
from src import main as main_module
from src.api_keys import (
    ApiKeyRecord,
    InMemoryRateLimiter,
    api_key_inactive_reason,
    api_key_matches,
    check_api_key_rate_limit,
    compute_expires_at,
    generate_api_key,
    hash_api_key,
    looks_like_api_key,
    parse_api_key,
    rate_limit_window,
    should_touch_last_used,
)
from src.auth import AuthenticatedUser, UserProfile, create_session_token
from src.config import settings
from src.main import app

# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def _record(plaintext: str, **overrides) -> ApiKeyRecord:
    data = dict(
        id="key-1",
        key_hash=hash_api_key(plaintext),
        owner_email="curator@mskcc.org",
        name="test",
        created_at=datetime.now(timezone.utc),
    )
    data.update(overrides)
    return ApiKeyRecord(**data)


def test_generated_key_format_and_parse():
    key = generate_api_key()
    assert key.startswith("acgc_")
    assert len(key) == len("acgc_") + 43
    assert parse_api_key(key) == key
    assert parse_api_key(f"  {key} ") == key
    assert generate_api_key() != key


@pytest.mark.parametrize(
    "token",
    [None, "", "acgc_", "acgc_short", "acgc_" + "a" * 42, "acgc_" + "a" * 44, "acgc_" + "!" * 43, "xyz_" + "a" * 43],
)
def test_parse_api_key_rejects_malformed(token):
    assert parse_api_key(token) is None


def test_looks_like_api_key_distinguishes_session_tokens(monkeypatch):
    monkeypatch.setattr(settings, "auth_secret_key", "unit-secret")
    session = create_session_token(AuthenticatedUser(email="a@mskcc.org", name="A", domain="mskcc.org"))
    assert looks_like_api_key(generate_api_key())
    assert not looks_like_api_key(session)
    assert not looks_like_api_key(None)


def test_hash_is_sha256_and_never_the_plaintext():
    key = generate_api_key()
    digest = hash_api_key(key)
    assert len(digest) == 64
    assert key not in digest
    assert digest == hash_api_key(key)


def test_api_key_matches_constant_time_compare():
    key = generate_api_key()
    record = _record(key)
    assert api_key_matches(key, record)
    assert not api_key_matches(generate_api_key(), record)


def test_inactive_reason():
    key = generate_api_key()
    now = datetime.now(timezone.utc)
    assert api_key_inactive_reason(_record(key)) is None
    assert api_key_inactive_reason(_record(key, revoked_at=now)) == "revoked"
    assert api_key_inactive_reason(_record(key, expires_at=now - timedelta(seconds=1))) == "expired"
    assert api_key_inactive_reason(_record(key, expires_at=now + timedelta(days=1))) is None
    # Naive datetimes (as read back from MySQL) are treated as UTC.
    naive_past = (now - timedelta(hours=1)).replace(tzinfo=None)
    assert api_key_inactive_reason(_record(key, expires_at=naive_past)) == "expired"


def test_compute_expires_at():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert compute_expires_at(None, now) is None
    assert compute_expires_at(30, now) == now + timedelta(days=30)


def test_should_touch_last_used_throttles():
    now = datetime.now(timezone.utc)
    assert should_touch_last_used(None, 60, now)
    assert not should_touch_last_used(now - timedelta(seconds=10), 60, now)
    assert should_touch_last_used(now - timedelta(seconds=61), 60, now)
    assert should_touch_last_used(now, 0, now)


def test_rate_limit_window_retry_after():
    assert rate_limit_window(120.0) == (2, 60)
    assert rate_limit_window(179.5) == (2, 1)
    assert rate_limit_window(150.2)[1] == 30


def test_in_memory_rate_limiter_blocks_then_resets():
    limiter = InMemoryRateLimiter()
    t0 = 600.0
    for _ in range(3):
        assert limiter.hit("k", 3, now=t0)[0]
    allowed, retry_after = limiter.hit("k", 3, now=t0 + 10)
    assert not allowed
    assert retry_after == 50
    # Other keys have their own budget.
    assert limiter.hit("other", 3, now=t0 + 10)[0]
    # Next window starts fresh.
    assert limiter.hit("k", 3, now=t0 + 60)[0]


async def test_rate_limit_falls_back_to_memory_when_redis_errors(monkeypatch):
    monkeypatch.setattr(api_keys_module, "_memory_limiter", InMemoryRateLimiter())

    class BrokenRedis:
        def pipeline(self):
            raise ConnectionError("redis down")

    assert (await check_api_key_rate_limit("k", 1, redis_client=BrokenRedis(), now=60.0))[0]
    assert not (await check_api_key_rate_limit("k", 1, redis_client=BrokenRedis(), now=61.0))[0]


async def test_rate_limit_with_real_redis():
    import redis.asyncio as redis

    # Dedicated DB: other suites (possibly running concurrently against the
    # same server) FLUSHDB db 0 between tests, which would reset the counter.
    base = urlsplit(settings.redis_url or "redis://localhost:6379/0")
    client = redis.from_url(urlunsplit(base._replace(path="/15")), decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        pytest.skip(f"Redis not reachable: {exc}")
    key_id = f"test-{time.time_ns()}"
    try:
        now = time.time()
        assert (await check_api_key_rate_limit(key_id, 2, redis_client=client, now=now))[0]
        assert (await check_api_key_rate_limit(key_id, 2, redis_client=client, now=now))[0]
        allowed, retry_after = await check_api_key_rate_limit(key_id, 2, redis_client=client, now=now)
        assert not allowed
        assert 1 <= retry_after <= 60
    finally:
        window, _ = rate_limit_window(now)
        await client.delete(f"acgc:apikey:rl:{key_id}:{window}")
        await client.aclose()


# ---------------------------------------------------------------------------
# HTTP: endpoints + bearer auth (in-memory fake store)
# ---------------------------------------------------------------------------


class FakeStore:
    """In-memory stand-in for the RunStore api-key methods (plus get_run)."""

    def __init__(self):
        self.keys: Dict[str, ApiKeyRecord] = {}
        self.touches: List[str] = []

    async def create_api_key(self, record: ApiKeyRecord) -> None:
        self.keys[record.id] = record

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKeyRecord]:
        return next((k for k in self.keys.values() if k.key_hash == key_hash), None)

    async def get_api_key(self, key_id: str) -> Optional[ApiKeyRecord]:
        return self.keys.get(key_id)

    async def list_api_keys(self, owner_email: Optional[str] = None) -> List[ApiKeyRecord]:
        return [k for k in self.keys.values() if owner_email is None or k.owner_email == owner_email]

    async def revoke_api_key(self, key_id: str, revoked_at: datetime) -> None:
        self.keys[key_id] = self.keys[key_id].model_copy(update={"revoked_at": revoked_at})

    async def touch_api_key(self, key_id: str, used_at: datetime) -> None:
        self.touches.append(key_id)
        self.keys[key_id] = self.keys[key_id].model_copy(update={"last_used_at": used_at})

    async def get_run(self, run_id: str):
        return None


PROTECTED = "/v1/annotate/does-not-exist"  # require_auth; 404 once authenticated


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "api-key-test-secret-123456")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "api_key_rate_limit_per_minute", 60)
    # No Redis: exercise the in-memory limiter/profile store deterministically.
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(auth_module, "_redis_client", None)
    monkeypatch.setattr(api_keys_module, "_memory_limiter", InMemoryRateLimiter())
    fake = FakeStore()
    previous = getattr(app.state, "run_store", None)
    app.state.run_store = fake
    yield fake
    if previous is None:
        del app.state.run_store
    else:
        app.state.run_store = previous


def _session_client(email: str = "curator@mskcc.org", role: str = "curator") -> TestClient:
    token = create_session_token(
        AuthenticatedUser(email=email, name="Curator", domain=email.split("@")[-1], role=role)
    )
    client = TestClient(app)
    client.cookies.set(settings.auth_cookie_name, token)
    return client


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _create_key(client: TestClient, **body) -> dict:
    resp = client.post("/v1/api-keys", json={"name": "nightly script", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_returns_secret_once_and_list_hides_it(store):
    client = _session_client()
    created = _create_key(client, expires_in_days=30)
    key = created["key"]
    assert parse_api_key(key) == key
    # Nothing derived from the secret is exposed besides the one-time `key`.
    assert "key_prefix" not in created
    assert key[5:13] not in str({k: v for k, v in created.items() if k != "key"})
    assert created["owner_email"] == "curator@mskcc.org"
    assert created["active"] is True
    assert created["expires_at"] is not None

    # Only the hash is stored.
    stored = store.keys[created["id"]]
    assert stored.key_hash == hash_api_key(key)
    assert key not in stored.model_dump_json()

    listed = client.get("/v1/api-keys")
    assert listed.status_code == 200
    body = listed.json()
    assert [k["id"] for k in body] == [created["id"]]
    assert "key" not in body[0]
    assert "key_hash" not in body[0]
    assert "key_prefix" not in body[0]
    assert key[5:13] not in listed.text


def test_create_rejects_expiry_beyond_max(store, monkeypatch):
    monkeypatch.setattr(settings, "api_key_max_expires_in_days", 10)
    resp = _session_client().post("/v1/api-keys", json={"name": "x", "expires_in_days": 11})
    assert resp.status_code == 422


def test_create_requires_auth(store):
    assert TestClient(app).post("/v1/api-keys", json={"name": "x"}).status_code == 401


def test_bearer_key_authenticates_protected_route(store, monkeypatch):
    key = _create_key(_session_client())["key"]
    seen = {}
    real_set_user_context = main_module.set_user_context

    def spy(**kwargs):
        seen.update(kwargs)
        return real_set_user_context(**kwargs)

    monkeypatch.setattr(main_module, "set_user_context", spy)

    client = TestClient(app)
    assert client.get(PROTECTED).status_code == 401
    resp = client.get(PROTECTED, headers=_bearer(key))
    assert resp.status_code == 404  # authenticated; the run simply doesn't exist

    # Datadog user context is the key owner, tagged with the auth method/key id.
    assert seen["user_id"] == "curator@mskcc.org"
    assert seen["auth_method"] == "api_key"
    assert seen["api_key_id"] == next(iter(store.keys))

    # last_used_at is recorded, but throttled to one write per interval.
    assert client.get(PROTECTED, headers=_bearer(key)).status_code == 404
    assert len(store.touches) == 1
    assert store.keys[next(iter(store.keys))].last_used_at is not None


def test_session_bearer_token_still_works(store):
    token = create_session_token(AuthenticatedUser(email="curator@mskcc.org", name="C", domain="mskcc.org"))
    resp = TestClient(app).get(PROTECTED, headers=_bearer(token))
    assert resp.status_code == 404


def test_unknown_and_malformed_keys_rejected(store):
    client = TestClient(app)
    assert client.get(PROTECTED, headers=_bearer(generate_api_key())).status_code == 401
    assert client.get(PROTECTED, headers=_bearer("acgc_not-a-real-key")).status_code == 401


def test_revoked_key_rejected(store):
    session = _session_client()
    created = _create_key(session)
    client = TestClient(app)
    assert client.get(PROTECTED, headers=_bearer(created["key"])).status_code == 404

    resp = session.delete(f"/v1/api-keys/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["active"] is False
    assert resp.json()["revoked_at"] is not None

    assert client.get(PROTECTED, headers=_bearer(created["key"])).status_code == 401


def test_expired_key_rejected(store):
    created = _create_key(_session_client())
    store.keys[created["id"]] = store.keys[created["id"]].model_copy(
        update={"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}
    )
    assert TestClient(app).get(PROTECTED, headers=_bearer(created["key"])).status_code == 401


def test_disallowed_domain_key_rejected(store, monkeypatch):
    created = _create_key(_session_client())
    client = TestClient(app)
    assert client.get(PROTECTED, headers=_bearer(created["key"])).status_code == 404
    # Owner's domain is offboarded after the key was issued.
    monkeypatch.setattr(settings, "allowed_email_domains", "openevidence.com")
    assert client.get(PROTECTED, headers=_bearer(created["key"])).status_code == 401


def test_disallowed_user_key_rejected(store, monkeypatch):
    created = _create_key(_session_client())
    monkeypatch.setattr(settings, "allowed_emails", "someone.else@mskcc.org")
    assert TestClient(app).get(PROTECTED, headers=_bearer(created["key"])).status_code == 401


def test_api_key_caller_cannot_create_keys(store):
    key = _create_key(_session_client())["key"]
    resp = TestClient(app).post("/v1/api-keys", json={"name": "minted"}, headers=_bearer(key))
    assert resp.status_code == 403
    assert len(store.keys) == 1


def test_api_key_caller_cannot_list_or_revoke_keys(store):
    created = _create_key(_session_client())
    client = TestClient(app)
    assert client.get("/v1/api-keys", headers=_bearer(created["key"])).status_code == 403
    assert client.delete(f"/v1/api-keys/{created['id']}", headers=_bearer(created["key"])).status_code == 403
    assert store.keys[created["id"]].revoked_at is None
    assert client.get(PROTECTED, headers=_bearer(created["key"])).status_code == 404


def test_admin_api_key_cannot_manage_other_users_keys(store, monkeypatch):
    alice_key = _create_key(_session_client("alice@mskcc.org"))
    admin_key = _create_key(_session_client("admin@mskcc.org", role="admin"))
    # The admin key's owner has the admin role in their JIT profile.
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setitem(
        auth_module._user_store,
        "admin@mskcc.org",
        UserProfile(
            email="admin@mskcc.org", name="Admin", domain="mskcc.org", role="admin",
            first_login_at=now, last_login_at=now,
        ),
    )
    client = TestClient(app)
    assert client.get("/v1/api-keys?all=true", headers=_bearer(admin_key["key"])).status_code == 403
    assert client.delete(f"/v1/api-keys/{alice_key['id']}", headers=_bearer(admin_key["key"])).status_code == 403
    assert store.keys[alice_key["id"]].revoked_at is None


def test_users_cannot_see_or_revoke_others_keys_but_admins_can(store):
    alice = _session_client("alice@mskcc.org")
    bob = _session_client("bob@mskcc.org")
    admin = _session_client("admin@mskcc.org", role="admin")
    alice_key = _create_key(alice)
    _create_key(bob)

    assert [k["owner_email"] for k in bob.get("/v1/api-keys").json()] == ["bob@mskcc.org"]
    assert bob.get("/v1/api-keys?all=true").status_code == 403
    assert bob.delete(f"/v1/api-keys/{alice_key['id']}").status_code == 404
    assert store.keys[alice_key["id"]].revoked_at is None

    all_keys = admin.get("/v1/api-keys?all=true")
    assert all_keys.status_code == 200
    assert {k["owner_email"] for k in all_keys.json()} == {"alice@mskcc.org", "bob@mskcc.org"}
    assert admin.delete(f"/v1/api-keys/{alice_key['id']}").status_code == 200
    assert store.keys[alice_key["id"]].revoked_at is not None


def test_rate_limit_returns_429_with_retry_after(store, monkeypatch):
    monkeypatch.setattr(settings, "api_key_rate_limit_per_minute", 2)
    key = _create_key(_session_client())["key"]
    client = TestClient(app)
    statuses = [client.get(PROTECTED, headers=_bearer(key)).status_code for _ in range(2)]
    assert statuses == [404, 404]
    resp = client.get(PROTECTED, headers=_bearer(key))
    assert resp.status_code == 429
    assert 1 <= int(resp.headers["Retry-After"]) <= 60


def test_session_requests_are_not_rate_limited_by_api_key_limit(store, monkeypatch):
    monkeypatch.setattr(settings, "api_key_rate_limit_per_minute", 1)
    client = _session_client()
    assert [client.get(PROTECTED).status_code for _ in range(3)] == [404, 404, 404]


def test_store_unavailable_returns_503(store):
    key = _create_key(_session_client())["key"]
    del app.state.run_store
    try:
        resp = TestClient(app).get(PROTECTED, headers=_bearer(key))
    finally:
        app.state.run_store = store
    assert resp.status_code == 503


def test_store_error_during_lookup_fails_closed_with_503(store, monkeypatch):
    key = _create_key(_session_client())["key"]

    async def boom(key_hash):
        raise ConnectionError("mysql down")

    monkeypatch.setattr(store, "get_api_key_by_hash", boom)
    resp = TestClient(app).get(PROTECTED, headers=_bearer(key))
    assert resp.status_code == 503


def test_emitted_logs_carry_auth_method_and_key_id_but_no_secret(store, caplog):
    created = _create_key(_session_client())
    key = created["key"]
    caplog.set_level(logging.DEBUG, logger="src.auth")
    caplog.set_level(logging.DEBUG, logger="src.api_keys_routes")
    client = TestClient(app)
    assert client.get(PROTECTED, headers=_bearer(key)).status_code == 404
    assert client.get(PROTECTED, headers=_bearer(generate_api_key())).status_code == 401

    authed = [r for r in caplog.records if "authenticated with API key" in r.getMessage()]
    assert authed, "expected an API-key auth log record"
    record = authed[-1]
    assert getattr(record, "acgc.auth_method") == "api_key"
    assert getattr(record, "acgc.api_key_id") == created["id"]
    assert getattr(record, "usr.id") == "curator@mskcc.org"
    assert created["id"] in record.getMessage()

    # Also check creation logs: no part of the secret ever reaches a log line.
    _create_key(_session_client())
    secret = key[len("acgc_"):]
    for r in caplog.records:
        message = r.getMessage()
        assert secret[:8] not in message
        assert key not in message


def test_auth_disabled_ignores_api_keys(store, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    client = TestClient(app)
    # Same as before this feature: everything is open as the local developer.
    assert client.get(PROTECTED).status_code == 404
    assert client.get(PROTECTED, headers=_bearer(generate_api_key())).status_code == 404
    assert client.post("/v1/api-keys", json={"name": "x"}).status_code == 400


def test_cors_allows_authorization_header():
    resp = TestClient(app).options(
        "/v1/api-keys",
        headers={
            "Origin": "https://example.org",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert resp.status_code == 200
    assert "authorization" in resp.headers["access-control-allow-headers"].lower()


def test_cors_preflight_allows_delete():
    resp = TestClient(app).options(
        "/v1/api-keys/some-id",
        headers={
            "Origin": "https://example.org",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert resp.status_code == 200
    assert "DELETE" in resp.headers["access-control-allow-methods"]


# ---------------------------------------------------------------------------
# MySQL round-trip (skipped without a reachable MySQL)
# ---------------------------------------------------------------------------


@pytest.fixture
async def run_store():
    from src.pipeline.run_store import RunStore

    try:
        rs = await RunStore.create()
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}")
    yield rs
    await rs.close()


async def test_run_store_api_key_round_trip(run_store):
    import uuid

    key = generate_api_key()
    owner = f"rt-{uuid.uuid4().hex[:8]}@mskcc.org"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    record = _record(key, id=str(uuid.uuid4()), owner_email=owner, created_at=now, expires_at=now + timedelta(days=1))
    await run_store.create_api_key(record)

    found = await run_store.get_api_key_by_hash(hash_api_key(key))
    assert found is not None and found.id == record.id
    assert api_key_matches(key, found)
    assert found.expires_at == record.expires_at
    assert await run_store.get_api_key_by_hash(hash_api_key(generate_api_key())) is None

    assert [r.id for r in await run_store.list_api_keys(owner_email=owner)] == [record.id]
    assert record.id in {r.id for r in await run_store.list_api_keys()}

    await run_store.touch_api_key(record.id, now)
    assert (await run_store.get_api_key(record.id)).last_used_at == now

    await run_store.revoke_api_key(record.id, now)
    revoked = await run_store.get_api_key(record.id)
    assert revoked.revoked_at == now
    assert api_key_inactive_reason(revoked) == "revoked"
    assert await run_store.get_api_key("missing") is None
