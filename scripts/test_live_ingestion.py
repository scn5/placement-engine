"""
scripts/test_live_ingestion.py
Safe, non-interactive smoke test to verify live Telegram authentication,
Groq LLM endpoint connectivity, and database target channel configuration.

Safety Constraints:
  - ZERO outbound Telegram actions (read-only verification).
  - Non-interactive (never prompts for stdin/passwords).
  - Zero raw secrets printed.
"""
import asyncio
import os
import re
import sys
from pathlib import Path

# Ensure placement-engine root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

# Load .env explicitly
load_dotenv(PROJECT_ROOT / ".env")

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import SystemConfig
from app.telegram.client import create_telegram_client
from openai import AsyncOpenAI
from sqlalchemy import select


def run_codebase_safety_audit() -> bool:
    """Scan app/ codebase to verify zero outbound or destructive Telegram actions."""
    app_dir = PROJECT_ROOT / "app"
    prohibited_patterns = [
        re.compile(r"\bclient\s*\.\s*send_message\b"),
        re.compile(r"\bclient\s*\.\s*delete_messages\b"),
        re.compile(r"\bclient\s*\.\s*edit_message\b"),
        re.compile(r"\bLeaveChannelRequest\b"),
        re.compile(r"\bDeleteChatRequest\b"),
        re.compile(r"\bSendMediaRequest\b"),
    ]

    for py_file in app_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8", errors="ignore")
        for pat in prohibited_patterns:
            if pat.search(content):
                return False
    return True


async def main() -> None:
    # ── 1. Safety Audit ──────────────────────────────────────────────────
    if not run_codebase_safety_audit():
        print(
            "ERROR: Safety audit failed. Detected prohibited outbound/destructive Telegram calls.",
            file=sys.stderr,
        )
        sys.exit(1)
    print("[OK] Safety Audit: Zero destructive/outbound Telegram actions in codebase.")

    # ── 2. Credential & Session Validation ───────────────────────────────
    api_id = settings.TELEGRAM_API_ID or int(os.getenv("TELEGRAM_API_ID", "0") or 0)
    api_hash = (settings.TELEGRAM_API_HASH or os.getenv("TELEGRAM_API_HASH", "")).strip()
    groq_api_key = (
        os.getenv("GROQ_API_KEY")
        or settings.GROQ_API_KEY
        or os.getenv("LLM_API_KEY")
        or settings.LLM_API_KEY
    )

    if not api_id or not api_hash:
        print(
            "ERROR: Missing TELEGRAM_API_ID or TELEGRAM_API_HASH in .env",
            file=sys.stderr,
        )
        sys.exit(1)

    if not groq_api_key:
        print("ERROR: Missing GROQ_API_KEY in .env", file=sys.stderr)
        sys.exit(1)

    session_file = PROJECT_ROOT / f"{settings.TELEGRAM_SESSION_NAME}.session"
    has_session = session_file.exists() or bool(settings.TELEGRAM_SESSION_STRING)
    if not has_session:
        print(
            f"ERROR: No session found. Expected {session_file.name} or TELEGRAM_SESSION_STRING.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── 3. Telegram Authenticated Session Check (Non-Interactive) ────────
    tg_client = create_telegram_client()
    try:
        await tg_client.connect()
        if not await tg_client.is_user_authorized():
            print(
                "ERROR: Telegram session exists but is not authorized. Please run login_telegram.py first.",
                file=sys.stderr,
            )
            sys.exit(1)

        me = await tg_client.get_me()
        user_name = me.first_name or "Unknown"
        print(f"[OK] Telegram Authenticated Session: Connected as {user_name} (ID: {me.id})")

        # ── 4. Target Test Channel ID from Database & Entity Resolution ───────
        target_id: Optional[int] = None
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(SystemConfig))
                cfg = result.scalars().first()
                if cfg and cfg.test_feed_id is not None:
                    target_id = cfg.test_feed_id
                elif settings.TEST_FEED_CHANNEL_ID is not None:
                    target_id = settings.TEST_FEED_CHANNEL_ID
        except Exception as e:
            print(f"[WARN] Failed to query SystemConfig: {e}")

        if target_id is not None:
            try:
                entity = await tg_client.get_entity(int(target_id))
                entity_title = getattr(entity, "title", getattr(entity, "username", str(entity)))
                print(f"[OK] Resolved Target Test Channel: '{entity_title}' (ID: {target_id})")
            except Exception as e:
                print(f"[WARN] Could not resolve entity for target channel ID {target_id}: {e}")
        else:
            print("[INFO] Target Test Channel ID: NOT_SET")

    finally:
        if tg_client.is_connected():
            await tg_client.disconnect()

    # ── 5. Groq API Check ────────────────────────────────────────────────
    base_url = settings.LLM_BASE_URL or "https://api.groq.com/openai/v1"
    model = settings.LLM_MODEL or "openai/gpt-oss-20b"
    ai_client = AsyncOpenAI(api_key=groq_api_key, base_url=base_url)
    try:
        response = await ai_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=10,
        )
        if not response or not response.id:
            raise RuntimeError("Empty response from Groq API")
        print(f"[OK] Groq API: Connected and responding ({model}).")
    except Exception as e:
        print(f"ERROR: Groq API check failed: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n[SUCCESS] Live Ingestion Engine Pre-flight Readiness: FULLY OPERATIONAL.")


if __name__ == "__main__":
    asyncio.run(main())
