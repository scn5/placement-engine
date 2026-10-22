"""
app/main.py
FastAPI application entry-point — API only.

Telegram ingestion runs in its own process (`python -m app.worker`), so the API
can be restarted, or run with several workers, without touching the Telegram
session.

Lifespan sequence:
  1. Verify the database is at the Alembic head revision (the schema itself is
     created and migrated only by `alembic upgrade head`).
  2. On shutdown: dispose the database engine.
"""
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import assert_schema_current, engine
from app.routers import companies, events, triage
from app.routers import settings as settings_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # ── 1. Database schema check (Alembic owns the schema) ─────────────────
    await assert_schema_current()
    logger.info("Database schema is at the Alembic head revision.")

    # ── Hand control back to FastAPI ──────────────────────────────────────
    yield

    # ── 2. Shutdown ───────────────────────────────────────────────────────
    await engine.dispose()
    logger.info("Database engine disposed.")


app = FastAPI(
    title="Automated Placement Tracking Engine",
    version="0.4.0",
    description=(
        "Single source of truth for NITK placement tracking — "
        "no Sheets, no Calendar, no manual coordination."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(companies.router)
app.include_router(events.router)
app.include_router(triage.router)
app.include_router(settings_router.router)


@app.get("/health", tags=["Meta"])
async def health_check() -> dict[str, str]:
    return {"status": "healthy"}
