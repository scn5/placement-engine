"""
app/notifier.py
Push notification scheduler (Phase 1.4, step 2).

Runs as a background loop in the API process, polling every 60 seconds:
1. Deadline reminders (MASTER_SPEC §6.3):
   - Events where user_status = 'PENDING', not deleted
   - Linked role track is ELIGIBLE, company is active and not IGNORED
   - Target time (start_time, or end_time when start_time is null) is
     within 2 hours or 30 minutes.
   - Dispatches Expo push and records (event_id, window) in sent_notifications
     so duplicate alarms never fire across API restarts. Completing a task
     removes it from the PENDING query automatically.
2. Triage proposals (MASTER_SPEC §2, §6.4):
   - Untriaged role tracks (is_triaged = False) on active, non-IGNORED companies.
   - Dispatches one-time "New Drive in Triage" push and records in sent_notifications.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import database, models, push
from app.config import settings

logger = logging.getLogger(__name__)

WINDOW_30M = "30m"
WINDOW_2H = "2h"
TYPE_DEADLINE_REMINDER = "DEADLINE_REMINDER"
TYPE_TRIAGE_PROPOSAL = "TRIAGE_PROPOSAL"


async def check_deadline_reminders(
    db: AsyncSession, now: Optional[datetime] = None
) -> int:
    """Scan eligible pending timeline events and send 2h / 30m push reminders."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    stmt = (
        select(models.TimelineEvent, models.Company.name.label("company_name"))
        .join(models.Company, models.TimelineEvent.company_id == models.Company.id)
        .join(models.RoleTrack, models.TimelineEvent.role_track_id == models.RoleTrack.id)
        .where(
            models.TimelineEvent.user_status == "PENDING",
            models.TimelineEvent.is_deleted == False,
            models.RoleTrack.tracking_mode == "ELIGIBLE",
            models.Company.is_active == True,
            models.Company.tracking_mode != "IGNORED",
        )
    )
    rows = (await db.execute(stmt)).all()
    sent_count = 0

    for event, company_name in rows:
        target_time = event.start_time if event.start_time is not None else event.end_time
        if target_time is None:
            continue
        if target_time.tzinfo is None:
            target_time = target_time.replace(tzinfo=timezone.utc)

        if target_time <= now:
            continue  # already started or deadline passed

        time_left = target_time - now
        if time_left <= timedelta(minutes=30):
            target_window = WINDOW_30M
        elif time_left <= timedelta(hours=2):
            target_window = WINDOW_2H
        else:
            continue  # further than 2 hours out

        # Check if already sent for this window
        already_sent = (
            await db.execute(
                select(models.SentNotification).where(
                    models.SentNotification.event_id == event.id,
                    models.SentNotification.window == target_window,
                )
            )
        ).scalars().first()
        if already_sent is not None:
            continue

        title = f"⏰ {company_name}"
        body = f"{event.event_type} — {event.title} in {target_window}"
        data = {
            "event_id": event.id,
            "type": TYPE_DEADLINE_REMINDER,
            "window": target_window,
        }

        result = await push.push_to_all(db, title=title, body=body, data=data)
        if result is not None and result.sent > 0:
            db.add(
                models.SentNotification(
                    event_id=event.id,
                    window=target_window,
                    notification_type=TYPE_DEADLINE_REMINDER,
                    sent_at=now,
                )
            )
            if target_window == WINDOW_30M:
                # Also ensure 2h is recorded so a later 2h reminder is never fired
                has_2h = (
                    await db.execute(
                        select(models.SentNotification).where(
                            models.SentNotification.event_id == event.id,
                            models.SentNotification.window == WINDOW_2H,
                        )
                    )
                ).scalars().first()
                if has_2h is None:
                    db.add(
                        models.SentNotification(
                            event_id=event.id,
                            window=WINDOW_2H,
                            notification_type=TYPE_DEADLINE_REMINDER,
                            sent_at=now,
                        )
                    )
            await db.commit()
            sent_count += 1
            logger.info("Sent %s reminder for event #%d (%s - %s)", target_window, event.id, company_name, event.title)

    return sent_count


async def check_triage_proposals(
    db: AsyncSession, now: Optional[datetime] = None
) -> int:
    """Scan untriaged role tracks and send one-time Triage push proposals."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    stmt = (
        select(models.RoleTrack, models.Company.name.label("company_name"))
        .join(models.Company, models.RoleTrack.company_id == models.Company.id)
        .where(
            models.RoleTrack.is_triaged == False,
            models.Company.is_active == True,
            models.Company.tracking_mode != "IGNORED",
        )
    )
    rows = (await db.execute(stmt)).all()
    sent_count = 0

    for track, company_name in rows:
        already_sent = (
            await db.execute(
                select(models.SentNotification).where(
                    models.SentNotification.role_track_id == track.id,
                    models.SentNotification.notification_type == TYPE_TRIAGE_PROPOSAL,
                )
            )
        ).scalars().first()
        if already_sent is not None:
            continue

        title = "🎯 New Drive in Triage"
        role = track.role_name.strip() if track.role_name else ""
        if role and role != "Default Role":
            body = f"{company_name} — {role} awaiting review"
        else:
            body = f"{company_name} awaiting review"
        data = {"type": "triage"}

        result = await push.push_to_all(db, title=title, body=body, data=data)
        if result is not None and result.sent > 0:
            db.add(
                models.SentNotification(
                    role_track_id=track.id,
                    notification_type=TYPE_TRIAGE_PROPOSAL,
                    sent_at=now,
                )
            )
            await db.commit()
            sent_count += 1
            logger.info("Sent triage push for role track #%d (%s - %s)", track.id, company_name, track.role_name)

    return sent_count


async def poll_once(
    db: AsyncSession, now: Optional[datetime] = None
) -> dict:
    """Execute one polling iteration of deadline reminders and triage proposals."""
    reminders = await check_deadline_reminders(db, now=now)
    triage = await check_triage_proposals(db, now=now)
    return {"reminders": reminders, "triage": triage}


async def start_notifier() -> None:
    """Run the push notification polling scheduler indefinitely until cancelled."""
    logger.info(
        "Push notification scheduler loop active (polling every %.0fs).",
        settings.NOTIFIER_POLL_SECONDS,
    )
    while True:
        try:
            async with database.AsyncSessionLocal() as db:
                result = await poll_once(db)
                if result["reminders"] or result["triage"]:
                    logger.info(
                        "Push scheduler dispatched: %d reminder(s), %d triage proposal(s).",
                        result["reminders"],
                        result["triage"],
                    )
        except asyncio.CancelledError:
            logger.info("Push notification scheduler loop cancelled.")
            break
        except Exception as exc:
            logger.exception("Push notification scheduler iteration failed: %s", exc)

        try:
            await asyncio.sleep(settings.NOTIFIER_POLL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Push notification scheduler loop stopped.")
            break
