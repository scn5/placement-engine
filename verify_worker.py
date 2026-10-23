"""
verify_worker.py
Phase 0.3 — Ingestion worker verification suite.

Covers the API/worker split and the worker's loops with Telegram fully mocked:
  • TELEGRAM_API_ID=0 never builds a Telegram client; app.main imports no Telethon code
  • monitored chats follow the IGNORED / deleted / environment rules (invariants 2–4)
  • ledger outcomes and retry backoff; the retry loop re-dispatches due failures
  • catch-up skips processed, buffered and in-flight messages and honours its windows
  • heartbeat writes worker_status and pings the dead-man's switch only while connected
  • exit codes for session errors, unauthorized sessions, crashes and clean stops

Every database is an in-memory SQLite instance and nothing touches the network:
the real TelegramClient class is replaced by one that fails if constructed.
Run: python verify_worker.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import ast
import asyncio
import os
import subprocess
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Patch DB engine BEFORE importing app modules (same pattern as Phase 2/4)
# ---------------------------------------------------------------------------
import app.database as _db_module

_test_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
_TestSession = async_sessionmaker(
    bind=_test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)
_db_module.engine = _test_engine
_db_module.AsyncSessionLocal = _TestSession
_db_module.install_sqlite_pragmas(_test_engine)  # enforce foreign keys like production

from alembic import command
from alembic.config import Config
from telethon import errors as tg_errors

from app import crud, worker
from app.config import settings
from app.models import (
    Company,
    MessageLedger,
    NaiveDatetimeWarning,
    RoleTrack,
    SystemConfig,
    WorkerStatus,
)
from app.telegram import client as tg_client_module
from app.telegram import ledger
from app.telegram import listener as listener_module
from app.sources import MessageSource
from app.telegram.listener import TelegramIngestionService

# Every datetime handed to the ORM must be timezone-aware.
warnings.simplefilter("error", NaiveDatetimeWarning)

ROOT = Path(__file__).resolve().parent
MAIN_FEED, TEST_FEED = -1001000000001, -1001000000002  # root feeds must be channel IDs


class _ForbiddenTelegramClient:
    """Installed in place of telethon's TelegramClient for the whole suite."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("A real TelegramClient was constructed during the tests")


tg_client_module.TelegramClient = _ForbiddenTelegramClient
settings.DEBOUNCE_DELAY_SECONDS = 0.05
listener_module._CHAT_PACING_SECONDS = 0
worker.AUTH_FAILURE_WAIT_SECONDS = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeClient:
    """Stand-in for TelegramClient: no network; serves a fixed message history."""

    def __init__(self, authorized: bool = True, history=None):
        self.authorized = authorized
        self.history = history or {}  # chat_id -> list of messages
        self.connected = False
        self.iter_calls = []
        self.handlers = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized

    async def get_input_entity(self, peer):
        if peer not in self.history:
            raise ValueError(f"Could not find the input entity for {peer}")
        return peer

    async def iter_messages(self, entity, min_id=0, limit=None):
        self.iter_calls.append((entity, min_id))
        for message in sorted(self.history.get(entity, []), key=lambda m: m.id, reverse=True):
            if message.id > min_id:
                yield message

    async def get_messages(self, entity, ids=None):
        by_id = {m.id: m for m in self.history.get(entity, [])}
        if isinstance(ids, list):
            return [by_id.get(i) for i in ids]
        return by_id.get(ids)

    def add_event_handler(self, callback, event):
        self.handlers.append(event)

    async def run_until_disconnected(self):
        await asyncio.Event().wait()  # runs until cancelled


def _msg(message_id: int, text: str, age: timedelta = timedelta(minutes=5)):
    return SimpleNamespace(
        id=message_id,
        text=text,
        date=datetime.now(timezone.utc) - age,
        media=None,
        reply_to_msg_id=None,
    )


async def _company(name, *, chat_id, env="MAIN", mode="ELIGIBLE", roles=(), active=True) -> int:
    async with _TestSession() as db:
        company = Company(
            name=name,
            normalized_name=name.lower(),
            environment_type=env,
            subchannel_id=chat_id,
            tracking_mode=mode,
            is_active=active,
        )
        db.add(company)
        await db.flush()
        for role_mode in roles:
            db.add(RoleTrack(company_id=company.id, role_name=f"{name} role", tracking_mode=role_mode))
        await db.commit()
        return company.id


async def _set_company_tracking(company_id: int, mode: str) -> None:
    """The explicit user path (PATCH /companies/{id}/tracking)."""
    async with _TestSession() as db:
        await crud.update_company_tracking(db, company_id, mode)


async def _ledger_rows(chat_id: int) -> dict:
    async with _TestSession() as db:
        rows = await db.execute(select(MessageLedger).where(MessageLedger.chat_id == chat_id))
        return {row.message_id: row for row in rows.scalars().all()}


async def _status() -> WorkerStatus:
    async with _TestSession() as db:
        return await db.get(WorkerStatus, 1)


def _migrate(connection) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_disabled_worker_builds_no_client() -> None:
    original = (settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
    try:
        for api_id, api_hash in ((0, "test-hash"), (12345, "")):
            settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH = api_id, api_hash
            with patch.object(tg_client_module, "create_telegram_client") as factory:
                assert await worker.run_worker() == worker.EXIT_OK
                factory.assert_not_called()
            try:
                tg_client_module.create_telegram_client()
            except RuntimeError as exc:
                assert "disabled" in str(exc)
            else:
                raise AssertionError("create_telegram_client() returned while Telegram is disabled")
    finally:
        settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH = original

    print("  [1] ✓ TELEGRAM_API_ID=0 or no API hash: the worker exits 0 and no Telegram client is ever built")


def test_main_is_api_only() -> None:
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = [m for m in imported if m.startswith(("telethon", "app.telegram", "app.worker", "app.ai"))]
    assert not forbidden, forbidden

    env = {
        **os.environ,
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "TELEGRAM_API_ID": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    probe = (
        "import sys, app.main; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'telethon' "
        "or m.startswith('app.telegram') or m == 'app.worker'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout

    print("  [2] ✓ app.main imports no Telethon, listener or worker code (checked statically and in a fresh interpreter)")


async def test_monitored_set_follows_tracking_rules() -> None:
    async with _TestSession() as db:
        db.add(SystemConfig(main_feed_id=MAIN_FEED, test_feed_id=TEST_FEED))
        await db.commit()

    eligible = await _company("Eligible Co", chat_id=-1101, roles=["ELIGIBLE"])
    await _company("Silent Test Co", chat_id=-1102, env="TEST", mode="SILENT_TRACK", roles=["SILENT_TRACK"])
    ignored = await _company("Ignored Co", chat_id=-1103, mode="IGNORED", roles=["IGNORED"])
    await _company("All Roles Ignored Co", chat_id=-1104, mode="SILENT_TRACK", roles=["IGNORED", "IGNORED"])
    await _company("Mixed Roles Co", chat_id=-1105, roles=["IGNORED", "ELIGIBLE"])
    await _company("Deleted Co", chat_id=-1106, roles=["ELIGIBLE"], active=False)
    await _company("No Roles Co", chat_id=-1107)
    await _company("Ignored Test Co", chat_id=-1108, env="TEST", mode="SILENT_TRACK", roles=["IGNORED"])

    both = TelegramIngestionService(FakeClient(), {"MAIN", "TEST"})
    await both.initialize()
    assert both.root_feed_ids == {MAIN_FEED: "MAIN", TEST_FEED: "TEST"}, both.root_feed_ids
    assert both.active_subchannel_ids == {-1101, -1102, -1105, -1107}, both.active_subchannel_ids

    test_only = TelegramIngestionService(FakeClient(), {"TEST"})
    await test_only.initialize()
    assert test_only.root_feed_ids == {TEST_FEED: "TEST"}, test_only.root_feed_ids
    assert test_only.active_subchannel_ids == {-1102}, test_only.active_subchannel_ids

    # Un-ignoring through the explicit user path makes the chat monitored again.
    await _set_company_tracking(ignored, "ELIGIBLE")
    assert await both.refresh_monitored() == {-1103}

    # Ignoring a company stops its channel at the router and drops its buffer.
    await both._debouncer.add_message(-1101, {"id": 1, "text": "x", "date": 0}, AsyncMock(), delay=60)
    await _set_company_tracking(eligible, "IGNORED")
    assert await both.refresh_monitored() == set()
    assert -1101 not in both.active_subchannel_ids
    assert both._debouncer.pending_ids(-1101) == set()
    await both._process_event(
        SimpleNamespace(chat_id=-1101, message=_msg(50, "OA deadline 5 PM today"), is_private=False),
        is_edit=False,
    )
    assert both._debouncer.pending_ids(-1101) == set()
    assert 50 not in await _ledger_rows(-1101)  # no ledger rows for IGNORED channels

    print("  [3] ✓ Monitored chats: ingested environments only; IGNORED (company or every role) and deleted companies excluded; un-ignore re-adds")


async def test_new_chats_get_bounded_catch_up() -> None:
    chat = -1109
    company_id = await _company("Returning Co", chat_id=chat, mode="IGNORED", roles=["IGNORED"])
    history = [
        _msg(1, "processed before the ignore", age=timedelta(hours=40)),
        _msg(2, "posted while ignored, 30 h ago", age=timedelta(hours=30)),
        _msg(3, "posted while ignored, 2 h ago", age=timedelta(hours=2)),
        _msg(4, "posted while ignored, 10 min ago", age=timedelta(minutes=10)),
    ]
    client = FakeClient(history={chat: history})
    service = TelegramIngestionService(client, {"MAIN"})
    await service.initialize()
    await ledger.record(chat, [1], ledger.DONE)

    routed = []

    async def capture(chat_id, message, is_edit=False):
        routed.append(message.id)

    service._route_message = capture
    await _set_company_tracking(company_id, "ELIGIBLE")
    await worker._refresh_and_catch_up(service)
    assert routed == [3, 4], routed  # 24 h window: the 30 h-old message is not replayed
    assert client.iter_calls[-1] == (chat, 0)  # cursor 1 minus the lookback, floored at 0

    # A chat with no history is bootstrapped from 24 h; one with history from CATCHUP_MAX_AGE_HOURS.
    fresh_chat, known_chat = -1110, -1111
    client.history[fresh_chat] = [_msg(7, "old", age=timedelta(hours=30)), _msg(8, "recent", age=timedelta(hours=1))]
    client.history[known_chat] = [
        _msg(20, "seen", age=timedelta(hours=31)),
        _msg(21, "posted 30 h ago", age=timedelta(hours=30)),
    ]
    await ledger.record(known_chat, [20], ledger.DONE)
    service.active_subchannel_ids.update({fresh_chat, known_chat})
    routed.clear()
    await service.catch_up([fresh_chat, known_chat])
    assert routed == [8, 21], routed

    print("  [4] ✓ Un-ignored and new chats get a bounded 24 h catch-up; chats with history use the 72 h window")


async def test_ledger_outcomes_and_backoff() -> None:
    chat = -1201
    service = TelegramIngestionService(FakeClient(), {"MAIN"})

    async def ok_handler(source, messages):
        return None

    async def failing_handler(source, messages):
        raise RuntimeError("LLM unavailable\n[SQL: INSERT ...] [parameters: ('secret message text',)]")

    await service.set_payload_handler(ok_handler)
    await service._emit(MessageSource(chat, "subchannel"), [{"id": 1}, {"id": 2}])
    rows = await _ledger_rows(chat)
    assert rows[1].status == rows[2].status == ledger.DONE

    await service.set_payload_handler(failing_handler)
    before = datetime.now(timezone.utc)
    await service._emit(MessageSource(chat, "subchannel"), [{"id": 3}])
    row = (await _ledger_rows(chat))[3]
    assert row.status == ledger.FAILED and row.attempts == 1
    assert timedelta(seconds=55) <= row.next_attempt_at - before <= timedelta(seconds=65)
    assert row.reason == "RuntimeError: LLM unavailable", row.reason  # first line only, no SQL parameters
    assert service.last_error == "RuntimeError: LLM unavailable"

    for attempts, delay in enumerate(
        [timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=2)], start=2
    ):
        now = datetime.now(timezone.utc)
        await ledger.record_failure(chat, [3], "still failing")
        row = (await _ledger_rows(chat))[3]
        assert row.attempts == attempts
        assert abs((row.next_attempt_at - now) - delay) < timedelta(seconds=5), (attempts, row.next_attempt_at - now)
    await ledger.record_failure(chat, [3], "final failure")
    row = (await _ledger_rows(chat))[3]
    assert row.attempts == ledger.MAX_ATTEMPTS == 6 and row.next_attempt_at is None  # exhausted

    await ledger.record(chat, [3], ledger.DONE)
    row = (await _ledger_rows(chat))[3]
    assert row.status == ledger.DONE and row.attempts == 0 and row.next_attempt_at is None

    service.active_subchannel_ids.add(chat)
    await service._handle_subchannel(SimpleNamespace(chat_id=chat, message=_msg(4, "  \n ")), is_edit=False)
    row = (await _ledger_rows(chat))[4]
    assert row.status == ledger.SKIPPED and row.reason == "empty"

    print("  [5] ✓ Ledger: DONE/FAILED/SKIPPED outcomes; backoff 1 m → 5 m → 30 m → 2 h, given up after 6 attempts; errors stored without SQL parameters")


async def test_retry_loop_redispatches_due_failures() -> None:
    chat, unmonitored = -1301, -1302
    client = FakeClient(history={chat: [_msg(10, "OA link https://oa.example.com, deadline 5 PM")]})
    service = TelegramIngestionService(client, {"MAIN"})
    service.active_subchannel_ids.add(chat)
    received = []

    async def handler(source, messages):
        received.append((source.chat_id, source.context, [m["id"] for m in messages]))

    await service.set_payload_handler(handler)
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with _TestSession() as db:
        for chat_id, message_id in ((chat, 10), (chat, 11), (unmonitored, 20)):
            db.add(MessageLedger(chat_id=chat_id, message_id=message_id, status=ledger.FAILED, attempts=1, next_attempt_at=due))
        await db.commit()

    assert await service.retry_failed() == 1
    await asyncio.sleep(settings.DEBOUNCE_DELAY_SECONDS * 3)  # the retried message is debounced first
    assert received == [(chat, "subchannel", [10])], received
    rows = await _ledger_rows(chat)
    assert rows[10].status == ledger.DONE
    assert rows[11].status == ledger.SKIPPED and rows[11].reason == "message_deleted"
    left = (await _ledger_rows(unmonitored))[20]
    assert left.status == ledger.FAILED and left.attempts == 1  # unmonitored chat left untouched

    print("  [6] ✓ Retry loop: due failures are re-fetched and routed again; deleted messages are skipped; unmonitored chats are left alone")


async def test_catch_up_skips_processed_buffered_and_inflight() -> None:
    chat, uncached = -1401, -1402
    client = FakeClient(history={chat: [_msg(i, f"message {i}") for i in range(1, 7)]})
    service = TelegramIngestionService(client, {"MAIN"})
    service.active_subchannel_ids.update({chat, uncached})
    await ledger.record(chat, [2, 5], ledger.DONE)  # processed; cursor = 5
    await service._debouncer.add_message(chat, {"id": 3, "text": "x", "date": 0}, AsyncMock(), delay=60)
    service._inflight[chat] = {4}  # being dispatched right now

    routed = []

    async def capture(chat_id, message, is_edit=False):
        routed.append(message.id)

    service._route_message = capture
    assert await service.catch_up([chat, uncached]) == 2
    assert routed == [1, 6], routed  # oldest first; 2/5 known, 3 buffered, 4 in flight
    assert client.iter_calls == [(chat, 0)]  # the uncached chat is skipped without an RPC
    assert service.last_catchup_at is not None
    await service._debouncer.discard(chat)

    print("  [7] ✓ Catch-up re-scans behind the cursor, skips processed, buffered and in-flight messages, and skips chats not yet in the session cache")


async def test_heartbeat_and_deadman() -> None:
    client = FakeClient()
    client.connected = True
    service = TelegramIngestionService(client, {"MAIN"})
    service.last_update_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    service.last_dispatch_ok_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    service.last_error = "RuntimeError: boom"
    reporter = worker.StatusReporter()

    original_url = settings.DEADMAN_URL
    settings.DEADMAN_URL = "https://deadman.invalid/ping"
    try:
        with patch.object(worker.httpx.AsyncClient, "get", new=AsyncMock()) as ping:
            await reporter.beat(client, service)
            assert (await _status()).telegram_state == "CONNECTED"
            await reporter.beat(client, service)  # within 5 minutes: no second ping
            client.connected = False
            await reporter.beat(client, service)  # disconnected: never pings
            assert ping.await_count == 1, ping.await_count
    finally:
        settings.DEADMAN_URL = original_url

    status = await _status()
    assert status.telegram_state == "DISCONNECTED"
    assert status.heartbeat_at.utcoffset() == timedelta(0)
    assert status.last_update_at == service.last_update_at
    assert status.last_llm_ok_at == service.last_dispatch_ok_at
    assert status.last_error == "RuntimeError: boom"

    print("  [8] ✓ Heartbeat writes worker_status; the dead-man's switch is pinged at most every 5 min and only while connected")


async def test_exit_codes() -> None:
    reporter = worker.StatusReporter()

    async def raises(exc):
        raise exc

    async def returns():
        return None

    cases = [
        (raises(tg_errors.AuthKeyDuplicatedError(request=None)), worker.EXIT_SESSION, "SESSION_INVALID"),
        (raises(ConnectionError("network down")), worker.EXIT_RESTART, "DISCONNECTED"),
        (returns(), worker.EXIT_RESTART, "DISCONNECTED"),
    ]
    for coro, expected_code, expected_state in cases:
        task = asyncio.create_task(coro)
        await asyncio.wait({task})
        assert await worker._listener_exit_code(task, reporter) == expected_code
        assert (await _status()).telegram_state == expected_state

    original = (settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
    settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH = 12345, "test-hash"
    try:
        fake = FakeClient(authorized=False)
        with patch.object(tg_client_module, "create_telegram_client", return_value=fake):
            assert await worker.run_worker() == worker.EXIT_SESSION
        assert not fake.connected
        assert (await _status()).telegram_state == "UNAUTHORIZED"

        fake = FakeClient(authorized=True)

        def stop_soon(stop):
            asyncio.get_running_loop().call_later(0.3, stop.set)

        with patch.object(tg_client_module, "create_telegram_client", return_value=fake), \
                patch.object(worker, "_install_stop_handlers", side_effect=stop_soon):
            assert await worker.run_worker() == worker.EXIT_OK
        assert fake.handlers and not fake.connected
        assert (await _status()).telegram_state == "STOPPED"
    finally:
        settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH = original

    print("  [9] ✓ Exit codes: session errors and unauthorized sessions → 2, crashes/disconnects → 1, clean stop → 0 (client always disconnected)")


async def test_dispatch_bridge_propagates_errors() -> None:
    with patch.object(worker.dispatcher, "process_and_dispatch", new=AsyncMock(side_effect=RuntimeError("boom"))):
        try:
            await worker.dispatch_to_ai(MessageSource(-1, "subchannel"), [{"id": 1, "text": "x"}])
        except RuntimeError:
            pass
        else:
            raise AssertionError("dispatch_to_ai swallowed a dispatcher error")
    with patch.object(worker.dispatcher, "process_and_dispatch", new=AsyncMock()) as dispatch:
        await worker.dispatch_to_ai(MessageSource(-1, "main_feed", company_id=7), [{"id": 1, "text": "x"}])
        kwargs = dispatch.await_args.kwargs
        assert kwargs["chat_id"] == -1 and kwargs["context_header"] == "main_feed"
        assert kwargs["company_id"] == 7 and kwargs["subchannel"] is None
        assert kwargs["db"] is not None

    print("  [10] ✓ The dispatcher bridge lets errors propagate, so failed batches are recorded and retried")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    # Schema via the real migrations, so the worker's startup schema check passes.
    async with _test_engine.begin() as conn:
        await conn.run_sync(_migrate)

    print("\nRunning Phase 0.3 ingestion worker verification suite…\n")

    await test_disabled_worker_builds_no_client()
    test_main_is_api_only()
    await test_monitored_set_follows_tracking_rules()
    await test_new_chats_get_bounded_catch_up()
    await test_ledger_outcomes_and_backoff()
    await test_retry_loop_redispatches_due_failures()
    await test_catch_up_skips_processed_buffered_and_inflight()
    await test_heartbeat_and_deadman()
    await test_exit_codes()
    await test_dispatch_bridge_propagates_errors()

    await _test_engine.dispose()
    print("\n✅ Phase 0.3 Ingestion Worker Verified Successfully!")


if __name__ == "__main__":
    asyncio.run(main())
