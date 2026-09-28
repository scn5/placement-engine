from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import false, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import models, schemas


# ---------------------------------------------------------------------------
# Company Operations
# ---------------------------------------------------------------------------


async def create_company(
    db: AsyncSession, company_in: schemas.CompanyCreate
) -> models.Company:
    """Create a new company, normalizing its name to lowercase + stripped."""
    normalized = company_in.name.strip().lower()
    db_company = models.Company(
        name=company_in.name.strip(),
        normalized_name=normalized,
        tracking_mode=company_in.tracking_mode,
        is_custom=company_in.is_custom,
    )
    db.add(db_company)
    await db.commit()
    await db.refresh(db_company)
    return db_company


async def get_companies(
    db: AsyncSession, tracking_mode: Optional[str] = None
) -> List[models.Company]:
    """Retrieve companies ordered by name, eagerly loading role_tracks.
    Optionally filter by tracking_mode."""
    stmt = (
        select(models.Company)
        .options(selectinload(models.Company.role_tracks))
        .order_by(models.Company.name)
    )
    if tracking_mode is not None:
        stmt = stmt.where(models.Company.tracking_mode == tracking_mode)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_company_tracking(
    db: AsyncSession, company_id: int, mode: str
) -> Optional[models.Company]:
    """Update a company's tracking_mode. Returns None if company not found."""
    stmt = (
        update(models.Company)
        .where(models.Company.id == company_id)
        .values(tracking_mode=mode)
        .returning(models.Company)
    )
    result = await db.execute(stmt)
    await db.commit()
    row = result.scalars().first()
    return row


async def create_role_track(
    db: AsyncSession, track_in: schemas.RoleTrackCreate
) -> models.RoleTrack:
    """Add a new role track to an existing company."""
    db_track = models.RoleTrack(
        company_id=track_in.company_id,
        role_name=track_in.role_name,
        drive_type=track_in.drive_type,
        ctc_stipend_info=track_in.ctc_stipend_info,
        is_targeted_by_user=track_in.is_targeted_by_user,
    )
    db.add(db_track)
    await db.commit()
    await db.refresh(db_track)
    return db_track


# ---------------------------------------------------------------------------
# Timeline Event & State Mutation Operations
# ---------------------------------------------------------------------------


async def get_timeline_events(
    db: AsyncSession, drive_type: str = "FTE"
) -> List[models.TimelineEvent]:
    """Return active timeline events ordered by end_time ASC.

    Filters:
    - Company must be active and not IGNORED.
    - Event matches drive_type via its RoleTrack, OR drive_type is 'COMBINED',
      OR the event has no associated RoleTrack (company-level events).
    """
    stmt = (
        select(models.TimelineEvent)
        .join(models.Company, models.TimelineEvent.company_id == models.Company.id)
        .outerjoin(
            models.RoleTrack,
            models.TimelineEvent.role_track_id == models.RoleTrack.id,
        )
        .where(
            models.Company.is_active == True,  # noqa: E712
            models.Company.tracking_mode != "IGNORED",
        )
        .where(
            or_(
                # COMBINED viewport: accept every drive type
                true() if drive_type == "COMBINED" else false(),
                # Company-level events (no role track) always appear
                models.TimelineEvent.role_track_id == None,  # noqa: E711
                # Role-specific events matching the requested drive type
                models.RoleTrack.drive_type == drive_type,
            )
        )
        .options(
            selectinload(models.TimelineEvent.company),
            selectinload(models.TimelineEvent.role_track),
        )
        .order_by(models.TimelineEvent.end_time.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_custom_event(
    db: AsyncSession, event_in: schemas.TimelineEventCreate
) -> models.TimelineEvent:
    """Create a new custom timeline event."""
    db_event = models.TimelineEvent(
        company_id=event_in.company_id,
        role_track_id=event_in.role_track_id,
        event_type=event_in.event_type,
        title=event_in.title,
        action_link=event_in.action_link,
        start_time=event_in.start_time,
        end_time=event_in.end_time,
        is_custom=True,
        user_status="PENDING",
    )
    db.add(db_event)
    await db.commit()
    await db.refresh(db_event)
    return db_event


async def set_event_completion(
    db: AsyncSession, event_id: int, is_completed: bool
) -> Optional[models.TimelineEvent]:
    """Toggle an event's completion status.

    - is_completed=True  → user_status='COMPLETED', completed_at=utc_now
    - is_completed=False → user_status='PENDING',   completed_at=None
    """
    now = datetime.now(timezone.utc)
    values: dict = (
        {"user_status": "COMPLETED", "completed_at": now}
        if is_completed
        else {"user_status": "PENDING", "completed_at": None}
    )
    stmt = (
        update(models.TimelineEvent)
        .where(models.TimelineEvent.id == event_id)
        .values(**values)
        .returning(models.TimelineEvent)
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.scalars().first()


async def extend_event_deadline(
    db: AsyncSession, event_id: int, new_end_time: datetime
) -> Optional[models.TimelineEvent]:
    """Extend an event's end_time. A MISSED event naturally recovers to PENDING
    when new_end_time > now (via the display_state property)."""
    stmt = (
        update(models.TimelineEvent)
        .where(models.TimelineEvent.id == event_id)
        .values(end_time=new_end_time)
        .returning(models.TimelineEvent)
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.scalars().first()


async def replace_form_event(
    db: AsyncSession,
    company_id: int,
    role_track_id: Optional[int],
    payload: schemas.ReplaceFormPayload,
) -> models.TimelineEvent:
    """Supersede the current FORM_SUBMISSION event with a new versioned entry.

    Finds the max version of existing FORM_SUBMISSION events for this company
    (and optional role_track) and inserts a brand-new event with version+1.
    """
    stmt = select(func.max(models.TimelineEvent.version)).where(
        models.TimelineEvent.company_id == company_id,
        models.TimelineEvent.event_type == "FORM_SUBMISSION",
    )
    if role_track_id is not None:
        stmt = stmt.where(models.TimelineEvent.role_track_id == role_track_id)

    result = await db.execute(stmt)
    current_max: Optional[int] = result.scalar()
    next_version = (current_max or 0) + 1

    new_event = models.TimelineEvent(
        company_id=company_id,
        role_track_id=role_track_id,
        event_type="FORM_SUBMISSION",
        title=payload.title or "Google Form Submission",
        action_link=payload.new_form_link,
        end_time=payload.new_end_time,
        user_status="PENDING",
        is_custom=True,
        version=next_version,
    )
    db.add(new_event)
    await db.commit()
    await db.refresh(new_event)
    return new_event
