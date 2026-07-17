"""Small persistent JSON cache for repeat LLM sub-tasks."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from src.config import settings
from src.pipeline.local_config import app_config_dir

logger = logging.getLogger(__name__)


def stable_cache_key(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def cache_entry_path(namespace: str, key: str) -> Path:
    safe_namespace = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in namespace
    )
    return app_config_dir() / "llm-cache" / safe_namespace / f"{key}.json"


def read_cached_json(namespace: str, key_payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not settings.llm_cache_enabled:
        return None

    path = cache_entry_path(namespace, stable_cache_key(key_payload))
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Ignoring unreadable LLM cache entry at %s: %s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def write_cached_json(namespace: str, key_payload: dict[str, Any], value: dict[str, Any]) -> None:
    if not settings.llm_cache_enabled:
        return

    path = cache_entry_path(namespace, stable_cache_key(key_payload))
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning("Could not write LLM cache entry at %s: %s", path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
