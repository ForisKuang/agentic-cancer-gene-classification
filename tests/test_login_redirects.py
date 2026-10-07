"""Login return targets stay local and survive every provider round trip."""

import base64
import hashlib
import hmac
import json
import re
from html import unescape
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from src import main
from src.auth import (
    AuthenticatedUser,
    SAMLAssertionData,
    create_oauth_state,
    create_session_token,
    validate_redirect_to,
    verify_oauth_state,
)
from src.config import settings


TARGET = "/?run=abc&view=details"
BAD_TARGETS = [
    "//evil.com",
    "https://evil.com",
    "/\\evil.com",
    "javascript:alert(1)",
    "/foo\\bar",
    "/\t/evil.com",
    "",
    None,
    123,
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "login-redirect-test-secret")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "saml_enabled", True)
    monkeypatch.setattr(settings, "saml_idp_sso_url", "https://idp.example/sso")
    monkeypatch.setattr(settings, "saml_allowed_groups", "")
    monkeypatch.setattr(settings, "keycloak_url", "https://idp.example")
    monkeypatch.setattr(settings, "keycloak_client_id", "test-client")
    monkeypatch.setattr(settings, "keycloak_allowed_roles", "")
    monkeypatch.setattr(settings, "google_client_id", "test-client")
    monkeypatch.setattr(settings, "dev_login_enabled", True)
    user = AuthenticatedUser(email="user@mskcc.org", name="User", domain="mskcc.org")
    monkeypatch.setattr(main, "provision_or_update_user", AsyncMock(return_value=user))
    monkeypatch.setattr(main, "record_user_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        main, "exchange_google_code", AsyncMock(return_value={"access_token": "token"})
    )
    monkeypatch.setattr(
        main,
        "get_google_user_info",
        AsyncMock(
            return_value={
                "email": user.email,
                "email_verified": True,
            }
        ),
    )
    monkeypatch.setattr(
        main, "exchange_keycloak_code", AsyncMock(return_value={"access_token": "token"})
    )
    monkeypatch.setattr(main, "get_keycloak_user_info", AsyncMock(return_value={}))
    monkeypatch.setattr(main, "parse_keycloak_user", lambda *args: user)
    monkeypatch.setattr(
        main, "parse_saml_response", lambda *args: SAMLAssertionData(name_id=user.email)
    )
    return TestClient(main.app), user


@pytest.mark.parametrize("target", BAD_TARGETS)
def test_validator_rejects_external_targets(target):
    assert validate_redirect_to(target) == "/"


@pytest.mark.parametrize("target", ["/", TARGET, "/results/abc?query=a%26b#section"])
def test_validator_preserves_local_targets(target):
    assert validate_redirect_to(target) == target


@pytest.mark.parametrize("session", ["missing", "expired", "valid"])
def test_root_login_redirect(client, session):
    browser, user = client
    if session != "missing":
        browser.cookies.set(
            settings.auth_cookie_name,
            create_session_token(
                user,
                ttl_seconds=-10 if session == "expired" else 3600,
            ),
        )
    response = browser.get("/?run=abc", follow_redirects=False)
    if session == "valid":
        assert response.status_code == 200
    else:
        assert response.status_code == 303
        assert response.headers["location"] == "/login?redirect_to=%2F%3Frun%3Dabc"


@pytest.mark.parametrize("target", [TARGET, *BAD_TARGETS[:6]])
@pytest.mark.parametrize("provider", ["google", "keycloak", "saml", "dev"])
def test_provider_round_trip(client, provider, target):
    browser, _ = client
    expected = validate_redirect_to(target)
    route = "/auth/login" if provider == "google" else f"/auth/{provider}/login"
    response = browser.get(route, params={"redirect_to": target}, follow_redirects=False)
    assert response.status_code in (200, 303, 307)
    if provider == "dev":
        # The selector must carry the target into its identity links.
        link = re.search(r'href="([^"]+email=curator[^"]+)"', response.text).group(1)
        response = browser.get(unescape(link), follow_redirects=False)
    else:
        query = parse_qs(urlsplit(response.headers["location"]).query)
        if provider == "saml":
            assert query["RelayState"] == [expected]
            response = browser.post(
                "/auth/saml/acs",
                data={
                    "SAMLResponse": "assertion",
                    "RelayState": query["RelayState"][0],
                },
                follow_redirects=False,
            )
        else:
            state = query["state"][0]
            assert verify_oauth_state(state)["redirect_to"] == expected
            response = browser.get(
                f"/auth/callback/{provider}",
                params={
                    "code": "code",
                    "state": state,
                },
                follow_redirects=False,
            )
    assert response.status_code == 303
    assert response.headers["location"] == expected
    assert browser.get(expected, follow_redirects=False).status_code == 200


@pytest.mark.parametrize("provider", ["google", "keycloak", "saml"])
@pytest.mark.parametrize("target", BAD_TARGETS[:6])
def test_callback_rejects_unsafe_legacy_state(client, provider, target):
    browser, _ = client
    if provider == "saml":
        response = browser.post(
            "/auth/saml/acs",
            json={
                "SAMLResponse": "assertion",
                "RelayState": target,
            },
            follow_redirects=False,
        )
    else:
        # Sign a legacy payload directly so creation-time validation cannot mask
        # a missing callback check.
        payload = verify_oauth_state(create_oauth_state())
        payload["redirect_to"] = target
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        signature = hmac.new(
            settings.auth_secret_key.encode(), encoded.encode(), hashlib.sha256
        ).digest()
        state = encoded + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")
        response = browser.get(
            f"/auth/callback/{provider}",
            params={
                "code": "code",
                "state": state,
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.parametrize("target", [TARGET, *BAD_TARGETS[:6]])
def test_authenticated_login_page(client, target):
    browser, user = client
    browser.cookies.set(settings.auth_cookie_name, create_session_token(user))
    response = browser.get("/login", params={"redirect_to": target}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == validate_redirect_to(target)


@pytest.mark.parametrize("provider", ["keycloak", "saml", "dev"])
def test_fallback_provider_preserves_query(client, monkeypatch, provider):
    browser, _ = client
    monkeypatch.setattr(settings, "google_client_id", "")
    if provider != "keycloak":
        monkeypatch.setattr(settings, "keycloak_client_id", "")
    if provider == "dev":
        monkeypatch.setattr(settings, "saml_enabled", False)
    response = browser.get("/auth/login", params={"redirect_to": TARGET}, follow_redirects=False)
    location = urlsplit(response.headers["location"])
    assert location.path == f"/auth/{provider}/login"
    assert parse_qs(location.query) == {"redirect_to": [TARGET]}
