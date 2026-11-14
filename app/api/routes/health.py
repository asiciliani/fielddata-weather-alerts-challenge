"""Liveness and readiness probes (outside the ``/api/v1`` prefix)."""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import SessionDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Succeeds while the process is running."""
    return {"status": "ok"}


@router.get("/ready", response_model=None)
async def ready(session: SessionDep) -> dict[str, str] | JSONResponse:
    """Succeeds when the database answers a trivial query."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        # Not ready is an expected state of a probe, so it answers 503 instead of raising.
        logger.warning("readiness_check_failed", extra={"error_type": type(exc).__name__})
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    return {"status": "ok"}
