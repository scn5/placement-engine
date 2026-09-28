"""
app/main.py
FastAPI application entry-point — Phase 3 update.

Lifespan:
  1. Bootstrap PostgreSQL schema (create_all).
  2. If TELEGRAM_API_ID is configured, start the Telethon ingestion service
     and schedule client.run_until_disconnected() as a background task.
  3. On shutdown: flush debounce buffers, cancel the background task,
     and disconnect the Telegram client cleanly.
"""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import AsyncGenerator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine
from app.models import Base  # noqa: F401 — registers all ORM models
from app.routers import companies, events

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # ── 1. Database schema bootstrap ──────────────────────────────────
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema ready.")

    # ── 2. Telegram ingestion engine (optional — requires credentials) ─
    tg_client = None
    tg_service = None
    tg_task: Optional[asyncio.Task] = None

    if settings.TELEGRAM_API_ID:
        try:
            # Deferred import so the app starts without Telegram creds in CI
            from app.telegram.client import create_telegram_client
            from app.telegram.listener import TelegramIngestionService

            tg_client = create_telegram_client()
            await tg_client.start(phone=settings.TELEGRAM_PHONE or None)

            tg_service = TelegramIngestionService(tg_client)
            tg_service.register_handlers()

            # Run the Telethon event loop as a background asyncio task
            tg_task = asyncio.create_task(
                tg_client.run_until_disconnected(),
                name="telegram-listener",
            )
            logger.info("Telegram ingestion engine started.")
        except Exception:
            logger.exception(
                "Failed to start Telegram client — running without ingestion."
            )
    else:
        logger.info(
            "TELEGRAM_API_ID not set — Telegram ingestion engine disabled."
        )

    # ── Hand control back to FastAPI ──────────────────────────────────
    yield

    # ── 3. Graceful shutdown ──────────────────────────────────────────
    if tg_service:
        await tg_service.shutdown()

    if tg_task and not tg_task.done():
        tg_task.cancel()
        with suppress(asyncio.CancelledError):
            await tg_task

    if tg_client and tg_client.is_connected():
        await tg_client.disconnect()
        logger.info("Telegram client disconnected.")

    await engine.dispose()
    logger.info("Database engine disposed.")


app = FastAPI(
    title="Automated Placement Tracking Engine",
    version="0.3.0",
    description=(
        "Single source of truth for NITK placement tracking — "
        "no Sheets, no Calendar, no manual coordination."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(companies.router)
app.include_router(events.router)


@app.get("/health", tags=["Meta"])
async def health_check() -> dict[str, str]:
    return {"status": "healthy"}
