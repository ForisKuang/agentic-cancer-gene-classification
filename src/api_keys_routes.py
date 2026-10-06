"""HTTP endpoints for managing ACGC API keys (/v1/api-keys).

Keys are minted only by browser-session users for themselves; an API-key
caller can list/revoke but never create keys, so a leaked key can't be used
to mint fresh ones. Admins may list (``?all=true``) and revoke any key.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from src.api_keys import (
    ApiKeyRecord,
    api_key_inactive_reason,
    compute_expires_at,
    generate_api_key,
    hash_api_key,
    key_prefix_of,
)
from src.auth import AuthenticatedUser, is_email_allowed, require_auth
from src.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expires_in_days: Optional[int] = Field(default=None, ge=1)


class ApiKeyInfo(BaseModel):
    id: str
    name: str
    key_prefix: str
    owner_email: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    active: bool

    @classmethod
    def from_record(cls, record: ApiKeyRecord) -> "ApiKeyInfo":
        return cls(
            id=record.id,
            name=record.name,
            key_prefix=record.key_prefix,
            owner_email=record.owner_email,
            created_at=record.created_at,
            last_used_at=record.last_used_at,
            revoked_at=record.revoked_at,
            expires_at=record.expires_at,
            active=api_key_inactive_reason(record) is None,
        )


class ApiKeyCreateResponse(ApiKeyInfo):
    # Plaintext secret — returned only in this response, never stored or retrievable.
    key: str


def _store(request: Request):
    store = getattr(request.app.state, "run_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="API key storage is unavailable.")
    return store


def _is_admin(user: AuthenticatedUser) -> bool:
    return user.role == "admin" or settings.agcg_dev_mode


@router.post("", response_model=ApiKeyCreateResponse, status_code=201)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_auth),
) -> ApiKeyCreateResponse:
    if not settings.auth_enabled:
        raise HTTPException(
            status_code=400,
            detail="API keys require AUTH_ENABLED=true; with auth disabled no key is needed.",
        )
    if current_user.auth_method == "api_key":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys can only be created from a signed-in browser session, not with another API key.",
        )
    allowed, reason = is_email_allowed(current_user.email)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)
    if payload.expires_in_days is not None and payload.expires_in_days > settings.api_key_max_expires_in_days:
        raise HTTPException(
            status_code=422,
            detail=f"expires_in_days must be at most {settings.api_key_max_expires_in_days}.",
        )

    plaintext = generate_api_key()
    now = datetime.now(timezone.utc)
    record = ApiKeyRecord(
        id=str(uuid.uuid4()),
        key_prefix=key_prefix_of(plaintext),
        key_hash=hash_api_key(plaintext),
        owner_email=current_user.email.strip().lower(),
        name=payload.name.strip(),
        created_at=now,
        expires_at=compute_expires_at(payload.expires_in_days, now),
    )
    await _store(request).create_api_key(record)
    logger.info("API key %s (%s) created for %s", record.id, record.key_prefix, record.owner_email)
    return ApiKeyCreateResponse(**ApiKeyInfo.from_record(record).model_dump(), key=plaintext)


@router.get("", response_model=List[ApiKeyInfo])
async def list_api_keys(
    request: Request,
    all_keys: bool = Query(default=False, alias="all", description="Admins only: list every user's keys."),
    current_user: AuthenticatedUser = Depends(require_auth),
) -> List[ApiKeyInfo]:
    if all_keys and not _is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required for this action.",
        )
    owner = None if all_keys else current_user.email.strip().lower()
    records = await _store(request).list_api_keys(owner_email=owner)
    return [ApiKeyInfo.from_record(r) for r in records]


@router.delete("/{key_id}", response_model=ApiKeyInfo)
async def revoke_api_key(
    key_id: str,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_auth),
) -> ApiKeyInfo:
    store = _store(request)
    record = await store.get_api_key(key_id)
    # 404 (not 403) for other users' keys so ids can't be probed.
    if record is None or (
        record.owner_email.strip().lower() != current_user.email.strip().lower() and not _is_admin(current_user)
    ):
        raise HTTPException(status_code=404, detail="API key not found.")
    if record.revoked_at is None:
        now = datetime.now(timezone.utc)
        await store.revoke_api_key(record.id, now)
        record = record.model_copy(update={"revoked_at": now})
        logger.info("API key %s revoked by %s", record.id, current_user.email)
    return ApiKeyInfo.from_record(record)
