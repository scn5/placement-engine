"""
app/main.py
FastAPI application entry-point — Phase 4 update.

Lifespan sequence:
  1. Bootstrap PostgreSQL schema (create_all).
  2. Optionally start Telethon ingestion engine (requires TELEGRAM_API_ID).
  3. Wire telegram_service.set_payload_handler → dispatcher.process_and_dispatch.
  4. On shutdown: flush debounce buffers, cancel background task, disconnect client.
"""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import AsyncGenerator, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.models import Base  # noqa: F401 — registers all ORM models
from app.routers import companies, events, triage
from app.routers import settings as settings_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # ── 1. Database schema bootstrap ──────────────────────────────────────
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        with suppress(Exception):
            await conn.exec_driver_sql(
                "ALTER TABLE companies ADD COLUMN environment_type VARCHAR(16) NOT NULL DEFAULT 'MAIN'"
            )
    logger.info("Database schema ready.")

    # ── 2. Telegram ingestion engine (optional) ────────────────────────────
    tg_client = None
    tg_service = None
    tg_task: Optional[asyncio.Task] = None

    if settings.TELEGRAM_API_ID:
        try:
            from app.telegram.client import create_telegram_client
            from app.telegram.listener import TelegramIngestionService

            tg_client = create_telegram_client()
            await tg_client.start(phone=settings.TELEGRAM_PHONE or None)

            tg_service = TelegramIngestionService(tg_client)
            tg_service.register_handlers()

            # ── 3. Wire dispatcher as payload handler ──────────────────────
            from app.ai import dispatcher as _dispatcher

            async def _payload_handler(
                chat_id: int, context_header: str, messages: List[dict]
            ) -> None:
                """Bridge between the Telethon listener and the AI dispatcher.
                Each invocation opens its own DB session for isolation.
                """
                db = AsyncSessionLocal()
                try:
                    await _dispatcher.process_and_dispatch(
                        chat_id=chat_id,
                        context_header=context_header,
                        messages=messages,
                        db=db,
                    )
                except Exception:
                    logger.exception(
                        "Dispatcher raised for chat_id=%s context=%s",
                        chat_id, context_header,
                    )
                finally:
                    await db.close()

            await tg_service.set_payload_handler(_payload_handler)

            tg_task = asyncio.create_task(
                tg_client.run_until_disconnected(),
                name="telegram-listener",
            )
            logger.info("Telegram ingestion engine started and wired to AI dispatcher.")
        except Exception:
            logger.exception(
                "Failed to start Telegram client — running without ingestion."
            )
    else:
        logger.info("TELEGRAM_API_ID not set — Telegram ingestion engine disabled.")

    # ── Hand control back to FastAPI ──────────────────────────────────────
    yield

    # ── 4. Graceful shutdown ──────────────────────────────────────────────
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
    version="0.4.0",
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
app.include_router(triage.router)
app.include_router(settings_router.router)


@app.get("/health", tags=["Meta"])
async def health_check() -> dict[str, str]:
    return {"status": "healthy"}
