from __future__ import annotations

import base64
import zlib
from typing import Optional
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from src.auth import (
    AuthenticatedUser,
    build_saml_authn_request,
    create_oauth_state,
    create_session_token,
    decode_session_token,
    is_email_allowed,
    is_saml_group_allowed,
    list_user_profiles,
    parse_saml_response,
    provision_or_update_user,
    verify_oauth_state,
)
from src.config import settings
from src.main import app


def test_is_email_allowed_domains(monkeypatch):
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    monkeypatch.setattr(settings, "allowed_emails", "")

    # Allowed domains
    ok, _ = is_email_allowed("oncologist@mskcc.org")
    assert ok is True

    ok, _ = is_email_allowed("RESEARCHER@OPENEVIDENCE.COM")
    assert ok is True

    ok, _ = is_email_allowed("  user.name+tag@mskcc.org  ")
    assert ok is True

    # Blocked domains
    ok, reason = is_email_allowed("user@gmail.com")
    assert ok is False
    assert "@gmail.com" in reason
    assert "@mskcc.org" in reason

    ok, reason = is_email_allowed("attacker@columbia.edu")
    assert ok is False

    ok, reason = is_email_allowed("invalid-email")
    assert ok is False


def test_is_email_allowed_with_user_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    monkeypatch.setattr(settings, "allowed_emails", "curator@mskcc.org,scientist@openevidence.com")

    # In allowlist
    ok, _ = is_email_allowed("curator@mskcc.org")
    assert ok is True

    ok, _ = is_email_allowed("scientist@openevidence.com")
    assert ok is True

    # Matching domain, but not in user allowlist
    ok, reason = is_email_allowed("other@mskcc.org")
    assert ok is False
    assert "not on the authorized user list" in reason


def test_session_token_lifecycle(monkeypatch):
    monkeypatch.setattr(settings, "auth_secret_key", "test-secret-key-1234567890-abcdef")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    monkeypatch.setattr(settings, "allowed_emails", "")

    user = AuthenticatedUser(
        email="doctor@mskcc.org",
        name="Dr. Smith",
        picture="https://example.com/pic.jpg",
        domain="mskcc.org",
        role="curator",
        provider="google",
    )

    token = create_session_token(user, ttl_seconds=3600)
    assert "." in token

    decoded = decode_session_token(token)
    assert decoded is not None
    assert decoded.email == "doctor@mskcc.org"
    assert decoded.name == "Dr. Smith"
    assert decoded.domain == "mskcc.org"
    assert decoded.role == "curator"

    # Tampered token
    tampered = token[:-4] + "xxxx"
    assert decode_session_token(tampered) is None

    # Expired token
    expired_token = create_session_token(user, ttl_seconds=-10)
    assert decode_session_token(expired_token) is None


def test_oauth_state_lifecycle(monkeypatch):
    monkeypatch.setattr(settings, "auth_secret_key", "test-secret-key-1234567890-abcdef")

    state = create_oauth_state(redirect_to="/results/job-123")
    assert "." in state

    payload = verify_oauth_state(state)
    assert payload is not None
    assert payload["redirect_to"] == "/results/job-123"
    assert "nonce" in payload

    # Tampered state
    assert verify_oauth_state(state + "corrupt") is None


def test_auth_me_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "secret-key-for-testing")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    client = TestClient(app)

    # Unauthenticated
    resp = client.get("/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["auth_enabled"] is True
    assert data["authenticated"] is False
    assert data["user"] is None
    assert "mskcc.org" in data["allowed_domains"]

    # Authenticated with session cookie
    user = AuthenticatedUser(
        email="curator@openevidence.com",
        name="Curation Lead",
        domain="openevidence.com",
    )
    token = create_session_token(user)
    client.cookies.set(settings.auth_cookie_name, token)

    resp2 = client.get("/auth/me")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["authenticated"] is True
    assert data2["user"]["email"] == "curator@openevidence.com"
    assert data2["user"]["domain"] == "openevidence.com"


def test_auth_logout(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "secret-key-for-testing")
    client = TestClient(app)

    client.cookies.set(settings.auth_cookie_name, "dummy-token")
    resp = client.get("/auth/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    # Check Set-Cookie clears the cookie
    assert any(
        settings.auth_cookie_name in cookie and ('max-age=0' in cookie.lower() or 'expires=' in cookie.lower() or '""' in cookie)
        for cookie in resp.headers.get_list("set-cookie")
    )


def test_dev_login_allowed_and_blocked(monkeypatch):
    monkeypatch.setattr(settings, "dev_login_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "secret-key-for-testing")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    client = TestClient(app)

    # MSK allowed
    resp_msk = client.get(
        "/auth/dev/login?email=doc@mskcc.org&name=MSK+Doctor",
        follow_redirects=False,
    )
    assert resp_msk.status_code == 303
    assert resp_msk.headers["location"] == "/"
    assert any(settings.auth_cookie_name in c for c in resp_msk.headers.get_list("set-cookie"))

    # OpenEvidence allowed
    resp_oe = client.get(
        "/auth/dev/login?email=staff@openevidence.com&name=Staff",
        follow_redirects=False,
    )
    assert resp_oe.status_code == 303
    assert resp_oe.headers["location"] == "/"

    # Gmail blocked with 403 Access Denied
    resp_blocked = client.get(
        "/auth/dev/login?email=intruder@gmail.com",
        follow_redirects=False,
    )
    assert resp_blocked.status_code == 403
    assert "Domain Not Authorized" in resp_blocked.text
    assert "intruder@gmail.com" in resp_blocked.text
    assert "@mskcc.org" in resp_blocked.text


def test_protected_routes_enforce_auth_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "secret-key-for-testing")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    client = TestClient(app)

    # 1. Unauthenticated request to protected endpoint -> 401
    resp = client.get("/v1/annotate/jobs/nonexistent-id")
    assert resp.status_code == 401
    assert "Authentication required" in resp.json()["detail"]

    # 2. Authenticated request with @mskcc.org cookie -> proceeds past auth (returns 404 because job doesn't exist)
    user = AuthenticatedUser(
        email="oncologist@mskcc.org",
        name="Oncologist",
        domain="mskcc.org",
    )
    token = create_session_token(user)
    client.cookies.set(settings.auth_cookie_name, token)

    resp2 = client.get("/v1/annotate/jobs/nonexistent-id")
    assert resp2.status_code == 404
    assert resp2.json()["detail"] == "Annotation job not found"

    # 3. Authenticated request using Authorization: Bearer header
    client.cookies.clear()
    resp3 = client.get(
        "/v1/annotate/jobs/nonexistent-id",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp3.status_code == 404


@pytest.mark.asyncio
async def test_google_callback_flow(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "secret-key-for-testing")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    monkeypatch.setattr(settings, "google_client_id", "mock-google-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "mock-google-client-secret")
    client = TestClient(app)

    state = create_oauth_state(redirect_to="/results/target-job")

    # Mock token exchange and userinfo retrieval
    fake_tokens = {"access_token": "mock-access-token-123"}

    # Case A: Successful MSK login
    fake_userinfo_msk = {
        "email": "doctor@mskcc.org",
        "email_verified": True,
        "name": "Dr. MSK",
        "picture": "https://avatar.example.com/msk.png",
    }
    with patch("src.main.exchange_google_code", AsyncMock(return_value=fake_tokens)), \
         patch("src.main.get_google_user_info", AsyncMock(return_value=fake_userinfo_msk)):

        resp = client.get(
            f"/auth/callback/google?code=fake-code&state={state}",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/results/target-job"
        assert any(settings.auth_cookie_name in c for c in resp.headers.get_list("set-cookie"))

    # Case B: Successful OpenEvidence login
    fake_userinfo_oe = {
        "email": "curator@openevidence.com",
        "email_verified": True,
        "name": "OE Curator",
        "picture": "https://avatar.example.com/oe.png",
    }
    with patch("src.main.exchange_google_code", AsyncMock(return_value=fake_tokens)), \
         patch("src.main.get_google_user_info", AsyncMock(return_value=fake_userinfo_oe)):

        resp = client.get(
            f"/auth/callback/google?code=fake-code&state={state}",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/results/target-job"

    # Case C: Unauthorized domain rejected with 403
    fake_userinfo_unauthorized = {
        "email": "intruder@gmail.com",
        "email_verified": True,
        "name": "Intruder",
    }
    with patch("src.main.exchange_google_code", AsyncMock(return_value=fake_tokens)), \
         patch("src.main.get_google_user_info", AsyncMock(return_value=fake_userinfo_unauthorized)):

        resp = client.get(
            f"/auth/callback/google?code=fake-code&state={state}",
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert "Domain Not Authorized" in resp.text
        assert "intruder@gmail.com" in resp.text

    # Case D: Unverified email rejected with 403
    fake_userinfo_unverified = {
        "email": "doctor@mskcc.org",
        "email_verified": False,
        "name": "Unverified Doctor",
    }
    with patch("src.main.exchange_google_code", AsyncMock(return_value=fake_tokens)), \
         patch("src.main.get_google_user_info", AsyncMock(return_value=fake_userinfo_unverified)):

        resp = client.get(
            f"/auth/callback/google?code=fake-code&state={state}",
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert "unverified" in resp.text


def make_mock_saml_response(
    email: str,
    name: str = "",
    groups: Optional[list] = None,
    issuer: str = "https://idp.mskcc.org",
    status_success: bool = True,
) -> str:
    groups = groups or []
    status_code = (
        "urn:oasis:names:tc:SAML:2.0:status:Success"
        if status_success
        else "urn:oasis:names:tc:SAML:2.0:status:AuthnFailed"
    )
    group_values = "".join(f"<saml:AttributeValue>{g}</saml:AttributeValue>" for g in groups)
    xml = f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_resp_{email}" Version="2.0">
  <samlp:Status>
    <samlp:StatusCode Value="{status_code}"/>
  </samlp:Status>
  <saml:Assertion ID="_assert_{email}" Version="2.0">
    <saml:Issuer>{issuer}</saml:Issuer>
    <saml:Subject>
      <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{email}</saml:NameID>
    </saml:Subject>
    <saml:AuthnStatement SessionIndex="_session_{email}"/>
    <saml:AttributeStatement>
      <saml:Attribute Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress">
        <saml:AttributeValue>{email}</saml:AttributeValue>
      </saml:Attribute>
      <saml:Attribute Name="http://schemas.microsoft.com/identity/claims/displayname">
        <saml:AttributeValue>{name or email}</saml:AttributeValue>
      </saml:Attribute>
      <saml:Attribute Name="http://schemas.microsoft.com/ws/2008/06/identity/claims/groups">
        {group_values}
      </saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>"""
    return base64.b64encode(xml.encode("utf-8")).decode("utf-8")


def test_build_saml_authn_request(monkeypatch):
    monkeypatch.setattr(settings, "saml_sp_entity_id", "https://cancer-gene.mskcc.org/sp")
    monkeypatch.setattr(settings, "saml_idp_sso_url", "https://sso.mskcc.org/saml2/idp")

    acs_url = "https://cancer-gene.mskcc.org/auth/saml/acs"
    req_id, redirect_url = build_saml_authn_request(acs_url=acs_url, relay_state="/dashboard")

    assert req_id.startswith("id_")
    assert "https://sso.mskcc.org/saml2/idp" in redirect_url
    assert "SAMLRequest=" in redirect_url
    assert "RelayState=%2Fdashboard" in redirect_url

    parsed = urlparse(redirect_url)
    qs = parse_qs(parsed.query)
    saml_req_b64 = qs["SAMLRequest"][0]

    # Decompress raw DEFLATE stream (RFC 1951)
    raw_deflated = base64.b64decode(saml_req_b64)
    decompressed = zlib.decompress(raw_deflated, -zlib.MAX_WBITS).decode("utf-8")

    assert req_id in decompressed
    assert 'AssertionConsumerServiceURL="https://cancer-gene.mskcc.org/auth/saml/acs"' in decompressed
    assert "https://cancer-gene.mskcc.org/sp" in decompressed


def test_parse_saml_response_and_claims():
    b64_response = make_mock_saml_response(
        email="lead.curator@mskcc.org",
        name="Lead Curator Jane",
        groups=["MSK-OncoKB-Curators", "MSK-All-Staff"],
        issuer="https://sts.windows.net/mskcc-tenant/",
    )

    assertion = parse_saml_response(b64_response)
    assert assertion.name_id == "lead.curator@mskcc.org"
    assert assertion.display_name == "Lead Curator Jane"
    assert "MSK-OncoKB-Curators" in assertion.groups
    assert "MSK-All-Staff" in assertion.groups
    assert assertion.issuer == "https://sts.windows.net/mskcc-tenant/"
    assert assertion.session_index == "_session_lead.curator@mskcc.org"


def test_is_saml_group_allowed(monkeypatch):
    monkeypatch.setattr(settings, "saml_allowed_groups", "MSK-OncoKB-Curators,OE-Genomics-Team")

    # Matching groups
    ok, _ = is_saml_group_allowed(["MSK-OncoKB-Curators", "General-Staff"])
    assert ok is True

    ok, _ = is_saml_group_allowed(["OE-Genomics-Team"])
    assert ok is True

    # Non-matching groups
    ok, reason = is_saml_group_allowed(["General-Staff", "Other-Department"])
    assert ok is False
    assert "Access requires membership in one of" in reason
    assert "MSK-OncoKB-Curators" in reason

    # No group restriction configured -> all permitted
    monkeypatch.setattr(settings, "saml_allowed_groups", "")
    ok, _ = is_saml_group_allowed(["Any-Group"])
    assert ok is True


def test_saml_metadata_endpoint():
    client = TestClient(app)
    resp = client.get("/auth/saml/metadata")
    assert resp.status_code == 200
    assert "application/xml" in resp.headers["content-type"]
    assert "EntityDescriptor" in resp.text
    assert "/auth/saml/acs" in resp.text


def test_saml_login_redirect(monkeypatch):
    monkeypatch.setattr(settings, "saml_enabled", True)
    monkeypatch.setattr(settings, "saml_idp_sso_url", "https://login.microsoftonline.com/tenant/saml2")
    client = TestClient(app)

    resp = client.get("/auth/saml/login?redirect_to=/jobs/123", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"].startswith("https://login.microsoftonline.com/tenant/saml2?")
    assert "SAMLRequest=" in resp.headers["location"]
    assert "RelayState=%2Fjobs%2F123" in resp.headers["location"]


@pytest.mark.asyncio
async def test_saml_acs_success_and_jit_role_mapping(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "saml_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "saml-secret-key-12345")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    monkeypatch.setattr(settings, "saml_allowed_groups", "MSK-Curators")
    monkeypatch.setattr(settings, "saml_admin_groups", "MSK-Admins")

    client = TestClient(app)
    b64_response = make_mock_saml_response(
        email="lead.admin@mskcc.org",
        name="Lead Administrator",
        groups=["MSK-Curators", "MSK-Admins"],
    )

    # Post SAMLResponse via form POST (simulating IdP redirect)
    resp = client.post(
        "/auth/saml/acs",
        data={"SAMLResponse": b64_response, "RelayState": "/results/annot-456"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/results/annot-456"

    # Verify session cookie was set
    cookie_headers = resp.headers.get_list("set-cookie")
    assert any(settings.auth_cookie_name in c for c in cookie_headers)

    # Extract session cookie and decode token
    session_cookie = resp.cookies.get(settings.auth_cookie_name)
    user = decode_session_token(session_cookie)
    assert user is not None
    assert user.email == "lead.admin@mskcc.org"
    assert user.name == "Lead Administrator"
    assert user.domain == "mskcc.org"
    assert user.role == "admin"  # Mapped from MSK-Admins!
    assert user.provider == "saml"
    assert "MSK-Curators" in user.groups


def test_saml_acs_rejections(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "saml_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "saml-secret-key-12345")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    monkeypatch.setattr(settings, "saml_allowed_groups", "MSK-OncoKB-Curators")

    client = TestClient(app)

    # Case A: Unauthorized domain in SAML assertion
    b64_unauth_domain = make_mock_saml_response(
        email="attacker@external-domain.org",
        name="External User",
        groups=["MSK-OncoKB-Curators"],
    )
    resp_domain = client.post(
        "/auth/saml/acs",
        data={"SAMLResponse": b64_unauth_domain},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp_domain.status_code == 403
    assert "Domain Not Authorized" in resp_domain.text
    assert "attacker@external-domain.org" in resp_domain.text

    # Case B: Allowed domain (@mskcc.org) but missing required SAML group
    b64_wrong_group = make_mock_saml_response(
        email="curator@mskcc.org",
        name="MSK Curator",
        groups=["MSK-Finance-Staff"],
    )
    resp_group = client.post(
        "/auth/saml/acs",
        data={"SAMLResponse": b64_wrong_group},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp_group.status_code == 403
    assert "Access Restricted" in resp_group.text
    assert "MSK-OncoKB-Curators" in resp_group.text


@pytest.mark.asyncio
async def test_jit_provisioning_and_user_store(monkeypatch):
    monkeypatch.setattr(settings, "jit_default_role", "curator")
    monkeypatch.setattr(settings, "saml_admin_groups", "MSK-Admins")

    # 1. First login -> provision new user
    profile1 = await provision_or_update_user(
        email="jit.test@mskcc.org",
        name="JIT Initial Name",
        provider="saml",
        groups=["MSK-Curators"],
    )
    assert profile1.email == "jit.test@mskcc.org"
    assert profile1.name == "JIT Initial Name"
    assert profile1.role == "curator"
    assert profile1.login_count == 1
    assert profile1.first_login_at == profile1.last_login_at

    # 2. Second login -> updates login_count, updates last_login_at, syncs new groups
    profile2 = await provision_or_update_user(
        email="jit.test@mskcc.org",
        name="JIT Updated Name",
        provider="saml",
        groups=["MSK-Admins"],  # Now has admin group!
    )
    assert profile2.email == "jit.test@mskcc.org"
    assert profile2.name == "JIT Updated Name"
    assert profile2.login_count == 2
    assert profile2.role == "admin"
    assert "MSK-Curators" in profile2.groups
    assert "MSK-Admins" in profile2.groups

    # 3. List all profiles includes the user
    all_users = await list_user_profiles()
    emails = [u.email for u in all_users]
    assert "jit.test@mskcc.org" in emails


def test_auth_users_admin_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agcg_dev_mode", False)
    monkeypatch.setattr(settings, "auth_secret_key", "secret-key-admin-test")
    client = TestClient(app)

    # 1. Non-admin user gets 403 Forbidden
    curator_user = AuthenticatedUser(
        email="regular@mskcc.org",
        name="Regular Curator",
        domain="mskcc.org",
        role="curator",
    )
    curator_token = create_session_token(curator_user)
    client.cookies.set(settings.auth_cookie_name, curator_token)
    resp_curator = client.get("/auth/users")
    assert resp_curator.status_code == 403
    assert "Administrator privileges required" in resp_curator.json()["detail"]

    # 2. Admin user gets 200 OK and list of users
    admin_user = AuthenticatedUser(
        email="superadmin@mskcc.org",
        name="Super Admin",
        domain="mskcc.org",
        role="admin",
    )
    admin_token = create_session_token(admin_user)
    client.cookies.set(settings.auth_cookie_name, admin_token)
    resp_admin = client.get("/auth/users")
    assert resp_admin.status_code == 200
    users_list = resp_admin.json()
    assert isinstance(users_list, list)


def test_mskcc_email_works_out_of_the_box(monkeypatch):
    # 1. Verify default configuration has mskcc.org enabled
    assert "mskcc.org" in settings.allowed_domains_list
    assert "openevidence.com" in settings.allowed_domains_list
    assert settings.allowed_emails_list == []
    assert settings.saml_allowed_groups_list == []

    # 2. Standard MSK email passes authorization
    ok, reason = is_email_allowed("oncologist@mskcc.org")
    assert ok is True
    assert reason == "Authorized"

    # 3. Uppercase and leading/trailing whitespace normalized
    ok, _ = is_email_allowed("  JANE.DOE@MSKCC.ORG  ")
    assert ok is True

    # 4. Email tagging / sub-addressing (+tag) supported
    ok, _ = is_email_allowed("curator+oncokb@mskcc.org")
    assert ok is True

    # 5. Non-whitelisted domains blocked out of the box
    ok, reason_gmail = is_email_allowed("user@gmail.com")
    assert ok is False
    assert "@gmail.com" in reason_gmail
    assert "@mskcc.org" in reason_gmail

    ok, reason_other = is_email_allowed("researcher@columbia.edu")
    assert ok is False
    assert "@columbia.edu" in reason_other

    # 6. End-to-end login sets session cookie and authenticates as MSK curator
    monkeypatch.setattr(settings, "dev_login_enabled", True)
    client = TestClient(app)
    resp = client.get(
        "/auth/dev/login?email=oncologist@mskcc.org&name=MSK+Oncologist",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    session_cookie = resp.cookies.get(settings.auth_cookie_name)
    assert session_cookie is not None
    user = decode_session_token(session_cookie)
    assert user is not None
    assert user.email == "oncologist@mskcc.org"
    assert user.domain == "mskcc.org"
    assert user.status == "active"


