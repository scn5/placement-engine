"""
app/main.py
FastAPI application entry-point — API only.

Telegram ingestion runs in its own process (`python -m app.worker`), so the API
can be restarted, or run with several workers, without touching the Telegram
session.

Security (Phase 0.6): every router requires the X-API-Key header (app/security.py);
only /health is public. There is no CORS middleware — the React Native app makes
native requests — and in production the interactive docs and the OpenAPI schema
are not served.

Lifespan sequence:
  1. Enforce the API key policy: production refuses to start without API_KEY.
  2. Verify the database is at the Alembic head revision (the schema itself is
     created and migrated only by `alembic upgrade head`).
  3. On shutdown: dispose the database engine.
"""
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import assert_schema_current, engine
from app.routers import companies, events, triage
from app.routers import settings as settings_router
from app.routers import status as status_router
from app.security import check_api_key_configuration, require_api_key

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # ── 1. API key policy (production refuses to start without a key) ──────
    check_api_key_configuration()

    # ── 2. Database schema check (Alembic owns the schema) ─────────────────
    await assert_schema_current()
    logger.info("Database schema is at the Alembic head revision.")

    # ── Hand control back to FastAPI ──────────────────────────────────────
    yield

    # ── 3. Shutdown ───────────────────────────────────────────────────────
    await engine.dispose()
    logger.info("Database engine disposed.")


def _constraint_kind(exc: IntegrityError) -> str:
    """foreign_key | unique | other, for SQLite and PostgreSQL drivers."""
    original = getattr(exc, "orig", None)
    code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    text = str(original).lower() if original is not None else ""
    if code == "23503" or "foreign key" in text:
        return "foreign_key"
    if code == "23505" or "unique" in text:
        return "unique"
    return "other"


async def _integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    """A constraint violation the route didn't catch: a clean 422/409 instead of
    a 500, and never the SQL or its parameters."""
    kind = _constraint_kind(exc)
    logger.warning("%s %s violated a database constraint (%s).", request.method, request.url.path, kind)
    if kind == "foreign_key":
        return JSONResponse(
            status_code=422,
            content={"detail": "The request refers to a record that does not exist."},
        )
    if kind == "unique":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "A record with these values already exists."},
        )
    return JSONResponse(
        status_code=422,
        content={"detail": "The request violates a data constraint."},
    )


def create_app() -> FastAPI:
    production = settings.is_production
    app = FastAPI(
        title="Automated Placement Tracking Engine",
        version="0.6.0",
        description=(
            "Single source of truth for NITK placement tracking — "
            "no Sheets, no Calendar, no manual coordination."
        ),
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )

    protected = [Depends(require_api_key)]
    for router in (companies.router, events.router, triage.router, settings_router.router, status_router.router):
        app.include_router(router, dependencies=protected)
    app.add_exception_handler(IntegrityError, _integrity_error)

    @app.get("/health", tags=["Meta"])
    async def health_check() -> dict[str, str]:
        """Public liveness probe of the API process; reveals nothing else."""
        return {"status": "ok"}

    return app


app = create_app()
