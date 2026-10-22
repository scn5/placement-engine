"""
app/telegram/ledger.py
Per-message processing ledger (table ``message_ledger``).

Records whether each Telegram message was processed (DONE), dropped on purpose
(SKIPPED) or failed (FAILED), so the worker can:
  • skip messages it already handled (catch-up, backfill, retries),
  • find where to resume each chat after downtime (the cursor),
  • retry failed messages with backoff instead of losing them.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Set

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import database
from app.models import MessageLedger

logger = logging.getLogger(__name__)

DONE = "DONE"
FAILED = "FAILED"
SKIPPED = "SKIPPED"

# Delay before the next attempt after the Nth consecutive failure; the last
# value repeats. After MAX_ATTEMPTS failures a message is no longer retried.
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
)
MAX_ATTEMPTS = 6

_REASON_LIMIT = 500


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def retry_delay(failures: int) -> timedelta:
    """Delay before the next attempt after *failures* consecutive failures."""
    return RETRY_DELAYS[min(max(failures, 1), len(RETRY_DELAYS)) - 1]


async def _upsert(
    chat_id: int,
    message_ids: Iterable[int],
    apply: Callable[[MessageLedger], None],
) -> None:
    ids = sorted(set(message_ids))
    if not ids:
        return
    for attempt in (1, 2):  # a concurrent insert of the same row wins once; retry as an update
        async with database.AsyncSessionLocal() as db:
            for message_id in ids:
                row = await db.get(MessageLedger, (chat_id, message_id))
                if row is None:
                    row = MessageLedger(chat_id=chat_id, message_id=message_id, status=FAILED)
                    db.add(row)
                apply(row)
            try:
                await db.commit()
                return
            except IntegrityError:
                await db.rollback()
                if attempt == 2:
                    raise


async def record(
    chat_id: int,
    message_ids: Iterable[int],
    status: str,
    reason: Optional[str] = None,
) -> None:
    """Record DONE or SKIPPED outcomes; clears any pending retry."""

    def apply(row: MessageLedger) -> None:
        row.status = status
        row.reason = reason[:_REASON_LIMIT] if reason else None
        row.attempts = 0
        row.next_attempt_at = None
        row.processed_at = _utc_now()

    await _upsert(chat_id, message_ids, apply)


async def record_failure(chat_id: int, message_ids: Iterable[int], error: str) -> None:
    """Record a failed attempt and schedule the next one with backoff. After
    MAX_ATTEMPTS consecutive failures the row stays FAILED with no next attempt."""

    def apply(row: MessageLedger) -> None:
        failures = (row.attempts if row.status == FAILED else 0) + 1
        row.status = FAILED
        row.attempts = failures
        row.reason = error[:_REASON_LIMIT]
        row.processed_at = _utc_now()
        if failures < MAX_ATTEMPTS:
            row.next_attempt_at = _utc_now() + retry_delay(failures)
        else:
            row.next_attempt_at = None
            logger.error(
                "Giving up on message %s in chat %s after %d failed attempts: %s",
                row.message_id, chat_id, failures, error,
            )

    await _upsert(chat_id, message_ids, apply)


async def cursor(chat_id: int) -> Optional[int]:
    """Highest message id with any recorded outcome in *chat_id*, or None."""
    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.max(MessageLedger.message_id)).where(MessageLedger.chat_id == chat_id)
        )
        return result.scalar()


async def known_ids(chat_id: int, message_ids: Iterable[int]) -> Set[int]:
    """Which of *message_ids* already have an outcome recorded."""
    ids = list(set(message_ids))
    if not ids:
        return set()
    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            select(MessageLedger.message_id).where(
                MessageLedger.chat_id == chat_id,
                MessageLedger.message_id.in_(ids),
            )
        )
        return set(result.scalars().all())


async def due_retries(limit: int = 200) -> Dict[int, List[int]]:
    """FAILED messages whose next attempt is due, grouped by chat (oldest first)."""
    async with database.AsyncSessionLocal() as db:
        result = await db.execute(
            select(MessageLedger.chat_id, MessageLedger.message_id)
            .where(
                MessageLedger.status == FAILED,
                MessageLedger.next_attempt_at.is_not(None),
                MessageLedger.next_attempt_at <= _utc_now(),
            )
            .order_by(MessageLedger.next_attempt_at)
            .limit(limit)
        )
        due: Dict[int, List[int]] = {}
        for chat_id, message_id in result.all():
            due.setdefault(int(chat_id), []).append(int(message_id))
        return due


async def safely(write: Callable[..., Awaitable[None]], *args) -> None:
    """Run a ledger write without letting a database error escape into the
    Telegram event loop. A missed write only means a message may be fetched
    and processed again (at-least-once), never lost."""
    try:
        await write(*args)
    except Exception:
        logger.exception("Ledger write failed (%s)", getattr(write, "__name__", write))
