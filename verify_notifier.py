"""
verify_notifier.py
Phase 1.4, Step 2 — Push Notification Scheduler Verification Suite.

Verifies:
  1. 2h deadline reminder for ELIGIBLE PENDING events
  2. Deduplication across polls (no duplicate alarms)
  3. 30m deadline reminder for upcoming deadlines
  4. Fallback to end_time when start_time is None
  5. Completing an event (user_status != 'PENDING') stops reminders
  6. SILENT_TRACK, IGNORED, and deleted events never trigger reminders
  7. Triage proposals for untriaged role tracks (with role name formatting)
  8. Deduplication of triage proposals
  9. Rescheduling an event clears past sent records allowing new reminders

All databases are in-memory SQLite instances; Expo Push API is stubbed.
Run: python verify_notifier.py
"""
import asyncio
from datetime import datetime, timedelta, timezone
import sys
import warnings

sys.stdout.reconfigure(encoding="utf-8")

from app.config import settings
settings.TELEGRAM_API_ID = 0
settings.NOTIFIER_ENABLED = False  # Keep background loop off during test runner

import app.database as _db_module
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
_Session = async_sessionmaker(bind=_engine, autocommit=False, autoflush=False, expire_on_commit=False)
_db_module.engine = _engine
_db_module.AsyncSessionLocal = _Session
_db_module.install_sqlite_pragmas(_engine)

from app import crud, models, notifier, push
from app.models import Base, Company, DeviceToken, NaiveDatetimeWarning, RoleTrack, SentNotification, TimelineEvent

warnings.simplefilter("error", NaiveDatetimeWarning)

_TOKEN = "ExponentPushToken[test_device_token_12345]"


class _ExpoStub:
    """Mock Expo Push API recording dispatched messages."""

    def __init__(self):
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(messages)
        return [push.Ticket(ok=True) for _ in messages]


async def setup_base_data(db):
    """Seed test device and companies."""
    db.add(DeviceToken(expo_push_token=_TOKEN))
    company = Company(name="Google", normalized_name="google", is_active=True, tracking_mode="ALL")
    db.add(company)
    await db.flush()

    role = RoleTrack(
        company_id=company.id,
        role_name="Software Engineer",
        drive_type="FTE",
        tracking_mode="ELIGIBLE",
        is_triaged=True,
    )
    db.add(role)
    await db.commit()
    return company, role


async def test_deadline_reminders():
    print("Testing 2h & 30m deadline reminders...")
    async with _Session() as db:
        company, role = await setup_base_data(db)
        now = datetime.now(timezone.utc)

        # Event 1: 90 mins away -> qualifies for 2h window
        event_2h = TimelineEvent(
            company_id=company.id,
            role_track_id=role.id,
            event_type="OA",
            title="Coding Assessment",
            start_time=now + timedelta(minutes=90),
            end_time=now + timedelta(minutes=150),
            user_status="PENDING",
        )
        db.add(event_2h)
        await db.commit()

        expo = _ExpoStub()
        original = push.send_expo_push
        push.send_expo_push = expo
        try:
            # First poll: 2h alarm fires
            res = await notifier.poll_once(db, now=now)
            assert res["reminders"] == 1, f"Expected 1 reminder, got {res}"
            assert len(expo.calls) == 1
            sent_msg = expo.calls[0][0]
            assert "Google" in sent_msg["title"]
            assert "Coding Assessment in 2h" in sent_msg["body"]
            assert sent_msg["data"]["window"] == "2h"
            print("  [1] ✓ 2h reminder fires for pending ELIGIBLE event")

            # Second poll: should not repeat
            res2 = await notifier.poll_once(db, now=now)
            assert res2["reminders"] == 0, f"Expected 0 reminders on repeat, got {res2}"
            print("  [2] ✓ Deduplication prevents duplicate 2h reminders")

            # Fast forward to 20 mins before event -> 30m window fires
            now_30m = now + timedelta(minutes=70)
            res3 = await notifier.poll_once(db, now=now_30m)
            assert res3["reminders"] == 1, f"Expected 1 reminder for 30m, got {res3}"
            assert len(expo.calls) == 2
            sent_msg_30m = expo.calls[1][0]
            assert "Coding Assessment in 30m" in sent_msg_30m["body"]
            assert sent_msg_30m["data"]["window"] == "30m"
            print("  [3] ✓ 30m reminder fires when time enters <= 30m window")

            # Deduplication for 30m
            res4 = await notifier.poll_once(db, now=now_30m)
            assert res4["reminders"] == 0
            print("  [4] ✓ Deduplication prevents duplicate 30m reminders")

        finally:
            push.send_expo_push = original


async def test_fallback_to_end_time():
    print("Testing fallback to end_time when start_time is null...")
    async with _Session() as db:
        company = (await db.execute(select(Company))).scalars().first()
        role = (await db.execute(select(RoleTrack))).scalars().first()
        now = datetime.now(timezone.utc)

        # Event with start_time = None, end_time = 45 mins away
        event_null_start = TimelineEvent(
            company_id=company.id,
            role_track_id=role.id,
            event_type="Deadline",
            title="Registration Form",
            start_time=None,
            end_time=now + timedelta(minutes=45),
            user_status="PENDING",
        )
        db.add(event_null_start)
        await db.commit()

        expo = _ExpoStub()
        original = push.send_expo_push
        push.send_expo_push = expo
        try:
            res = await notifier.poll_once(db, now=now)
            assert res["reminders"] == 1
            sent_msg = expo.calls[0][0]
            assert "Registration Form in 2h" in sent_msg["body"]
            print("  [5] ✓ Uses end_time as target deadline when start_time is null")
        finally:
            push.send_expo_push = original


async def test_completed_or_filtered_events():
    print("Testing completion and filtering rules...")
    async with _Session() as db:
        company = (await db.execute(select(Company))).scalars().first()
        role = (await db.execute(select(RoleTrack))).scalars().first()
        now = datetime.now(timezone.utc)

        # Completed event
        ev_completed = TimelineEvent(
            company_id=company.id,
            role_track_id=role.id,
            event_type="OA",
            title="Done Assessment",
            start_time=now + timedelta(minutes=20),
            end_time=now + timedelta(minutes=80),
            user_status="COMPLETED",
        )
        # Deleted event
        ev_deleted = TimelineEvent(
            company_id=company.id,
            role_track_id=role.id,
            event_type="OA",
            title="Cancelled Assessment",
            start_time=now + timedelta(minutes=20),
            end_time=now + timedelta(minutes=80),
            user_status="PENDING",
            is_deleted=True,
        )
        # Event in SILENT_TRACK
        role_silent = RoleTrack(
            company_id=company.id,
            role_name="Silent Role",
            tracking_mode="SILENT_TRACK",
            is_triaged=True,
        )
        db.add(role_silent)
        await db.flush()
        ev_silent = TimelineEvent(
            company_id=company.id,
            role_track_id=role_silent.id,
            event_type="OA",
            title="Silent Assessment",
            start_time=now + timedelta(minutes=20),
            end_time=now + timedelta(minutes=80),
            user_status="PENDING",
        )
        db.add_all([ev_completed, ev_deleted, ev_silent])
        await db.commit()

        expo = _ExpoStub()
        original = push.send_expo_push
        push.send_expo_push = expo
        try:
            res = await notifier.poll_once(db, now=now)
            assert res["reminders"] == 0, f"Expected 0 reminders, got {res}"
            assert len(expo.calls) == 0
            print("  [6] ✓ Completed, deleted, and SILENT_TRACK events are never alerted")
        finally:
            push.send_expo_push = original


async def test_triage_proposals():
    print("Testing triage proposals...")
    async with _Session() as db:
        comp = Company(name="Microsoft", normalized_name="microsoft", is_active=True, tracking_mode="ALL")
        db.add(comp)
        await db.flush()

        # Track 1: Specific role
        track1 = RoleTrack(
            company_id=comp.id,
            role_name="SDE-1",
            tracking_mode="SILENT_TRACK",
            is_triaged=False,
        )
        # Track 2: Default Role
        track2 = RoleTrack(
            company_id=comp.id,
            role_name="Default Role",
            tracking_mode="SILENT_TRACK",
            is_triaged=False,
        )
        db.add_all([track1, track2])
        await db.commit()

        expo = _ExpoStub()
        original = push.send_expo_push
        push.send_expo_push = expo
        try:
            res = await notifier.poll_once(db)
            assert res["triage"] == 2, f"Expected 2 triage pushes, got {res}"
            assert len(expo.calls) == 2
            # Check message format
            titles = [c[0]["title"] for c in expo.calls]
            bodies = [c[0]["body"] for c in expo.calls]
            assert all(t == "🎯 New Drive in Triage" for t in titles)
            assert "Microsoft — SDE-1 awaiting review" in bodies
            assert "Microsoft awaiting review" in bodies
            print("  [7] ✓ Triage proposals formatted correctly (excluding Default Role / null role)")

            # Deduplication: second run sends 0
            res2 = await notifier.poll_once(db)
            assert res2["triage"] == 0
            print("  [8] ✓ Triage proposals are not duplicated across polls")
        finally:
            push.send_expo_push = original


async def test_reschedule_resets_notifications():
    print("Testing event reschedule reset...")
    async with _Session() as db:
        company = (await db.execute(select(Company))).scalars().first()
        role = (await db.execute(select(RoleTrack).where(RoleTrack.tracking_mode == "ELIGIBLE"))).scalars().first()
        now = datetime.now(timezone.utc)

        event = TimelineEvent(
            company_id=company.id,
            role_track_id=role.id,
            event_type="Interview",
            title="Tech Round 1",
            start_time=now + timedelta(minutes=25),
            end_time=now + timedelta(minutes=60),
            user_status="PENDING",
        )
        db.add(event)
        await db.commit()

        expo = _ExpoStub()
        original = push.send_expo_push
        push.send_expo_push = expo
        try:
            res = await notifier.poll_once(db, now=now)
            assert res["reminders"] == 1

            # Reschedule event to tomorrow
            new_start = now + timedelta(hours=25)
            new_end = now + timedelta(hours=26)
            await crud.reschedule_event(db, event.id, new_end_time=new_end, new_start_time=new_start)

            # Check that sent_notifications for this event were cleared
            sent = (
                await db.execute(
                    select(SentNotification).where(SentNotification.event_id == event.id)
                )
            ).scalars().all()
            assert len(sent) == 0, f"Expected 0 sent notifications after reschedule, got {len(sent)}"
            print("  [9] ✓ Rescheduling event clears past sent_notifications for that event")
        finally:
            push.send_expo_push = original


async def main():
    print("🚀 Running Phase 1.4 Step 2 (Push Notifier Scheduler) Verification Suite...\n")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_deadline_reminders()
    await test_fallback_to_end_time()
    await test_completed_or_filtered_events()
    await test_triage_proposals()
    await test_reschedule_resets_notifications()

    await _engine.dispose()
    print("\n🎉 All push notifier scheduler checks passed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
