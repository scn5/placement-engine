"""
verify_phase3.py
Phase 3 — Telegram MTProto Ingestion Engine verification suite.

Uses only mock objects and asyncio — no live Telegram credentials required.
Run: python verify_phase3.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Patch DB engine BEFORE importing app modules (same pattern as Phase 2/4):
# the listener reads companies and writes the message ledger through
# app.database, so it must never reach the configured database.
# ---------------------------------------------------------------------------
import warnings

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database as _db_module

_test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
)
_TestSession = async_sessionmaker(
    bind=_test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)
_db_module.engine = _test_engine
_db_module.AsyncSessionLocal = _TestSession
_db_module.install_sqlite_pragmas(_test_engine)  # enforce foreign keys like production

from app.models import Base, NaiveDatetimeWarning

# Every datetime handed to the ORM must be timezone-aware.
warnings.simplefilter("error", NaiveDatetimeWarning)

# ---------------------------------------------------------------------------
# Import application modules under test
# ---------------------------------------------------------------------------
from app.telegram.pre_filter import should_filter_noise
from app.telegram.debouncer import ChannelDebouncer
from app.telegram.backfill import fetch_24h_backfill
from app.telegram.listener import TelegramIngestionService, _build_message_dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_message(
    msg_id: int,
    text: str,
    date: datetime,
    media=None,
    reply_to_msg_id=None,
) -> SimpleNamespace:
    """Lightweight Telethon Message stand-in."""
    return SimpleNamespace(
        id=msg_id,
        text=text,
        date=date,
        media=media,
        reply_to_msg_id=reply_to_msg_id,
    )


def _make_event(chat_id: int, message: SimpleNamespace) -> SimpleNamespace:
    """Lightweight Telethon Event stand-in."""
    return SimpleNamespace(chat_id=chat_id, message=message)


# ---------------------------------------------------------------------------
# Test 1 — Pre-filter: noise gate determinism
# ---------------------------------------------------------------------------

async def test_pre_filter() -> None:
    # ── Should DROP ──
    cases_drop = [
        # Empty / whitespace
        (None, False, "None text"),
        ("", False, "empty string"),
        ("   ", False, "whitespace only"),
        # Too short
        ("ok", False, "too short"),
        # Admin SPOC chatter
        ("Rudrash kumar -> send phone number", False, "SPOC send phone"),
        ("dm me", False, "dm me"),
        ("ping me", False, "ping me"),
        ("thanks", False, "thanks"),
        # No recruitment signals whatsoever
        ("Hey everyone, good morning!", False, "generic greeting"),
        ("Please check your mails.", False, "vague mail reminder"),
    ]
    for text, has_media, label in cases_drop:
        result = should_filter_noise(text, has_media)
        assert result is True, f"Expected DROP for [{label}] but got KEEP"

    # ── Should KEEP ──
    cases_keep = [
        # Has media — always keep
        (None, True, "no-text but has media"),
        ("", True, "empty text but has media"),
        # Recruitment signals
        ("Deadline for IRIS registration is 10 PM tonight.", False, "IRIS deadline"),
        ("OA link: https://hackerrank.com/xyz", False, "OA with link"),
        ("Shortlist for DE Shaw interviews out!", False, "shortlist keyword"),
        ("PPT rescheduled to 5:30 PM", False, "PPT time rescheduled"),
        ("CTC: 45 LPA. Stipend: 80k/month", False, "ctc stipend"),
        ("Form link extended till Oct 15", False, "form extended date"),
        ("Interview round 1 results out", False, "interview round"),
    ]
    for text, has_media, label in cases_keep:
        result = should_filter_noise(text, has_media)
        assert result is False, f"Expected KEEP for [{label}] but got DROP"

    print("  [1] ✓ Pre-filter noise gate verified (DROP and KEEP cases)")


# ---------------------------------------------------------------------------
# Test 2 — Debouncer: sliding window consolidation
# ---------------------------------------------------------------------------

async def test_debounce_consolidation() -> None:
    debouncer = ChannelDebouncer()
    received: List[dict] = []

    async def callback(chat_id: int, messages: List[dict]) -> None:
        received.extend(messages)

    CHAT_ID = 999
    DELAY = 0.10  # 100 ms — fast enough for tests

    t_base = _now().timestamp()

    msg1 = {"id": 1, "text": "OA is tomorrow", "date": t_base + 0.00, "is_edit": False, "reply_to_msg_id": None}
    msg2 = {"id": 2, "text": "Link: https://oa.example.com", "date": t_base + 0.02, "is_edit": False, "reply_to_msg_id": None}
    msg3 = {"id": 3, "text": "Deadline: 11:59 PM", "date": t_base + 0.04, "is_edit": False, "reply_to_msg_id": None}

    # Burst: add 3 messages 20 ms apart — each resets the 100 ms timer
    await debouncer.add_message(CHAT_ID, msg1, callback, DELAY)
    await asyncio.sleep(0.020)
    await debouncer.add_message(CHAT_ID, msg2, callback, DELAY)
    await asyncio.sleep(0.020)
    await debouncer.add_message(CHAT_ID, msg3, callback, DELAY)

    # Wait for the debounce window to close (100 ms + generous buffer)
    await asyncio.sleep(0.160)

    assert len(received) == 3, f"Expected 3 messages in consolidated payload, got {len(received)}"
    # Chronological order by date
    assert received[0]["id"] == 1
    assert received[1]["id"] == 2
    assert received[2]["id"] == 3

    print("  [2] ✓ Debouncer: 3-burst → single consolidated payload verified")


# ---------------------------------------------------------------------------
# Test 3 — Edit event: is_edit flag propagates through debounce
# ---------------------------------------------------------------------------

async def test_edit_event_debounce() -> None:
    debouncer = ChannelDebouncer()
    received: List[dict] = []

    async def callback(chat_id: int, messages: List[dict]) -> None:
        received.extend(messages)

    CHAT_ID = 777
    DELAY = 0.08

    t_base = _now().timestamp()

    original = {"id": 50, "text": "Interview at 3pm", "date": t_base, "is_edit": False, "reply_to_msg_id": None}
    edit = {"id": 50, "text": "Interview at 4pm (rescheduled)", "date": t_base + 0.01, "is_edit": True, "reply_to_msg_id": None}

    await debouncer.add_message(CHAT_ID, original, callback, DELAY)
    await asyncio.sleep(0.015)
    await debouncer.add_message(CHAT_ID, edit, callback, DELAY)

    await asyncio.sleep(0.120)

    assert len(received) == 2, f"Expected 2 messages, got {len(received)}"
    assert received[1]["is_edit"] is True, "Edited message must carry is_edit=True"
    assert "4pm" in received[1]["text"], "Edit text not preserved"

    print("  [3] ✓ Edit event: is_edit=True propagates through debounce window")


# ---------------------------------------------------------------------------
# Test 4 — 24h backfill: strict time-window filtering & chronological index
# ---------------------------------------------------------------------------

async def test_backfill_time_window() -> None:
    now = _now()

    # Mix of messages: 2 within 24h, 1 older than 24h
    old_msg   = _make_message(1, "Old announcement (36h ago)",   now - timedelta(hours=36))
    recent1   = _make_message(2, "IRIS deadline is tomorrow",    now - timedelta(hours=12))
    recent2   = _make_message(3, "Form: https://forms.gle/xyz",  now - timedelta(hours=1))

    all_messages = [old_msg, recent1, recent2]  # includes the old one

    # Mock client.iter_messages as an async generator that yields all messages
    async def mock_iter(*args, **kwargs):
        for m in all_messages:
            yield m

    mock_client = MagicMock()
    mock_client.iter_messages = mock_iter
    mock_entity = MagicMock()
    mock_entity.id = 12345

    results = await fetch_24h_backfill(mock_client, mock_entity)

    assert len(results) == 2, f"Expected 2 results (within 24h), got {len(results)}"
    assert results[0]["id"] == 2, "First result must be the 12h-old message"
    assert results[1]["id"] == 3, "Second result must be the 1h-old message"
    # Chronological numbering
    assert results[0]["label"] == "[1]"
    assert results[1]["label"] == "[2]"
    assert results[0]["index"] == 1
    assert results[1]["index"] == 2
    # Strict ascending order
    assert results[0]["timestamp"] < results[1]["timestamp"], "Results must be in ascending time order"

    print("  [4] ✓ 24h backfill: old messages excluded, chronological indexing verified")


# ---------------------------------------------------------------------------
# Test 5 — Main-feed reply-to stitching
# ---------------------------------------------------------------------------

async def test_reply_to_stitching() -> None:
    parent_text = "DE Shaw placement drive 2024. Registration opens soon."
    child_text  = "Registration form: https://forms.gle/deshaw-2024. Deadline: Oct 15, 11:59 PM"

    # Mock Telethon client: get_messages returns the parent
    mock_parent_msg = MagicMock()
    mock_parent_msg.text = parent_text

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(return_value=mock_parent_msg)

    service = TelegramIngestionService(mock_client)

    # ── Test _fetch_parent_context ──
    fetched_context = await service._fetch_parent_context(
        chat_id=11111, reply_to_msg_id=42
    )
    assert fetched_context == parent_text, (
        f"Expected parent text, got: {fetched_context!r}"
    )
    mock_client.get_messages.assert_awaited_once_with(11111, ids=42)

    # ── Test _build_message_dict stitches parent into output ──
    child_msg = _make_message(
        msg_id=43,
        text=child_text,
        date=_now(),
        reply_to_msg_id=42,
    )
    msg_dict = _build_message_dict(child_msg, is_edit=False, parent_context=fetched_context)

    assert msg_dict["parent_context"] == parent_text
    assert "[CONTEXT:" in msg_dict["text"], "Stitched text must contain [CONTEXT: ...]"
    assert parent_text in msg_dict["text"], "Parent text must be present in stitched output"
    assert child_text in msg_dict["text"], "Child text must be present in stitched output"
    assert msg_dict["reply_to_msg_id"] == 42
    assert msg_dict["is_edit"] is False

    print("  [5] ✓ Reply-to stitching: parent context merged into child payload")


# ---------------------------------------------------------------------------
# Test 6 — Listener dispatches via payload handler (smoke test)
# ---------------------------------------------------------------------------

async def test_listener_payload_handler() -> None:
    """Verify that messages routed through the listener reach the handler."""
    received_payloads = []

    async def handler(chat_id: int, context: str, messages: List[dict]) -> None:
        received_payloads.append((chat_id, context, messages))

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(return_value=None)

    service = TelegramIngestionService(mock_client)
    await service.set_payload_handler(handler)

    # Patch settings so subchannel debounce fires instantly
    with patch("app.telegram.listener.settings") as mock_settings:
        mock_settings.MAIN_FEED_CHANNEL_ID = 999
        mock_settings.DEBOUNCE_DELAY_SECONDS = 0.05

        # ── Subchannel message with actionable content ──
        msg = _make_message(
            msg_id=1,
            text="OA deadline: 10:00 PM tonight. Link: https://oa.example.com",
            date=_now(),
        )
        event = _make_event(chat_id=88888, message=msg)  # non-main-feed chat
        await service._handle_subchannel(event, is_edit=False)

        await asyncio.sleep(0.10)  # wait for debounce

    assert len(received_payloads) == 1, f"Expected 1 payload, got {len(received_payloads)}"
    chat_id, context, messages = received_payloads[0]
    assert chat_id == 88888
    assert context == "subchannel"
    assert messages[0]["id"] == 1

    print("  [6] ✓ Listener payload handler dispatch verified (subchannel route)")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    # Bootstrap schema in the in-memory database
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("\nRunning Phase 3 verification suite…\n")

    await test_pre_filter()
    await test_debounce_consolidation()
    await test_edit_event_debounce()
    await test_backfill_time_window()
    await test_reply_to_stitching()
    await test_listener_payload_handler()

    await _test_engine.dispose()
    print("\n✅ Phase 3 Telegram Ingestion Engine Verified Successfully!")


if __name__ == "__main__":
    asyncio.run(main())
