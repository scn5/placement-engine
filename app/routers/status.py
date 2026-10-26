"""
app/routers/status.py
GET /status — the ingestion worker's liveness, as last written to worker_status
(authenticated; /health stays the public liveness probe of the API itself).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import models, schemas
from app.config import settings
from app.database import get_db

router = APIRouter(tags=["Meta"])

# The worker counts as down once this many heartbeats have been missed.
_MISSED_HEARTBEATS = 3


@router.get("/status", response_model=schemas.WorkerStatusResponse, summary="Ingestion worker status")
async def get_status(db: AsyncSession = Depends(get_db)) -> schemas.WorkerStatusResponse:
    """`ok` when the worker's heartbeat is fresh and Telegram is connected;
    `degraded` otherwise; `unknown` if the worker has never written a heartbeat."""
    now = datetime.now(timezone.utc)
    row = await db.get(models.WorkerStatus, 1)
    if row is None:
        return schemas.WorkerStatusResponse(status="unknown", checked_at=now)

    age = (now - row.heartbeat_at).total_seconds() if row.heartbeat_at else None
    fresh = age is not None and age <= settings.HEARTBEAT_SECONDS * _MISSED_HEARTBEATS
    return schemas.WorkerStatusResponse(
        status="ok" if fresh and row.telegram_state == "CONNECTED" else "degraded",
        telegram_state=row.telegram_state,
        heartbeat_at=row.heartbeat_at,
        heartbeat_age_seconds=round(age) if age is not None else None,
        last_update_at=row.last_update_at,
        last_llm_ok_at=row.last_llm_ok_at,
        catchup_at=row.catchup_at,
        last_error=row.last_error,
        checked_at=now,
    )
