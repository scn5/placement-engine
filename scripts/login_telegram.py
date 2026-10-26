"""
scripts/login_telegram.py
Standalone interactive CLI script to authenticate a Telethon user session
for the Placement Engine backend.

The session file it creates is a live credential for the whole Telegram
account: it is never printed, copied or committed. (Earlier versions also
printed a StringSession for TELEGRAM_SESSION_STRING; that was removed in
Phase 0.6 — anything printed ends up in terminal scrollback and logs.)

Usage (from placement-engine/ directory):
    .\\venv\\Scripts\\python.exe scripts/login_telegram.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Ensure `placement-engine` root is on sys.path so `app.*` imports resolve
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from telethon import TelegramClient

# Load .env explicitly from placement-engine root
load_dotenv(PROJECT_ROOT / ".env")

from app.config import settings
from app.telegram.client import get_session_path


async def main() -> None:
    api_id = settings.TELEGRAM_API_ID or int(os.getenv("TELEGRAM_API_ID", "0") or 0)
    api_hash = (settings.TELEGRAM_API_HASH or os.getenv("TELEGRAM_API_HASH", "")).strip()

    if not api_id or not api_hash:
        print(
            "ERROR: Missing Telegram credentials in .env.\n"
            "Please set valid TELEGRAM_API_ID and TELEGRAM_API_HASH in placement-engine/.env "
            "before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    session_path = get_session_path()
    print(f"Initializing Telethon session at: {session_path}.session")

    client = TelegramClient(
        session=session_path,
        api_id=api_id,
        api_hash=api_hash,
    )

    try:
        # Interactive login: prompts for phone number, OTP code, and 2FA password if enabled
        await client.start()

        me = await client.get_me()
        full_name = " ".join(filter(None, [getattr(me, "first_name", None), getattr(me, "last_name", None)]))
        username_str = f" (@{me.username})" if getattr(me, "username", None) else ""

        print("\n============================================================")
        print("Telegram authentication successful!")
        print(f"  Logged in as : {full_name}{username_str}")
        print(f"  User ID      : {me.id}")
        print(f"  Session file : {session_path}.session")
        print("\nThe session file is a live login for this account: keep it private,")
        print("never commit, copy or share it. Run exactly one worker per session.")
        print("============================================================\n")
    finally:
        if client.is_connected():
            await client.disconnect()
            print("Telegram client disconnected cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
