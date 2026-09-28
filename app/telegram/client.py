"""
app/telegram/client.py
Telethon TelegramClient factory using StringSession for credential-free restarts.
"""
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings

logger = logging.getLogger(__name__)


def create_telegram_client() -> TelegramClient:
    """Construct a Telethon client from Settings.

    Uses StringSession so no session file is written to disk — the session
    token lives entirely in the environment variable TELEGRAM_SESSION_STRING.
    Pass an empty string to start a fresh (interactive) session.
    """
    session = StringSession(settings.TELEGRAM_SESSION_STRING or "")
    client = TelegramClient(
        session=session,
        api_id=settings.TELEGRAM_API_ID,
        api_hash=settings.TELEGRAM_API_HASH,
        # Flood-sleep threshold: auto-sleep up to 60 s on FloodWaitError
        flood_sleep_threshold=60,
    )
    logger.info(
        "Telegram client created (api_id=%s, session=%s)",
        settings.TELEGRAM_API_ID,
        "provided" if settings.TELEGRAM_SESSION_STRING else "fresh",
    )
    return client
