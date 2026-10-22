"""
app/worker.py
Ingestion worker — the only process that talks to Telegram.

Owns the Telethon client, the Telegram → dispatcher bridge, and the background
loops:
  • monitored-chat refresh every CONFIG_REFRESH_SECONDS — picks up Settings
    changes, IGNORE / un-IGNORE and deletions; chats that become monitored get a
    bounded 24-hour catch-up (PROJECT_STATE §0.1 invariant 4),
  • catch-up at boot and every CATCHUP_INTERVAL_SECONDS,
  • retries of failed batches with backoff, polled every RETRY_POLL_SECONDS,
  • the worker_status heartbeat every HEARTBEAT_SECONDS, plus the optional
    dead-man's-switch ping.

Run exactly ONE worker per Telegram session — Telegram revokes a session that
is used from two places at once:

    python -m app.worker

Exit codes: 0 = Telegram disabled (TELEGRAM_API_ID=0) or a clean stop;
1 = the listener stopped or crashed (safe to restart); 2 = the session is not
authorized or was revoked (re-run scripts/login_telegram.py). Before exiting
with 2 the worker waits AUTH_FAILURE_WAIT_SECONDS, so a supervisor that
restarts it doesn't hammer Telegram.
"""
import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, List, Optional

import httpx
from telethon import errors as tg_errors

from app import database
from app.ai import dispatcher
from app.config import settings
from app.models import WorkerStatus
from app.telegram import client as telegram_client
from app.telegram.listener import TelegramIngestionService

logger = logging.getLogger("app.worker")

EXIT_OK = 0
EXIT_RESTART = 1
EXIT_SESSION = 2

AUTH_FAILURE_WAIT_SECONDS = 600.0
SHUTDOWN_FLUSH_TIMEOUT_SECONDS = 30.0
DEADMAN_PING_INTERVAL = timedelta(minutes=5)
_NEW_CHAT_CATCH_UP = timedelta(hours=24)

# Errors meaning the session itself is unusable — restarting will not help.
_SESSION_ERRORS = tuple(
    getattr(tg_errors, name)
    for name in (
        "AuthKeyUnregisteredError",
        "AuthKeyDuplicatedError",
        "AuthKeyInvalidError",
        "SessionRevokedError",
        "SessionExpiredError",
        "UserDeactivatedError",
        "UserDeactivatedBanError",
    )
    if hasattr(tg_errors, name)
)


async def dispatch_to_ai(chat_id: int, context_header: str, messages: List[dict]) -> None:
    """Telegram → dispatcher bridge. Each batch gets its own database session.
    Errors propagate, so the listener records the batch as FAILED and retries it."""
    async with database.AsyncSessionLocal() as db:
        await dispatcher.process_and_dispatch(
            chat_id=chat_id,
            context_header=context_header,
            messages=messages,
            db=db,
        )


class StatusReporter:
    """Persists the worker's liveness into the single worker_status row and
    pings the optional dead-man's-switch URL while Telegram is connected."""

    def __init__(self) -> None:
        self._last_ping: Optional[datetime] = None

    async def write(self, **fields) -> None:
        async with database.AsyncSessionLocal() as db:
            row = await db.get(WorkerStatus, 1)
            if row is None:
                row = WorkerStatus()
                db.add(row)
            for name, value in fields.items():
                setattr(row, name, value)
            await db.commit()

    async def write_safely(self, **fields) -> None:
        try:
            await self.write(**fields)
        except Exception:
            logger.exception("Could not write worker_status.")

    async def beat(self, client, service: TelegramIngestionService) -> None:
        now = datetime.now(timezone.utc)
        state = "CONNECTED" if client.is_connected() else "DISCONNECTED"
        await self.write(
            telegram_state=state,
            heartbeat_at=now,
            last_update_at=service.last_update_at,
            last_llm_ok_at=service.last_dispatch_ok_at,
            catchup_at=service.last_catchup_at,
            last_error=service.last_error,
        )
        if state == "CONNECTED":
            await self._ping_deadman(now)

    async def _ping_deadman(self, now: datetime) -> None:
        if not settings.DEADMAN_URL:
            return
        if self._last_ping is not None and now - self._last_ping < DEADMAN_PING_INTERVAL:
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                await http.get(settings.DEADMAN_URL)
            self._last_ping = now
        except httpx.HTTPError as exc:
            logger.warning("Dead-man's-switch ping failed: %s", exc)


async def _every(
    seconds: float,
    job: Callable[[], Awaitable[object]],
    name: str,
    *,
    first_delay: float = 0.0,
) -> None:
    """Run *job* every *seconds* until cancelled. A failed run is logged and the
    loop carries on, so one bad cycle never stops ingestion."""
    if first_delay:
        await asyncio.sleep(first_delay)
    while True:
        try:
            await job()
        except Exception:
            logger.exception("Background job %r failed; next run in %.0f s.", name, seconds)
        await asyncio.sleep(seconds)


async def _refresh_and_catch_up(service: TelegramIngestionService) -> None:
    """Refresh the monitored chats; chats that just became monitored (a new
    feed in Settings, an un-ignored company) get a bounded 24 h catch-up."""
    added = await service.refresh_monitored()
    if added:
        await service.catch_up(added, max_age=_NEW_CHAT_CATCH_UP)


def _install_stop_handlers(stop: asyncio.Event) -> None:
    """Stop cleanly on SIGTERM/SIGINT (e.g. `docker stop`). Windows event loops
    don't support signal handlers; there Ctrl+C raises KeyboardInterrupt."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass


async def _session_failure(reporter: StatusReporter, state: str, reason: str) -> int:
    logger.error(
        "%s Re-run scripts/login_telegram.py, then restart the worker (exiting in %.0f s).",
        reason, AUTH_FAILURE_WAIT_SECONDS,
    )
    await reporter.write_safely(telegram_state=state, last_error=reason)
    await asyncio.sleep(AUTH_FAILURE_WAIT_SECONDS)
    return EXIT_SESSION


async def _listener_exit_code(listener: asyncio.Task, reporter: StatusReporter) -> int:
    exc = None if listener.cancelled() else listener.exception()
    if isinstance(exc, _SESSION_ERRORS):
        return await _session_failure(
            reporter, "SESSION_INVALID", f"Telegram rejected the session ({type(exc).__name__})."
        )
    reason = f"{type(exc).__name__}: {exc}"[:200] if exc else "Disconnected from Telegram"
    logger.error("Telegram listener stopped (%s); exiting so the supervisor restarts the worker.", reason)
    await reporter.write_safely(telegram_state="DISCONNECTED", last_error=reason)
    return EXIT_RESTART


async def run_worker() -> int:
    """Run the ingestion worker until it stops; returns the process exit code."""
    if not settings.telegram_enabled:
        logger.warning(
            "Telegram is disabled (TELEGRAM_API_ID=0 or TELEGRAM_API_HASH empty): "
            "the ingestion worker will not start."
        )
        return EXIT_OK

    await database.assert_schema_current()
    reporter = StatusReporter()
    client = telegram_client.create_telegram_client()
    service: Optional[TelegramIngestionService] = None
    tasks: List[asyncio.Task] = []
    stop = asyncio.Event()
    _install_stop_handlers(stop)

    try:
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
        except _SESSION_ERRORS as exc:
            return await _session_failure(
                reporter, "SESSION_INVALID", f"Telegram rejected the session ({type(exc).__name__})."
            )
        if not authorized:
            return await _session_failure(
                reporter, "UNAUTHORIZED", "The Telegram session is not authorized."
            )

        service = TelegramIngestionService(client, settings.ingest_environments)
        await service.initialize()
        service.register_handlers()
        await service.set_payload_handler(dispatch_to_ai)

        feeds = await service.get_root_feed_ids()
        logger.info(
            "Ingestion worker active for %s. Root feeds: Main=%s, Test=%s; %d subchannel(s).",
            ",".join(sorted(service.environments)),
            feeds.get("MAIN", "NOT_SET"),
            feeds.get("TEST", "NOT_SET"),
            len(service.active_subchannel_ids),
        )
        await reporter.write_safely(
            telegram_state="CONNECTED", heartbeat_at=datetime.now(timezone.utc), last_error=None
        )

        listener = asyncio.create_task(client.run_until_disconnected(), name="telegram-listener")
        stopper = asyncio.create_task(stop.wait(), name="stop-signal")
        tasks = [
            stopper,
            asyncio.create_task(service.catch_up(), name="catch-up-boot"),
            asyncio.create_task(
                _every(
                    settings.CONFIG_REFRESH_SECONDS,
                    lambda: _refresh_and_catch_up(service),
                    "config-refresh",
                    first_delay=settings.CONFIG_REFRESH_SECONDS,
                ),
                name="config-refresh",
            ),
            asyncio.create_task(
                _every(
                    settings.CATCHUP_INTERVAL_SECONDS,
                    service.catch_up,
                    "catch-up",
                    first_delay=settings.CATCHUP_INTERVAL_SECONDS,
                ),
                name="catch-up",
            ),
            asyncio.create_task(
                _every(
                    settings.RETRY_POLL_SECONDS,
                    service.retry_failed,
                    "retry-failed",
                    first_delay=settings.RETRY_POLL_SECONDS,
                ),
                name="retry-failed",
            ),
            asyncio.create_task(
                _every(settings.HEARTBEAT_SECONDS, lambda: reporter.beat(client, service), "heartbeat"),
                name="heartbeat",
            ),
        ]

        done, _ = await asyncio.wait({listener, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if listener not in done:
            logger.info("Stop requested; shutting down the ingestion worker.")
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
            await reporter.write_safely(telegram_state="STOPPED")
            return EXIT_OK
        return await _listener_exit_code(listener, reporter)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if service is not None:
            try:
                await asyncio.wait_for(service.shutdown(), timeout=SHUTDOWN_FLUSH_TIMEOUT_SECONDS)
            except Exception:
                logger.exception(
                    "Flushing debounce buffers on shutdown did not complete; catch-up will recover them."
                )
        if client.is_connected():
            await client.disconnect()


async def _main() -> int:
    try:
        return await run_worker()
    finally:
        await database.engine.dispose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )
    try:
        code = asyncio.run(_main())
    except KeyboardInterrupt:
        code = EXIT_OK
    sys.exit(code)


if __name__ == "__main__":
    main()
