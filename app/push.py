"""
app/push.py
Expo Push API client and fan-out to every registered device.

Push is only a transport that wakes the app (MASTER_SPEC §1, §6.4). Nothing here
touches Telegram, so it can run in the API process. `send_expo_push` is the one
network call; the verify suites replace it.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_BATCH_SIZE = 100  # Expo accepts at most 100 messages per request
_TIMEOUT_SECONDS = 15.0
_GONE = "DeviceNotRegistered"


@dataclass(frozen=True)
class Ticket:
    """Expo's verdict on one message: ok, or an error (`gone` = the token is dead)."""

    ok: bool
    gone: bool = False
    error: Optional[str] = None


@dataclass(frozen=True)
class PushResult:
    devices: int
    sent: int
    failed: int
    removed: int


async def send_expo_push(messages: List[dict]) -> List[Ticket]:
    """POST up to 100 messages; one Ticket per message, in order. A transport or
    HTTP failure marks every message failed (never `gone`) so no token is removed
    over a network blip."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()["data"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("Expo push request failed: %s", type(exc).__name__)
        return [Ticket(ok=False, error=type(exc).__name__) for _ in messages]

    tickets: List[Ticket] = []
    for item in data:
        if item.get("status") == "ok":
            tickets.append(Ticket(ok=True))
        else:
            code = (item.get("details") or {}).get("error")
            tickets.append(Ticket(ok=False, gone=code == _GONE, error=code or item.get("message")))
    if len(tickets) != len(messages):  # a malformed reply: trust none of it
        return [Ticket(ok=False, error="MalformedResponse") for _ in messages]
    return tickets


async def push_to_all(
    db: AsyncSession, title: str, body: str, data: Optional[dict] = None
) -> Optional[PushResult]:
    """Send one notification to every registered device and delete the tokens
    Expo reports as gone. None when no device is registered."""
    tokens = list((await db.execute(select(models.DeviceToken))).scalars().all())
    if not tokens:
        return None

    messages = [
        {
            "to": t.expo_push_token,
            "title": title,
            "body": body,
            "data": data or {},
            "sound": "default",
            "priority": "high",
            "channelId": "command-center",
        }
        for t in tokens
    ]
    tickets: List[Ticket] = []
    for start in range(0, len(messages), _BATCH_SIZE):
        tickets.extend(await send_expo_push(messages[start:start + _BATCH_SIZE]))

    gone = [t.id for t, ticket in zip(tokens, tickets) if ticket.gone]
    if gone:
        await db.execute(delete(models.DeviceToken).where(models.DeviceToken.id.in_(gone)))
        await db.commit()
        logger.info("Removed %d push token(s) Expo reports as no longer registered.", len(gone))

    sent = sum(1 for ticket in tickets if ticket.ok)
    return PushResult(devices=len(tokens), sent=sent, failed=len(tickets) - sent, removed=len(gone))
