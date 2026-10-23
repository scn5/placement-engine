"""
app/telegram/backfill.py
Bounded historical message fetcher.

Fetches the messages of a chat newer than a given id and younger than a given
age, oldest first. The listener routes them like live messages — the 24-hour
backfill of a newly linked channel is a catch-up with a 24-hour window.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)

_BACKFILL_WINDOW_HOURS = 24


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce a tz-naive datetime (e.g. from Telethon on some backends) to UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def fetch_messages_since(
    client: "TelegramClient",
    entity,
    min_id: int = 0,
    max_age: timedelta = timedelta(hours=_BACKFILL_WINDOW_HOURS),
    limit: int = 200,
) -> list:
    """Raw messages newer than *min_id* and younger than *max_age*, oldest first.

    Used by catch-up and backfill; the messages are routed through the listener
    like live events. Telethon iterates newest-first, so the scan stops at the
    first message older than the cutoff.
    """
    cutoff = datetime.now(timezone.utc) - max_age
    found = []
    async for message in client.iter_messages(entity, min_id=min_id, limit=limit):
        msg_date = _to_utc(getattr(message, "date", None))
        if msg_date is None or msg_date < cutoff:
            break
        found.append(message)
    if len(found) == limit:
        logger.warning(
            "Catch-up for entity=%s hit the %d-message cap; older messages after id %s were not fetched",
            getattr(entity, "id", entity), limit, min_id,
        )
    found.reverse()
    return found
