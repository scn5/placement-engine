"""
app/telegram/client.py
Telethon TelegramClient factory supporting both file-based sessions
(TELEGRAM_SESSION_NAME, default "placement_session") and stateless
StringSession (TELEGRAM_SESSION_STRING).
"""
import logging
from pathlib import Path
from typing import Union

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def get_session_path() -> str:
    """Return the absolute path (without .session suffix) for the Telethon session file."""
    return str(PROJECT_ROOT / settings.TELEGRAM_SESSION_NAME)


def create_telegram_client() -> TelegramClient:
    """Construct a Telethon client from Settings.

    If TELEGRAM_SESSION_STRING is set in .env, uses StringSession.
    Otherwise uses the local SQLite session file at PROJECT_ROOT / TELEGRAM_SESSION_NAME
    (e.g. `placement_session.session` created by `scripts/login_telegram.py`).
    """
    session: Union[StringSession, str]
    if settings.TELEGRAM_SESSION_STRING:
        session = StringSession(settings.TELEGRAM_SESSION_STRING)
        session_label = "string_session"
    else:
        session = get_session_path()
        session_label = session

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
        session_label,
    )
    return client

