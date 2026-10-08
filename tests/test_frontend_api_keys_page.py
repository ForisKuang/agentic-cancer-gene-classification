"""The /api-keys page: the route (login redirect, config injection, nav link)
and src/static/api-keys.js itself.

The JS is exercised by tests/frontend/test_api_keys_page.js, a plain Node
script with a fake DOM and scripted fetch (same no-toolchain approach as
tests/test_frontend_openevidence_gate.py): create shows the key once and
clears it, the list renders without secrets, revoke confirms then refreshes,
and HTTP errors map to fixed friendly messages.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import main
from src.auth import AuthenticatedUser, create_session_token
from src.config import settings

_FRONTEND_DIR = Path(__file__).parent / "frontend"
_STATIC_DIR = Path(__file__).parent.parent / "src" / "static"


def test_api_keys_page_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    result = subprocess.run(
        [node, str(_FRONTEND_DIR / "test_api_keys_page.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"frontend API keys page tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "api-keys-page-test-secret")
    monkeypatch.setattr(settings, "public_app_base_url", "https://acgc.example.org/")
    monkeypatch.setattr(settings, "api_key_max_expires_in_days", 180)
    return TestClient(main.app)


def _sign_in(client: TestClient) -> None:
    user = AuthenticatedUser(email="user@mskcc.org", name="User", domain="mskcc.org")
    client.cookies.set(settings.auth_cookie_name, create_session_token(user))


def test_signed_out_redirects_through_login_back_to_page(client):
    response = client.get("/api-keys", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?redirect_to=%2Fapi-keys"


def test_signed_in_serves_page_with_config(client):
    _sign_in(client)
    response = client.get("/api-keys")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    body = response.text
    assert 'data-public-base-url="https://acgc.example.org"' in body
    assert 'data-max-expires-days="180"' in body
    assert "__ACGC_" not in body
    assert '<script src="/static/api-keys.js"></script>' in body


def test_public_base_url_is_html_escaped(client, monkeypatch):
    monkeypatch.setattr(settings, "public_app_base_url", 'https://x.org/"><script>')
    _sign_in(client)
    body = client.get("/api-keys").text
    assert "<script>\"" not in body
    assert 'data-public-base-url="https://x.org/&quot;&gt;&lt;script&gt;"' in body


def test_auth_disabled_serves_page_without_login(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    response = client.get("/api-keys", follow_redirects=False)
    assert response.status_code == 200


def test_signed_in_ui_links_to_api_keys_page():
    index = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'href="/api-keys"' in index
