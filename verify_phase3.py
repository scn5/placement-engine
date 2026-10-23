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
from app.config import settings
from app.telegram.pre_filter import EMPTY, skip_reason
from app.telegram.debouncer import ChannelDebouncer
from app.telegram.backfill import fetch_messages_since
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
# Test 1 — Pre-filter: only messages without visible text are skipped
# ---------------------------------------------------------------------------

async def test_pre_filter() -> None:
    # ── Skipped: no visible text at all (media without a caption included) ──
    cases_skip = [
        (None, "None text"),
        ("", "empty string"),
        ("   \n\t ", "whitespace only"),
        ("\u200b\u200d\ufeff", "zero-width characters only"),
        ("\u3164", "Hangul filler (a blank-looking message)"),
    ]
    for text, label in cases_skip:
        assert skip_reason(text) == EMPTY, f"Expected SKIP for [{label}]"

    # ── Kept: everything else, however short or chatty ──
    cases_keep = [
        "Slot 1",
        "Room 204",
        "Link active",
        "ok",
        "\U0001f44d",
        "thanks",
        "Rudrash kumar -> send phone number",
        "Hey everyone, good morning!",
        "Please check your mails.",
        "Deadline for IRIS registration is 10 PM tonight.",
        "PPT rescheduled to 5:30 PM",
    ]
    for text in cases_keep:
        assert skip_reason(text) is None, f"Expected KEEP for {text!r}"

    print("  [1] ✓ Pre-filter: only empty/whitespace/invisible messages are skipped; short announcements are kept")


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
# Test 3 — Edit event: a buffered message collapses into its latest version
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

    assert len(received) == 1, f"Expected the two versions collapsed into 1 message, got {len(received)}"
    assert "4pm" in received[0]["text"], "The latest version's text must win"
    # The original was never dispatched, so the LLM sees the final text as a new message.
    assert received[0]["is_edit"] is False

    # An edit of a message dispatched earlier stays an edit.
    received.clear()
    first_edit = {**edit, "text": "Interview at 5pm"}
    second_edit = {**edit, "text": "Interview at 6pm"}
    await debouncer.add_message(CHAT_ID, first_edit, callback, DELAY)
    await debouncer.add_message(CHAT_ID, second_edit, callback, DELAY)
    await asyncio.sleep(0.120)
    assert [m["text"] for m in received] == ["Interview at 6pm"] and received[0]["is_edit"] is True

    print("  [3] ✓ Edit event: versions of one message collapse into the latest; is_edit only when an earlier version was dispatched")


# ---------------------------------------------------------------------------
# Test 4 — Bounded fetch: time window, min_id and chronological order
# ---------------------------------------------------------------------------

async def test_backfill_time_window() -> None:
    now = _now()

    old_msg = _make_message(1, "Old announcement (36h ago)", now - timedelta(hours=36))
    recent1 = _make_message(2, "IRIS deadline is tomorrow", now - timedelta(hours=12))
    recent2 = _make_message(3, "Form: https://forms.gle/xyz", now - timedelta(hours=1))
    calls = []

    # Telethon iterates newest-first and honours min_id.
    async def mock_iter(entity, min_id=0, limit=None):
        calls.append((entity, min_id, limit))
        for m in sorted([old_msg, recent1, recent2], key=lambda m: m.id, reverse=True):
            if m.id > min_id:
                yield m

    mock_client = MagicMock()
    mock_client.iter_messages = mock_iter

    results = await fetch_messages_since(mock_client, 12345, max_age=timedelta(hours=24))
    assert [m.id for m in results] == [2, 3], "Only the last 24 h, oldest first"

    results = await fetch_messages_since(mock_client, 12345, min_id=2, max_age=timedelta(hours=72))
    assert [m.id for m in results] == [3], "Only messages newer than min_id"
    assert calls[-1] == (12345, 2, 200)

    print("  [4] ✓ Bounded fetch: messages outside the window or at/below min_id excluded; oldest first")


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

    async def handler(source, messages: List[dict]) -> None:
        received_payloads.append((source, messages))

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(return_value=None)

    service = TelegramIngestionService(mock_client)
    service.active_subchannel_ids.add(88888)
    await service.set_payload_handler(handler)

    original_delay = settings.DEBOUNCE_DELAY_SECONDS
    settings.DEBOUNCE_DELAY_SECONDS = 0.05  # subchannel debounce fires almost at once
    try:
        # ── Subchannel message with actionable content ──
        msg = _make_message(
            msg_id=1,
            text="OA deadline: 10:00 PM tonight. Link: https://oa.example.com",
            date=_now(),
        )
        event = _make_event(chat_id=88888, message=msg)
        await service._handle_subchannel(event, is_edit=False)

        await asyncio.sleep(0.10)  # wait for debounce
    finally:
        settings.DEBOUNCE_DELAY_SECONDS = original_delay

    assert len(received_payloads) == 1, f"Expected 1 payload, got {len(received_payloads)}"
    source, messages = received_payloads[0]
    assert source.chat_id == 88888
    assert source.context == "subchannel"
    assert source.company_id is None and source.subchannel is None
    assert messages[0]["id"] == 1
    assert messages[0]["label"] == "[1]"

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
