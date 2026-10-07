"""Strict persistence for API responses that include a link to a saved run."""

import logging

from fastapi import HTTPException, Request

from src.models.schema import AnnotationResult

logger = logging.getLogger(__name__)

GENERIC_FAILURE = "Gene query failed. Please retry; contact the ACGC team if this persists."


async def save_run_or_raise(http_request: Request, request_payload: dict, result: AnnotationResult) -> None:
    """Keep run links out of responses when their run cannot be persisted."""
    try:
        await http_request.app.state.run_store.save_run(
            result.run_id, result.timestamp, request_payload, result.model_dump()
        )
    except Exception as exc:
        logger.exception("Failed to save linked run %s", result.run_id)
        raise HTTPException(status_code=500, detail=GENERIC_FAILURE) from exc
