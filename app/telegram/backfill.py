"""
app/telegram/backfill.py
24-hour bounded historical message fetcher.

Fetches all messages from a Telegram entity posted in the last 24 hours,
returns them as a chronologically ordered list of normalised dicts ready
for pipeline ingestion.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Optional

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


async def fetch_24h_backfill(client: "TelegramClient", entity) -> List[dict]:
    """Fetch the last 24 hours of messages from *entity* in chronological order.

    Returns a list of normalised message dicts:
        {
            "index":     int,          # 1-based chronological position
            "label":     str,          # "[1]", "[2]", …
            "id":        int,
            "text":      str,
            "raw_text":  str,
            "date":      float,        # UTC epoch seconds
            "timestamp": float,        # UTC epoch seconds
            "has_media": bool,
        }

    Safety filter: messages older than cutoff are excluded.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_BACKFILL_WINDOW_HOURS)
    entity_id = getattr(entity, "id", repr(entity))
    logger.info("Starting 24h backfill for entity=%s, cutoff=%s", entity_id, cutoff.isoformat())

    raw: List[dict] = []
    try:
        async for message in client.iter_messages(
            entity,
            limit=200,
        ):
            msg_date = _to_utc(getattr(message, "date", None))
            if msg_date is None or msg_date < cutoff:
                # Safety gate: drop anything outside the 24h window
                continue

            text = getattr(message, "text", "") or ""
            raw.append(
                {
                    "id": message.id,
                    "text": text,
                    "raw_text": text,
                    "date": msg_date.timestamp(),
                    "timestamp": msg_date.timestamp(),
                    "has_media": getattr(message, "media", None) is not None,
                }
            )
    except Exception:
        logger.exception("Error during backfill for entity=%s", entity_id)
        return []

    # Ensure strict chronological order
    raw.sort(key=lambda m: m["timestamp"])

    result: List[dict] = []
    for idx, entry in enumerate(raw, start=1):
        entry["index"] = idx
        entry["label"] = f"[{idx}]"
        result.append(entry)

    logger.info(
        "Backfill complete for entity=%s: %d messages within last 24h",
        entity_id,
        len(result),
    )
    return result
