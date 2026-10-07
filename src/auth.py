"""
SSO & Google OAuth authentication, Enterprise SAML 2.0 claims processing,
JIT (Just-In-Time) provisioning, and domain-based access control.
Restricts access to @mskcc.org and @openevidence.com accounts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
import uuid
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from src.api_keys import (
    api_key_inactive_reason,
    api_key_matches,
    bearer_token,
    check_api_key_rate_limit,
    hash_api_key,
    looks_like_api_key,
    parse_api_key,
    should_touch_last_used,
)
from src.config import settings

logger = logging.getLogger(__name__)

# Fallback in-memory secret if AUTH_SECRET_KEY is omitted in non-production environments
_ephemeral_secret: Optional[str] = None
_redis_client = None

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

SAML_NS = {
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
}


class AuthenticatedUser(BaseModel):
    email: str
    name: str
    picture: Optional[str] = None
    domain: str
    role: str = "curator"
    status: str = "active"
    provider: str = "google"
    groups: List[str] = Field(default_factory=list)
    # "session" (cookie / session bearer token) or "api_key" (Authorization: Bearer acgc_...)
    auth_method: str = "session"
    api_key_id: Optional[str] = None


class UserProfile(BaseModel):
    email: str
    name: str
    picture: Optional[str] = None
    domain: str
    role: str = "curator"
    status: str = "active"
    provider: str = "google"
    groups: List[str] = Field(default_factory=list)
    claims: Dict[str, Any] = Field(default_factory=dict)
    first_login_at: str
    last_login_at: str
    login_count: int = 1
    annotation_count: int = 0
    last_annotation_at: Optional[str] = None


class AuthUserResponse(BaseModel):
    email: str
    name: str
    picture: Optional[str] = None
    domain: str
    role: str = "curator"
    status: str = "active"
    provider: str = "google"
    groups: List[str] = Field(default_factory=list)
    first_login_at: Optional[str] = None
    last_login_at: Optional[str] = None
    login_count: int = 1
    annotation_count: int = 0
    last_annotation_at: Optional[str] = None


class AuthMeResponse(BaseModel):
    auth_enabled: bool
    authenticated: bool
    user: Optional[AuthUserResponse] = None
    allowed_domains: List[str] = Field(default_factory=list)
    saml_enabled: bool = False
    jit_provisioning_enabled: bool = True
    dev_login_enabled: bool = False
    keycloak_enabled: bool = False


class SAMLAssertionData(BaseModel):
    name_id: str
    attributes: Dict[str, List[str]] = Field(default_factory=dict)
    groups: List[str] = Field(default_factory=list)
    display_name: str = ""
    issuer: str = ""
    session_index: Optional[str] = None


# ---------------------------------------------------------------------------
# JIT User Store (Redis + In-Memory Fallback)
# ---------------------------------------------------------------------------

_user_store: Dict[str, UserProfile] = {}
_user_store_lock = asyncio.Lock()


async def _get_auth_redis_client():
    global _redis_client
    redis_url = settings.redis_url.strip()
    if not redis_url:
        return None
    if _redis_client is None:
        try:
            import redis.asyncio as redis
            _redis_client = redis.from_url(redis_url, decode_responses=True)
        except (ImportError, ConnectionError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            logger.debug("Failed to initialize Redis auth client: %s", exc)
            return None
    return _redis_client


async def get_user_profile(email: str) -> Optional[UserProfile]:
    clean_email = email.strip().lower()
    redis_client = await _get_auth_redis_client()
    if redis_client is not None:
        try:
            payload = await redis_client.get(f"agcg:user:{clean_email}")
            if payload:
                return UserProfile.model_validate_json(payload)
        except Exception as exc:
            logger.debug("Redis error reading user %s: %s", clean_email, exc)

    async with _user_store_lock:
        return _user_store.get(clean_email)


async def save_user_profile(profile: UserProfile) -> None:
    clean_email = profile.email.strip().lower()
    redis_client = await _get_auth_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(f"agcg:user:{clean_email}", profile.model_dump_json())
            await redis_client.sadd("agcg:users", clean_email)
        except Exception as exc:
            logger.debug("Redis error saving user %s: %s", clean_email, exc)

    async with _user_store_lock:
        _user_store[clean_email] = profile


async def list_user_profiles() -> List[UserProfile]:
    redis_client = await _get_auth_redis_client()
    if redis_client is not None:
        try:
            emails = await redis_client.smembers("agcg:users")
            users = []
            for email in emails:
                u = await get_user_profile(email)
                if u:
                    users.append(u)
            if users:
                users.sort(key=lambda x: x.last_login_at, reverse=True)
                return users
        except Exception as exc:
            logger.debug("Redis error listing users: %s", exc)

    async with _user_store_lock:
        users = list(_user_store.values())
        users.sort(key=lambda x: x.last_login_at, reverse=True)
        return users


async def provision_or_update_user(
    email: str,
    name: str,
    picture: Optional[str] = None,
    domain: Optional[str] = None,
    provider: str = "google",
    groups: Optional[List[str]] = None,
    claims: Optional[Dict[str, Any]] = None,
    role: Optional[str] = None,
) -> UserProfile:
    """
    Just-In-Time (JIT) provisioning: creates a new user profile on first login,
    or updates login timestamp, groups, and claims for returning users.
    """
    clean_email = email.strip().lower()
    extracted_domain = domain or clean_email.split("@")[-1]
    now_iso = datetime.now(timezone.utc).isoformat()
    user_groups = groups or []
    user_claims = claims or {}

    # Derive role from SAML/OIDC groups or defaults
    effective_role = role or settings.jit_default_role
    if any(g in settings.saml_admin_groups_list for g in user_groups):
        effective_role = "admin"
    if any(g in settings.keycloak_admin_roles_list for g in user_groups):
        effective_role = "admin"

    existing = await get_user_profile(clean_email)
    if existing is None:
        status_val = "pending" if settings.jit_require_admin_approval else "active"
        logger.info(
            "JIT provisioning new user: %s (domain=%s, role=%s, provider=%s, status=%s, groups=%s)",
            clean_email, extracted_domain, effective_role, provider, status_val, user_groups,
        )
        profile = UserProfile(
            email=clean_email,
            name=name or clean_email,
            picture=picture,
            domain=extracted_domain,
            role=effective_role,
            status=status_val,
            provider=provider,
            groups=user_groups,
            claims=user_claims,
            first_login_at=now_iso,
            last_login_at=now_iso,
            login_count=1,
        )
    else:
        logger.info("JIT sync for returning user %s (login_count=%d)", clean_email, existing.login_count + 1)
        resolved_role = "admin" if (effective_role == "admin" or existing.role == "admin") else existing.role
        merged_groups = list(dict.fromkeys(existing.groups + user_groups))
        profile = existing.model_copy(
            update={
                "name": name or existing.name,
                "picture": picture or existing.picture,
                "last_login_at": now_iso,
                "login_count": existing.login_count + 1,
                "groups": merged_groups,
                "claims": {**existing.claims, **user_claims},
                "role": resolved_role,
            }
        )

    await save_user_profile(profile)
    return profile


async def record_user_annotation_activity(email: str, count: int = 1) -> None:
    """Updates user profile usage metrics in the user store upon annotation completion."""
    clean_email = email.strip().lower()
    profile = await get_user_profile(clean_email)
    if profile:
        now_iso = datetime.now(timezone.utc).isoformat()
        updated = profile.model_copy(
            update={
                "annotation_count": profile.annotation_count + max(1, count),
                "last_annotation_at": now_iso,
            }
        )
        await save_user_profile(updated)


# ---------------------------------------------------------------------------
# Signing & State
# ---------------------------------------------------------------------------

def get_auth_secret_key() -> str:
    global _ephemeral_secret
    configured = settings.auth_secret_key.strip()
    if configured:
        return configured
    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_hex(32)
        logger.warning(
            "AUTH_SECRET_KEY not set. Using generated ephemeral secret. "
            "Sessions will expire upon application restart. Configure AUTH_SECRET_KEY in production."
        )
    return _ephemeral_secret


def is_email_allowed(email: str) -> Tuple[bool, str]:
    """
    Validates whether an email is permitted access based on domain whitelist
    (@mskcc.org and @openevidence.com) and optional explicit user allowlist.
    """
    clean_email = email.strip().lower()
    if "@" not in clean_email:
        return False, "Invalid email address format."

    domain = clean_email.split("@")[-1]
    allowed_domains = settings.allowed_domains_list

    if allowed_domains and domain not in allowed_domains:
        allowed_desc = ", ".join(f"@{d}" for d in allowed_domains)
        return (
            False,
            f"Domain '@{domain}' is not authorized. Access is strictly limited to {allowed_desc} accounts.",
        )

    allowed_emails = settings.allowed_emails_list
    if allowed_emails and clean_email not in allowed_emails:
        return (
            False,
            f"User '{clean_email}' is not on the authorized user list. Please request access from an administrator.",
        )

    return True, "Authorized"


def is_saml_group_allowed(groups: List[str]) -> Tuple[bool, str]:
    """
    Checks if SAML/Enterprise claims satisfy the required group constraints.
    """
    allowed_groups = settings.saml_allowed_groups_list
    if not allowed_groups:
        return True, "No group restriction configured"

    matched = set(groups) & set(allowed_groups)
    if not matched:
        return (
            False,
            f"Access requires membership in one of: {', '.join(allowed_groups)}. Your account groups: {', '.join(groups) or 'none'}.",
        )
    return True, f"Authorized via group(s): {', '.join(matched)}"


def create_session_token(user: AuthenticatedUser, ttl_seconds: Optional[int] = None) -> str:
    """
    Creates a cryptographically signed HMAC-SHA256 session token.
    Token format: base64url(payload).base64url(signature)
    """
    ttl = ttl_seconds if ttl_seconds is not None else settings.auth_session_ttl_seconds
    now = int(time.time())
    payload = {
        "email": user.email.strip().lower(),
        "name": user.name,
        "picture": user.picture,
        "domain": user.domain.strip().lower(),
        "role": user.role,
        "status": user.status,
        "provider": user.provider,
        "groups": user.groups,
        "iat": now,
        "exp": now + ttl,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")

    secret = get_auth_secret_key().encode("utf-8")
    sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    return f"{payload_b64}.{sig_b64}"


def decode_session_token(token: str) -> Optional[AuthenticatedUser]:
    """
    Verifies signature, expiration, and domain authorization of a session token.
    Returns AuthenticatedUser if valid, None otherwise.
    """
    if not token or "." not in token:
        return None

    try:
        payload_b64, sig_b64 = token.split(".", 1)
        secret = get_auth_secret_key().encode("utf-8")

        expected_sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")

        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            logger.warning("Session token HMAC verification failed.")
            return None

        padding = "=" * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        payload = json.loads(payload_json)

        now = int(time.time())
        if payload.get("exp", 0) < now:
            logger.info("Session token expired for %s", payload.get("email"))
            return None

        email = payload.get("email", "").strip().lower()
        allowed, reason = is_email_allowed(email)
        if not allowed:
            logger.warning("Session token rejected for %s: %s", email, reason)
            return None

        status_val = payload.get("status", "active")
        if status_val != "active":
            logger.warning("Session rejected for %s: account status is %s", email, status_val)
            return None

        return AuthenticatedUser(
            email=email,
            name=payload.get("name") or email,
            picture=payload.get("picture"),
            domain=payload.get("domain") or email.split("@")[-1],
            role=payload.get("role", settings.jit_default_role),
            status=status_val,
            provider=payload.get("provider", "google"),
            groups=payload.get("groups", []),
        )
    except Exception:
        logger.exception("Failed to decode session token")
        return None


def validate_redirect_to(value: Any) -> str:
    """Accept only local absolute paths, never browser-normalized external URLs."""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return "/"
    return value


def create_oauth_state(redirect_to: str = "/") -> str:
    """
    Generates a tamper-proof state token for CSRF protection during OAuth / SAML flow.
    """
    now = int(time.time())
    payload = {
        "nonce": secrets.token_hex(16),
        "redirect_to": validate_redirect_to(redirect_to),
        "iat": now,
        "exp": now + 900,  # 15 minutes
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")

    secret = get_auth_secret_key().encode("utf-8")
    sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    return f"{payload_b64}.{sig_b64}"


def verify_oauth_state(state: str) -> Optional[Dict[str, Any]]:
    """
    Verifies that the state parameter is authentic and unexpired.
    """
    if not state or "." not in state:
        return None

    try:
        payload_b64, sig_b64 = state.split(".", 1)
        secret = get_auth_secret_key().encode("utf-8")

        expected_sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")

        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            return None

        padding = "=" * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        payload = json.loads(payload_json)

        if payload.get("exp", 0) < int(time.time()):
            return None

        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Google OAuth 2.0 / OIDC Helpers
# ---------------------------------------------------------------------------

def get_google_auth_url(redirect_uri: str, state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_google_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    if not settings.google_client_secret or not settings.google_client_secret.strip():
        logger.error("Google OAuth token exchange failed: GOOGLE_CLIENT_SECRET is missing or empty in environment.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Google OAuth misconfigured: GOOGLE_CLIENT_SECRET is not set in the server environment. "
                "Ensure Kubernetes Secret 'acgc' has GOOGLE_CLIENT_SECRET and the pod has been restarted."
            ),
        )

    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(GOOGLE_TOKEN_ENDPOINT, data=data)
        if resp.status_code != 200:
            logger.error("Failed Google token exchange (%s): %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Google authentication failed during token exchange: {resp.text}",
            )
        return resp.json()


async def get_google_user_info(access_token: str) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(GOOGLE_USERINFO_ENDPOINT, headers=headers)
        if resp.status_code != 200:
            logger.error("Failed to fetch Google user info (%s): %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to retrieve user profile from Google.",
            )
        return resp.json()


# ---------------------------------------------------------------------------
# Enterprise SAML 2.0 & Claims Helpers
# ---------------------------------------------------------------------------

def build_saml_authn_request(acs_url: str, relay_state: str = "/") -> Tuple[str, str]:
    """
    Builds a standard SAML 2.0 AuthnRequest, deflates and base64 encodes it.
    Returns (request_id, redirect_url).
    """
    request_id = f"id_{uuid.uuid4().hex}"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    issuer = settings.saml_sp_entity_id.strip() or acs_url
    destination = settings.saml_idp_sso_url.strip()

    xml = (
        f'<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="{request_id}" Version="2.0" IssueInstant="{now_iso}" '
        f'Destination="{html.escape(destination)}" '
        f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'AssertionConsumerServiceURL="{html.escape(acs_url)}">'
        f'<saml:Issuer>{html.escape(issuer)}</saml:Issuer>'
        f'<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" AllowCreate="true"/>'
        f'</samlp:AuthnRequest>'
    )

    deflated = zlib.compress(xml.encode("utf-8"))[2:-4]
    b64_request = base64.b64encode(deflated).decode("utf-8")
    params = {"SAMLRequest": b64_request, "RelayState": validate_redirect_to(relay_state)}
    redirect_url = f"{destination}?{urlencode(params)}"
    return request_id, redirect_url


def parse_saml_response(saml_response_b64: str) -> SAMLAssertionData:
    """
    Decodes and parses a SAML 2.0 Response XML, extracting NameID, attributes, and groups.
    """
    try:
        xml_bytes = base64.b64decode(saml_response_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Base64 in SAMLResponse") from exc

    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Malformed XML in SAMLResponse") from exc

    # Validate status
    status_elem = root.find(".//samlp:StatusCode", SAML_NS)
    if status_elem is not None:
        status_val = status_elem.attrib.get("Value", "")
        if "Success" not in status_val:
            raise HTTPException(status_code=401, detail=f"SAML Authentication failed at IdP: {status_val}")

    # Extract issuer
    issuer_elem = root.find(".//saml:Issuer", SAML_NS)
    issuer = issuer_elem.text.strip() if issuer_elem is not None and issuer_elem.text else ""

    # Extract NameID / Subject
    name_id_elem = root.find(".//saml:Subject/saml:NameID", SAML_NS)
    name_id = name_id_elem.text.strip() if name_id_elem is not None and name_id_elem.text else ""

    # Extract all AttributeStatement attributes
    attributes: Dict[str, List[str]] = {}
    attr_elems = root.findall(".//saml:AttributeStatement/saml:Attribute", SAML_NS)
    for attr in attr_elems:
        attr_name = attr.attrib.get("Name", "")
        values = []
        for val in attr.findall("saml:AttributeValue", SAML_NS):
            if val.text:
                values.append(val.text.strip())
        if attr_name and values:
            attributes[attr_name] = values

    # Resolve email from claims or NameID
    email = ""
    for email_key in [
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "http://schemas.microsoft.com/identity/claims/emailaddress",
        "email",
        "mail",
        "User.Email",
    ]:
        if email_key in attributes:
            email = attributes[email_key][0]
            break
    if not email:
        email = name_id

    # Resolve display name
    display_name = ""
    for name_key in [
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
        "http://schemas.microsoft.com/identity/claims/displayname",
        "displayName",
        "name",
        "cn",
    ]:
        if name_key in attributes:
            display_name = attributes[name_key][0]
            break
    if not display_name:
        display_name = email

    # Resolve groups / roles
    groups = []
    for group_key in [
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
        "http://schemas.xmlsoap.org/claims/Group",
        "groups",
        "group",
        "roles",
        "role",
        "memberOf",
    ]:
        if group_key in attributes:
            groups.extend(attributes[group_key])

    authn_statement = root.find(".//saml:AuthnStatement", SAML_NS)
    session_index = authn_statement.attrib.get("SessionIndex") if authn_statement is not None else None

    return SAMLAssertionData(
        name_id=email,
        attributes=attributes,
        groups=list(dict.fromkeys(groups)),
        display_name=display_name,
        issuer=issuer,
        session_index=session_index,
    )


def generate_sp_metadata_xml(acs_url: str, sp_entity_id: Optional[str] = None) -> str:
    """
    Generates standard SAML 2.0 SP Metadata XML for MSK IT and IdP configuration.
    """
    entity_id = sp_entity_id or settings.saml_sp_entity_id.strip() or acs_url
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{html.escape(entity_id)}">
    <md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
        <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>
        <md:NameIDFormat>urn:oasis:names:tc:SAML:2.0:nameid-format:persistent</md:NameIDFormat>
        <md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" Location="{html.escape(acs_url)}" index="1" isDefault="true"/>
    </md:SPSSODescriptor>
</md:EntityDescriptor>"""


# ---------------------------------------------------------------------------
# Cookie & Dependency Helpers
# ---------------------------------------------------------------------------

def set_session_cookie(response: Response, token: str, is_secure: bool = False) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.auth_cookie_name, path="/")


def _uses_api_key(request: Request) -> bool:
    """True when the request authenticates with an ACGC API key rather than a session.

    A session cookie takes precedence, matching get_current_user's lookup order.
    """
    if request.cookies.get(settings.auth_cookie_name):
        return False
    return looks_like_api_key(bearer_token(request.headers.get("Authorization")))


# request.state attribute caching the API-key resolution for the current request,
# so the observability middleware and require_auth share a single DB lookup.
_API_KEY_STATE_ATTR = "acgc_api_key_auth"
_API_KEY_RATE_CHECKED_ATTR = "acgc_api_key_rate_checked"


def get_current_user(request: Request) -> Optional[AuthenticatedUser]:
    if _uses_api_key(request):
        # API keys need an async DB lookup; this sync helper only sees a key
        # that resolve_api_key_user() already verified for this request.
        cached = getattr(request.state, _API_KEY_STATE_ATTR, None)
        return cached[0] if cached else None

    token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        token = bearer_token(request.headers.get("Authorization"))

    if not token:
        return None

    return decode_session_token(token)


async def _verify_api_key(request: Request, plaintext: str) -> Tuple[Optional[AuthenticatedUser], Optional[str]]:
    store = getattr(request.app.state, "run_store", None)
    if store is None:
        return None, "unavailable"
    try:
        # Every well-formed key takes the same path: one indexed lookup by its
        # full SHA-256, so the lookup reveals nothing about partial matches.
        record = await store.get_api_key_by_hash(hash_api_key(plaintext))
    except Exception:
        logger.exception("API key lookup failed")
        return None, "unavailable"

    if record is None or not api_key_matches(plaintext, record):
        logger.warning("API key rejected: unknown key")
        return None, "invalid"

    inactive = api_key_inactive_reason(record)
    if inactive:
        logger.warning("API key %s rejected: %s", record.id, inactive)
        return None, "invalid"

    # Re-check the owner against the live allowlist so offboarded users and
    # domains lose access even though their key was never revoked.
    owner_email = record.owner_email.strip().lower()
    allowed, reason = is_email_allowed(owner_email)
    if not allowed:
        logger.warning("API key %s rejected for %s: %s", record.id, owner_email, reason)
        return None, "invalid"

    now = datetime.now(timezone.utc)
    if should_touch_last_used(record.last_used_at, settings.api_key_last_used_update_seconds, now):
        try:
            await store.touch_api_key(record.id, now)
        except Exception as exc:
            logger.warning("Failed to update last_used_at for API key %s: %s", record.id, exc)

    profile = await get_user_profile(owner_email)
    return (
        AuthenticatedUser(
            email=owner_email,
            name=(profile.name if profile else None) or owner_email,
            picture=profile.picture if profile else None,
            domain=owner_email.split("@")[-1],
            role=profile.role if profile else settings.jit_default_role,
            status=profile.status if profile else "active",
            provider="api_key",
            groups=list(profile.groups) if profile else [],
            auth_method="api_key",
            api_key_id=record.id,
        ),
        None,
    )


async def resolve_api_key_user(request: Request) -> Optional[AuthenticatedUser]:
    """Verify an `Authorization: Bearer acgc_...` key (once per request, cached).

    Returns the key owner's identity, or None when auth is disabled, the request
    doesn't carry an API key, or the key is unknown/revoked/expired/disallowed.
    """
    if not settings.auth_enabled or not _uses_api_key(request):
        return None
    cached = getattr(request.state, _API_KEY_STATE_ATTR, None)
    if cached is None:
        plaintext = parse_api_key(bearer_token(request.headers.get("Authorization")))
        if plaintext is None:
            cached = (None, "invalid")
        else:
            cached = await _verify_api_key(request, plaintext)
        setattr(request.state, _API_KEY_STATE_ATTR, cached)
    return cached[0]


async def get_request_user(request: Request) -> Optional[AuthenticatedUser]:
    """Async counterpart of get_current_user that also resolves API keys."""
    await resolve_api_key_user(request)
    return get_current_user(request)


async def _enforce_api_key_rate_limit(request: Request, user: AuthenticatedUser) -> None:
    if not user.api_key_id or getattr(request.state, _API_KEY_RATE_CHECKED_ATTR, False):
        return
    setattr(request.state, _API_KEY_RATE_CHECKED_ATTR, True)
    allowed, retry_after = await check_api_key_rate_limit(
        user.api_key_id,
        settings.api_key_rate_limit_per_minute,
        redis_client=await _get_auth_redis_client(),
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="API key rate limit exceeded. Please slow down and retry later.",
            headers={"Retry-After": str(retry_after)},
        )


async def require_auth(request: Request) -> AuthenticatedUser:
    if not settings.auth_enabled:
        return AuthenticatedUser(
            email="local@internal",
            name="Local Developer",
            domain="internal",
            role="admin",
            provider="local",
            groups=["Admins"],
        )

    uses_api_key = _uses_api_key(request)
    if uses_api_key:
        user = await resolve_api_key_user(request)
        if not user and getattr(request.state, _API_KEY_STATE_ATTR, (None, None))[1] == "unavailable":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API key verification is temporarily unavailable.",
            )
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid, revoked, or expired API key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    else:
        user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in with your MSK (@mskcc.org) or OpenEvidence (@openevidence.com) account.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is pending administrator approval. Please contact your AGCG administrator.",
        )

    if uses_api_key:
        await _enforce_api_key_rate_limit(request, user)
        logger.debug("Request authenticated with API key %s for %s", user.api_key_id, user.email)

    return user


async def require_admin(request: Request) -> AuthenticatedUser:
    user = await require_auth(request)
    if user.role != "admin" and not settings.agcg_dev_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required for this action.",
        )
    return user


def render_access_denied_html(email: str, reason: str, login_url: str = "/auth/login") -> str:
    escaped_email = html.escape(email)
    escaped_reason = html.escape(reason)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Access Restricted — Agentic Cancer Gene Classification</title>
  <style>
    :root {{
      --bg: #f5f7f8;
      --card-bg: #ffffff;
      --line: #cdd6dc;
      --text: #152026;
      --muted: #60717c;
      --accent: #0f766e;
      --danger: #b42318;
      --danger-bg: #fef3f2;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: var(--bg);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
      color: var(--text);
      padding: 24px;
      box-sizing: border-box;
    }}
    .auth-card {{
      background: var(--card-bg);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 36px 32px;
      max-width: 480px;
      width: 100%;
      box-shadow: 0 18px 40px rgba(22, 34, 42, 0.08);
      text-align: center;
    }}
    .badge {{
      display: inline-block;
      background: var(--danger-bg);
      color: var(--danger);
      font-weight: 600;
      font-size: 12px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      padding: 4px 10px;
      border-radius: 999px;
      margin-bottom: 16px;
    }}
    h1 {{
      font-size: 22px;
      font-weight: 600;
      margin: 0 0 12px;
    }}
    .email-box {{
      background: #f8fafc;
      border: 1px dashed var(--line);
      padding: 10px;
      border-radius: 6px;
      font-family: monospace;
      font-size: 14px;
      margin: 16px 0;
      word-break: break-all;
    }}
    p {{
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
      margin: 0 0 18px;
    }}
    .allowed-tags {{
      display: flex;
      gap: 8px;
      justify-content: center;
      margin: 12px 0 24px;
    }}
    .domain-tag {{
      background: #eef2f4;
      color: var(--text);
      font-size: 13px;
      font-weight: 500;
      padding: 4px 8px;
      border-radius: 4px;
    }}
    .btn {{
      display: inline-block;
      background: var(--accent);
      color: white;
      text-decoration: none;
      font-size: 14px;
      font-weight: 500;
      padding: 10px 20px;
      border-radius: 6px;
      transition: background 0.15s ease;
    }}
    .btn:hover {{
      background: #0b5f59;
    }}
    .support-note {{
      font-size: 12px;
      color: var(--muted);
      margin-top: 24px;
    }}
  </style>
</head>
<body>
  <div class="auth-card">
    <span class="badge">Access Restricted</span>
    <h1>Domain Not Authorized</h1>
    <p>You authenticated with:</p>
    <div class="email-box">{escaped_email}</div>
    <p>{escaped_reason}</p>
    <div class="allowed-tags">
      <span class="domain-tag">@mskcc.org</span>
      <span class="domain-tag">@openevidence.com</span>
    </div>
    <a href="{login_url}" class="btn">Sign In with an Authorized Account</a>
    <div class="support-note">
      If you are a member of MSK or OpenEvidence and believe this is an error, please contact your systems administrator.
    </div>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Keycloak OIDC & PingID SSO Helpers (keycloak.oncokb.org)
# ---------------------------------------------------------------------------


def get_keycloak_realm_base_url() -> str:
    base = settings.keycloak_url.rstrip("/")
    realm = settings.keycloak_realm.strip()
    return f"{base}/realms/{realm}"


def get_keycloak_auth_url(redirect_uri: str, state: str, idp_hint: Optional[str] = None) -> str:
    """Constructs Keycloak OpenID Connect authorization URL.

    If idp_hint is provided (or defaults to keycloak_ping_idp_alias, e.g. 'msk-ping'),
    appends kc_idp_hint to redirect directly to MSK PingFederate without presenting
    an intermediate Keycloak login screen.
    """
    realm_url = get_keycloak_realm_base_url()
    auth_endpoint = f"{realm_url}/protocol/openid-connect/auth"
    params = {
        "client_id": settings.keycloak_client_id,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    hint = idp_hint if idp_hint is not None else settings.keycloak_ping_idp_alias
    if hint and hint.strip() and hint.strip().lower() != "none":
        params["kc_idp_hint"] = hint.strip()

    return f"{auth_endpoint}?{urlencode(params)}"


async def exchange_keycloak_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    """Exchanges authorization code for OIDC tokens with Keycloak token endpoint."""
    if not settings.keycloak_client_secret or not settings.keycloak_client_secret.strip():
        logger.error("Keycloak token exchange failed: KEYCLOAK_CLIENT_SECRET is missing or empty in environment.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Keycloak OAuth misconfigured: KEYCLOAK_CLIENT_SECRET is not set in the server environment. "
                "Ensure Kubernetes Secret has KEYCLOAK_CLIENT_SECRET and the pod has been restarted."
            ),
        )

    realm_url = get_keycloak_realm_base_url()
    token_endpoint = f"{realm_url}/protocol/openid-connect/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.keycloak_client_id,
        "client_secret": settings.keycloak_client_secret,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(token_endpoint, data=data)
        if resp.status_code != 200:
            logger.error("Failed Keycloak token exchange (%s): %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Keycloak authentication failed during token exchange: {resp.text}",
            )
        return resp.json()


async def get_keycloak_user_info(access_token: str) -> Dict[str, Any]:
    """Fetches user claims from Keycloak userinfo endpoint."""
    realm_url = get_keycloak_realm_base_url()
    userinfo_endpoint = f"{realm_url}/protocol/openid-connect/userinfo"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(userinfo_endpoint, headers=headers)
        if resp.status_code != 200:
            logger.error("Failed to fetch Keycloak user info (%s): %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to retrieve user profile from Keycloak.",
            )
        return resp.json()


def parse_keycloak_user(token_data: Dict[str, Any], userinfo: Dict[str, Any]) -> AuthenticatedUser:
    """Parses user claims from Keycloak token data and userinfo response."""
    email = userinfo.get("email") or ""
    if not email and "id_token" in token_data:
        try:
            parts = token_data["id_token"].split(".")
            if len(parts) >= 2:
                payload_json = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8")
                id_claims = json.loads(payload_json)
                email = id_claims.get("email", "")
        except Exception:
            pass

    clean_email = email.strip().lower()
    if not clean_email or "@" not in clean_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Keycloak user profile does not contain a valid email address.",
        )

    # Name extraction
    name = userinfo.get("name")
    if not name:
        given = userinfo.get("given_name", "").strip()
        family = userinfo.get("family_name", "").strip()
        if given or family:
            name = f"{given} {family}".strip()
        else:
            name = userinfo.get("preferred_username") or clean_email.split("@")[0]

    domain = clean_email.split("@")[-1]

    # Roles and groups extraction
    groups: List[str] = []
    if "groups" in userinfo and isinstance(userinfo["groups"], list):
        groups.extend(userinfo["groups"])
    if "roles" in userinfo and isinstance(userinfo["roles"], list):
        groups.extend(userinfo["roles"])

    for tok_key in ("access_token", "id_token"):
        tok = token_data.get(tok_key)
        if tok and isinstance(tok, str) and "." in tok:
            try:
                parts = tok.split(".")
                if len(parts) >= 2:
                    p = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8"))
                    realm_access = p.get("realm_access", {})
                    if isinstance(realm_access, dict):
                        roles = realm_access.get("roles", [])
                        if isinstance(roles, list):
                            groups.extend(roles)
                    resource_access = p.get("resource_access", {})
                    if isinstance(resource_access, dict):
                        client_access = resource_access.get(settings.keycloak_client_id, {})
                        if isinstance(client_access, dict):
                            roles = client_access.get("roles", [])
                            if isinstance(roles, list):
                                groups.extend(roles)
                    if "groups" in p and isinstance(p["groups"], list):
                        groups.extend(p["groups"])
            except Exception:
                pass

    unique_groups = sorted(list(set(groups)))

    # Determine user role
    role = settings.jit_default_role
    admin_roles = set(settings.keycloak_admin_roles_list)
    if admin_roles and any(r in admin_roles for r in unique_groups):
        role = "admin"
    elif settings.saml_admin_groups_list and any(g in set(settings.saml_admin_groups_list) for g in unique_groups):
        role = "admin"

    return AuthenticatedUser(
        email=clean_email,
        name=name,
        picture=userinfo.get("picture"),
        domain=domain,
        role=role,
        status="active",
        provider="keycloak",
        groups=unique_groups,
    )

