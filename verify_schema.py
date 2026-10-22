"""
verify_schema.py
Phase 0.2 — Schema foundation verification suite.

Covers the Alembic baseline (upgrade → downgrade → upgrade → drift check), the
startup schema check, and the storage invariants: UTC datetimes on SQLite,
company names unique per environment, enforced foreign keys, and the
message_ledger / worker_status constraints.

Every database in this suite is an in-memory SQLite instance.
Run: python verify_schema.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Patch DB engine BEFORE importing app modules (same pattern as Phase 2/4)
# ---------------------------------------------------------------------------
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

from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from app.database import assert_schema_current
from app.models import (
    Base,
    Company,
    MessageLedger,
    NaiveDatetimeWarning,
    TimelineEvent,
    WorkerStatus,
    as_utc,
)
from app.schemas import TimelineEventCreate

# Every datetime handed to the ORM must be timezone-aware.
warnings.simplefilter("error", NaiveDatetimeWarning)

_IST = timezone(timedelta(hours=5, minutes=30), name="IST")
_ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"
_EXPECTED_TABLES = {
    "alembic_version",
    "companies",
    "message_ledger",
    "role_tracks",
    "system_config",
    "timeline_events",
    "worker_status",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _memory_engine():
    """A private in-memory database; StaticPool keeps it on one connection."""
    return create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)


def _alembic_config(connection) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    return cfg


def _migration_round_trip(connection) -> None:
    cfg = _alembic_config(connection)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    command.check(cfg)  # raises if the models and the migrations disagree


async def _add_company(session: AsyncSession, name: str, env: str = "MAIN") -> Company:
    company = Company(name=name, normalized_name=name.lower(), environment_type=env)
    session.add(company)
    await session.flush()
    return company


async def _expect_integrity_error(session: AsyncSession, label: str) -> None:
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return
    raise AssertionError(f"Expected IntegrityError for: {label}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_migration_round_trip() -> None:
    engine = _memory_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_migration_round_trip)
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        assert tables == _EXPECTED_TABLES, tables
        await assert_schema_current(engine)  # a migrated database passes the startup check
    finally:
        await engine.dispose()

    blank = _memory_engine()
    try:
        await assert_schema_current(blank)
    except RuntimeError as exc:
        assert "alembic upgrade head" in str(exc), exc
    else:
        raise AssertionError("assert_schema_current accepted an unmigrated database")
    finally:
        await blank.dispose()

    print("  [1] ✓ Alembic baseline: upgrade → downgrade → upgrade with no model drift; startup check passes only at head")


async def test_utc_round_trip() -> None:
    deadline_ist = datetime.now(_IST).replace(microsecond=0) + timedelta(hours=3)

    async with _TestSession() as session:
        company = await _add_company(session, "UTC Roundtrip Corp")
        event = TimelineEvent(
            company_id=company.id,
            event_type="FORM_SUBMISSION",
            title="Registration form",
            start_time=deadline_ist,
            end_time=deadline_ist,
        )
        session.add(event)
        await session.commit()
        event_id = event.id

    async with _TestSession() as session:
        raw = (
            await session.execute(
                text("SELECT end_time FROM timeline_events WHERE id = :id"), {"id": event_id}
            )
        ).scalar_one()
        stored = (
            await session.execute(select(TimelineEvent).where(TimelineEvent.id == event_id))
        ).scalar_one()

    assert stored.end_time.utcoffset() == timedelta(0), stored.end_time
    assert stored.end_time == deadline_ist, (stored.end_time, deadline_ist)  # same instant
    utc_wall_clock = deadline_ist.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    assert str(raw).startswith(utc_wall_clock), (raw, utc_wall_clock)

    print("  [2] ✓ IST-aware datetime stored as UTC wall-clock and read back as the same aware UTC instant")


async def test_display_state_after_round_trip() -> None:
    now_ist = datetime.now(_IST)
    deadlines = {
        "just_missed": now_ist - timedelta(minutes=1),
        "still_pending": now_ist + timedelta(minutes=1),
    }

    event_ids = {}
    async with _TestSession() as session:
        company = await _add_company(session, "Display State Corp")
        for label, deadline in deadlines.items():
            event = TimelineEvent(
                company_id=company.id,
                event_type="FORM_SUBMISSION",
                title=label,
                start_time=deadline,
                end_time=deadline,
            )
            session.add(event)
            await session.flush()
            event_ids[label] = event.id
        await session.commit()

    async with _TestSession() as session:
        states = {
            label: (await session.get(TimelineEvent, event_id)).display_state
            for label, event_id in event_ids.items()
        }

    assert states == {"just_missed": "MISSED", "still_pending": "PENDING"}, states
    print("  [3] ✓ An IST deadline one minute ago reads back MISSED (the old storage kept it PENDING for 5.5 h)")


async def test_naive_datetime_policy() -> None:
    naive = datetime(2026, 10, 23, 12, 0)

    # Strict mode (set by the verify suites): a naive datetime is an error.
    try:
        as_utc(naive)
    except NaiveDatetimeWarning:
        pass
    else:
        raise AssertionError("A naive datetime was accepted silently")

    # Default mode: assumed to be UTC, with a warning.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", NaiveDatetimeWarning)
        converted = as_utc(naive)
    assert converted == naive.replace(tzinfo=timezone.utc)
    assert any(issubclass(w.category, NaiveDatetimeWarning) for w in caught)

    # API input must carry an offset.
    try:
        TimelineEventCreate(company_id=1, event_type="CUSTOM", title="x", end_time="2026-10-23T17:00:00")
    except ValidationError:
        pass
    else:
        raise AssertionError("TimelineEventCreate accepted an end_time without an offset")
    accepted = TimelineEventCreate(
        company_id=1, event_type="CUSTOM", title="x", end_time="2026-10-23T17:00:00+05:30"
    )
    assert accepted.end_time.utcoffset() == timedelta(hours=5, minutes=30)

    print("  [4] ✓ Naive datetimes: an error under the suites' strict filter, UTC + warning otherwise; API input needs an offset")


async def test_company_names_unique_per_environment() -> None:
    async with _TestSession() as session:
        await _add_company(session, "Twin Corp", env="MAIN")
        await _add_company(session, "Twin Corp", env="TEST")  # same name, other environment
        await session.commit()

    async with _TestSession() as session:
        session.add(Company(name="Twin Corp", normalized_name="twin corp", environment_type="MAIN"))
        await _expect_integrity_error(session, "duplicate company name within MAIN")

    print("  [5] ✓ Company names are unique per environment (MAIN and TEST may share a name)")


async def test_foreign_keys_enforced() -> None:
    async with _TestSession() as session:
        assert (await session.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
        session.add(
            TimelineEvent(
                company_id=999_999,
                event_type="CUSTOM",
                title="Orphan",
                end_time=datetime.now(timezone.utc),
            )
        )
        await _expect_integrity_error(session, "event for a non-existent company")

    print("  [6] ✓ SQLite enforces foreign keys (orphan events are rejected, as on PostgreSQL)")


async def test_ledger_and_status_constraints() -> None:
    async with _TestSession() as session:
        session.add(MessageLedger(chat_id=-1001, message_id=42, status="DONE"))
        session.add(WorkerStatus(telegram_state="DISABLED"))
        await session.commit()

    # Fresh sessions, so the database (not the identity map) rejects the rows.
    async with _TestSession() as session:
        session.add(MessageLedger(chat_id=-1001, message_id=42, status="FAILED"))
        await _expect_integrity_error(session, "second ledger row for the same message")

    async with _TestSession() as session:
        session.add(MessageLedger(chat_id=-1001, message_id=43, status="MAYBE"))
        await _expect_integrity_error(session, "unknown ledger status")

    async with _TestSession() as session:
        session.add(WorkerStatus(id=2))
        await _expect_integrity_error(session, "second worker_status row")

    print("  [7] ✓ message_ledger keeps one row per message with known statuses; worker_status is a single row")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    # Bootstrap schema in the in-memory database
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("\nRunning Phase 0.2 schema verification suite…\n")

    await test_migration_round_trip()
    await test_utc_round_trip()
    await test_display_state_after_round_trip()
    await test_naive_datetime_policy()
    await test_company_names_unique_per_environment()
    await test_foreign_keys_enforced()
    await test_ledger_and_status_constraints()

    await _test_engine.dispose()
    print("\n✅ Phase 0.2 Schema Foundation Verified Successfully!")


if __name__ == "__main__":
    asyncio.run(main())
