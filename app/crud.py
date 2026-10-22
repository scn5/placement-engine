from datetime import datetime, timezone
from typing import Iterable, List, Optional

from sqlalchemy import and_, false, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import models, schemas


# ---------------------------------------------------------------------------
# Company Operations
# ---------------------------------------------------------------------------


async def create_company(
    db: AsyncSession, company_in: schemas.CompanyCreate
) -> models.Company:
    """Create a new company, normalizing its name to lowercase + stripped.

    Atomically persists a default child RoleTrack from the supplied role
    metadata. Manually created companies are pre-triaged (is_triaged=True)."""
    raw_name = (company_in.name or "").strip()
    if not raw_name and getattr(company_in, "company_name", None):
        raw_name = company_in.company_name.strip()
    if not raw_name:
        raw_name = "Untitled Company"

    normalized = raw_name.lower()
    env_type = (company_in.environment_type or "MAIN").strip().upper()
    if env_type not in ("MAIN", "TEST"):
        env_type = "MAIN"

    tracking_mode = company_in.tracking_mode or "ELIGIBLE"

    db_company = models.Company(
        name=raw_name,
        normalized_name=normalized,
        tracking_mode=tracking_mode,
        environment_type=env_type,
        is_custom=company_in.is_custom,
    )
    db.add(db_company)
    await db.flush()  # assign db_company.id within the same transaction

    role_name = (
        getattr(company_in, "target_role", None)
        or company_in.role_name
        or "Default Role"
    ).strip() or "Default Role"

    db_track = models.RoleTrack(
        company_id=db_company.id,
        role_name=role_name,
        drive_type=company_in.drive_type or "FTE",
        ctc_stipend_info=company_in.ctc_stipend_info,
        tracking_mode=tracking_mode,
        # Placeholder default track: never a triage candidate (manual or not).
        is_triaged=True,
    )
    db.add(db_track)
    await db.commit()
    await db.refresh(db_company)
    await db.refresh(db_company, attribute_names=["role_tracks"])
    return db_company


async def get_companies(
    db: AsyncSession,
    tracking_mode: Optional[str] = None,
    env: Optional[str] = None,
) -> List[models.Company]:
    """Retrieve active companies ordered by name, eagerly loading role_tracks.
    Optionally filter by tracking_mode and/or environment_type (MAIN | TEST)."""
    stmt = (
        select(models.Company)
        .where(models.Company.is_active == True)  # noqa: E712
        .options(selectinload(models.Company.role_tracks))
        .order_by(models.Company.name)
    )
    if tracking_mode is not None:
        stmt = stmt.where(models.Company.tracking_mode == tracking_mode)
    if env is not None and env.strip().upper() in ("MAIN", "TEST"):
        stmt = stmt.where(models.Company.environment_type == env.strip().upper())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def soft_delete_company(
    db: AsyncSession, company_id: int
) -> bool:
    """Soft-delete a company by setting Company.is_active = False and marking
    all its TimelineEvent records with is_deleted = True.
    Returns True if the company was found and soft-deleted, False otherwise."""
    check_stmt = select(models.Company).where(models.Company.id == company_id)
    res = await db.execute(check_stmt)
    if res.scalars().first() is None:
        return False

    await db.execute(
        update(models.Company)
        .where(models.Company.id == company_id)
        .values(is_active=False)
    )
    await db.execute(
        update(models.TimelineEvent)
        .where(models.TimelineEvent.company_id == company_id)
        .values(is_deleted=True)
    )
    await db.commit()
    return True


async def update_company_tracking(
    db: AsyncSession, company_id: int, mode: str
) -> Optional[models.Company]:
    """Update a company's tracking_mode and cascade to all its child RoleTracks.
    Eagerly loads role_tracks to prevent MissingGreenlet in async serializers.
    Returns None if company not found."""
    check_stmt = select(models.Company).where(models.Company.id == company_id)
    res = await db.execute(check_stmt)
    if res.scalars().first() is None:
        return None

    # Update company tracking mode
    await db.execute(
        update(models.Company)
        .where(models.Company.id == company_id)
        .values(tracking_mode=mode)
    )

    # Cascade to all child role tracks
    await db.execute(
        update(models.RoleTrack)
        .where(models.RoleTrack.company_id == company_id)
        .values(tracking_mode=mode)
    )

    await db.commit()

    # Eagerly load company with role_tracks for response serialization
    result = await db.execute(
        select(models.Company)
        .where(models.Company.id == company_id)
        .options(selectinload(models.Company.role_tracks))
    )
    return result.scalars().first()


async def reopen_timeline_event(
    db: AsyncSession, event_id: int
) -> Optional[models.TimelineEvent]:
    """Reopen / uncomplete an event: sets user_status='PENDING', completed_at=None,
    restoring display_state dynamically (PENDING if future, MISSED if past)."""
    return await set_event_completion(db, event_id, is_completed=False)


async def soft_delete_timeline_event(
    db: AsyncSession, event_id: int
) -> bool:
    """Soft-delete a timeline event by setting TimelineEvent.is_deleted = True.
    Returns True if the event was found and soft-deleted, False otherwise."""
    check_stmt = select(models.TimelineEvent).where(models.TimelineEvent.id == event_id)
    res = await db.execute(check_stmt)
    if res.scalars().first() is None:
        return False

    await db.execute(
        update(models.TimelineEvent)
        .where(models.TimelineEvent.id == event_id)
        .values(is_deleted=True)
    )
    await db.commit()
    return True


async def create_role_track(
    db: AsyncSession, track_in: schemas.RoleTrackCreate
) -> models.RoleTrack:
    """Add a new role track to an existing company."""
    db_track = models.RoleTrack(
        company_id=track_in.company_id,
        role_name=track_in.role_name,
        drive_type=track_in.drive_type,
        ctc_stipend_info=track_in.ctc_stipend_info,
        tracking_mode=track_in.tracking_mode,
        is_triaged=track_in.is_triaged,
    )
    db.add(db_track)
    await db.commit()
    await db.refresh(db_track)
    return db_track


# ---------------------------------------------------------------------------
# Timeline Event & State Mutation Operations
# ---------------------------------------------------------------------------


async def get_timeline_events(
    db: AsyncSession,
    drive_type: str = "FTE",
    env: Optional[str] = None,
) -> List[models.TimelineEvent]:
    """Return active timeline events ordered by end_time ASC.

    Filters:
    - Company must be active (Company.is_active == True).
    - TimelineEvent must not be soft-deleted (TimelineEvent.is_deleted == False).
    - RoleTrack.tracking_mode must be 'ELIGIBLE'.
    - Event matches drive_type via its RoleTrack (or drive_type == 'COMBINED').
    - Optionally filter by Company.environment_type (MAIN | TEST).
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
            models.TimelineEvent.is_deleted == False,  # noqa: E712
            or_(
                models.RoleTrack.tracking_mode == "ELIGIBLE",
                and_(
                    models.TimelineEvent.role_track_id.is_(None),
                    models.Company.tracking_mode == "ELIGIBLE",
                ),
            ),
        )
        .where(
            or_(
                # COMBINED viewport: accept every drive type
                true() if drive_type == "COMBINED" else false(),
                # Events with no specific role track apply to all viewports
                models.TimelineEvent.role_track_id.is_(None),
                # COMBINED role tracks apply to all viewports
                models.RoleTrack.drive_type == "COMBINED",
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
    if env is not None and env.strip().upper() in ("MAIN", "TEST"):
        stmt = stmt.where(models.Company.environment_type == env.strip().upper())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _load_event(
    db: AsyncSession, event_id: int
) -> Optional[models.TimelineEvent]:
    """Reload an event with company/role_track eagerly loaded (response enrichment)."""
    result = await db.execute(
        select(models.TimelineEvent)
        .where(models.TimelineEvent.id == event_id)
        .options(
            selectinload(models.TimelineEvent.company),
            selectinload(models.TimelineEvent.role_track),
        )
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def get_company_events(
    db: AsyncSession, company_id: int
) -> List[models.TimelineEvent]:
    """Return every active timeline event for a company, ordered chronologically."""
    stmt = (
        select(models.TimelineEvent)
        .where(
            models.TimelineEvent.company_id == company_id,
            models.TimelineEvent.is_deleted == False,  # noqa: E712
        )
        .options(
            selectinload(models.TimelineEvent.company),
            selectinload(models.TimelineEvent.role_track),
        )
        .order_by(models.TimelineEvent.end_time.asc(), models.TimelineEvent.id.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_custom_event(
    db: AsyncSession, event_in: schemas.TimelineEventCreate
) -> models.TimelineEvent:
    """Create a new custom timeline event."""
    role_track_id = event_in.role_track_id
    if role_track_id is None:
        track_result = await db.execute(
            select(models.RoleTrack.id)
            .where(models.RoleTrack.company_id == event_in.company_id)
            .order_by(models.RoleTrack.id.asc())
            .limit(1)
        )
        role_track_id = track_result.scalar()
    db_event = models.TimelineEvent(
        company_id=event_in.company_id,
        role_track_id=role_track_id,
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
    return await _load_event(db, db_event.id)


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
    row = result.scalars().first()
    return await _load_event(db, row.id) if row is not None else None


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
    row = result.scalars().first()
    return await _load_event(db, row.id) if row is not None else None


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
        start_time=payload.new_end_time,
        end_time=payload.new_end_time,
        user_status="PENDING",
        is_custom=True,
        version=next_version,
    )
    db.add(new_event)
    await db.commit()
    return await _load_event(db, new_event.id)


# ---------------------------------------------------------------------------
# Triage Operations
# ---------------------------------------------------------------------------


async def get_pending_triage_items(
    db: AsyncSession,
    env: Optional[str] = None,
) -> List[models.RoleTrack]:
    """Query RoleTracks joined with Company where is_triaged == False and Company.is_active == True,
    eagerly loading company, ordered by created_at DESC.
    Optionally filter by Company.environment_type (MAIN | TEST)."""
    stmt = (
        select(models.RoleTrack)
        .join(models.Company, models.RoleTrack.company_id == models.Company.id)
        .where(
            models.Company.is_active == True,  # noqa: E712
            models.RoleTrack.is_triaged == False,  # noqa: E712
        )
        .options(selectinload(models.RoleTrack.company))
        .order_by(models.RoleTrack.created_at.desc())
    )
    if env is not None and env.strip().upper() in ("MAIN", "TEST"):
        stmt = stmt.where(models.Company.environment_type == env.strip().upper())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def triage_role_track(
    db: AsyncSession, role_track_id: int, action: str
) -> Optional[models.RoleTrack]:
    """Update RoleTrack.tracking_mode = action and strictly set is_triaged = True."""
    stmt = (
        select(models.RoleTrack)
        .options(selectinload(models.RoleTrack.company))
        .where(models.RoleTrack.id == role_track_id)
    )
    result = await db.execute(stmt)
    track = result.scalars().first()
    if track is None:
        return None

    track.tracking_mode = action
    track.is_triaged = True
    track.updated_at = datetime.now(timezone.utc)

    if action == "ELIGIBLE":
        await db.execute(
            update(models.Company)
            .where(models.Company.id == track.company_id)
            .values(tracking_mode="ELIGIBLE")
        )

    await db.commit()

    # Re-eagerly load RoleTrack with company to prevent MissingGreenletError on serialization
    res = await db.execute(
        select(models.RoleTrack)
        .options(selectinload(models.RoleTrack.company))
        .where(models.RoleTrack.id == role_track_id)
    )
    return res.scalars().first()


async def flag_for_retriage(
    db: AsyncSession, role_track_id: int, note: str
) -> Optional[models.RoleTrack]:
    """Update latest_status_note on a RoleTrack.
    Only if tracking_mode == 'SILENT_TRACK', also set is_triaged = False
    so it reappears in the pending triage queue."""
    stmt = (
        select(models.RoleTrack)
        .options(selectinload(models.RoleTrack.company))
        .where(models.RoleTrack.id == role_track_id)
    )
    result = await db.execute(stmt)
    track = result.scalars().first()
    if track is None:
        return None

    track.latest_status_note = note
    track.updated_at = datetime.now(timezone.utc)
    if track.tracking_mode == "SILENT_TRACK":
        track.is_triaged = False

    await db.commit()
    await db.refresh(track)
    return track


async def link_or_create_subchannel_company(
    db: AsyncSession,
    subchannel_id: int,
    company_name: str,
    environment_type: str = "MAIN",
) -> models.Company:
    """Link an auto-joined subchannel to an existing company by subchannel_id or normalized name,
    or create a new company in SILENT_TRACK mode with an untriaged (is_triaged=False) RoleTrack."""
    env = (environment_type or "MAIN").strip().upper()
    if env not in ("MAIN", "TEST"):
        env = "MAIN"

    # 1. Existing company matching subchannel_id
    res = await db.execute(
        select(models.Company).where(models.Company.subchannel_id == subchannel_id)
    )
    existing = res.scalars().first()
    if existing:
        if existing.environment_type != env:
            existing.environment_type = env
            await db.commit()
            await db.refresh(existing)
        return existing

    # 2. Existing company matching normalized name
    norm = company_name.strip().lower()
    res = await db.execute(
        select(models.Company).where(models.Company.normalized_name == norm)
    )
    matched = res.scalars().first()
    if matched:
        matched.subchannel_id = subchannel_id
        matched.environment_type = env
        await db.commit()
        await db.refresh(matched)
        return matched

    # 3. Create new Company in SILENT_TRACK mode
    display_name = company_name.strip() or f"Subchannel {subchannel_id}"
    new_company = models.Company(
        name=display_name,
        normalized_name=norm or f"subchannel_{subchannel_id}",
        subchannel_id=subchannel_id,
        tracking_mode="SILENT_TRACK",
        environment_type=env,
        is_custom=False,
    )
    db.add(new_company)
    await db.flush()

    role_track = models.RoleTrack(
        company_id=new_company.id,
        role_name="Default Role",
        drive_type="FTE",
        tracking_mode="SILENT_TRACK",
        is_triaged=False,
        lifecycle_status="APPLICATION_WINDOW",
    )
    db.add(role_track)
    await db.commit()
    await db.refresh(new_company)
    return new_company


def _ingestible_company_filters(environments: Iterable[str]) -> list:
    """Companies whose channel the ingestion worker may read (PROJECT_STATE §0.1
    invariants 2–3): active, in an ingested environment, and not effectively
    IGNORED — i.e. not IGNORED at company level, and not every role IGNORED."""
    has_roles = (
        select(models.RoleTrack.id)
        .where(models.RoleTrack.company_id == models.Company.id)
        .exists()
    )
    has_tracked_role = (
        select(models.RoleTrack.id)
        .where(
            models.RoleTrack.company_id == models.Company.id,
            models.RoleTrack.tracking_mode != "IGNORED",
        )
        .exists()
    )
    return [
        models.Company.is_active == True,  # noqa: E712
        models.Company.environment_type.in_(list(environments)),
        models.Company.tracking_mode != "IGNORED",
        or_(~has_roles, has_tracked_role),
    ]


async def get_ingestible_subchannel_ids(
    db: AsyncSession, environments: Iterable[str]
) -> set[int]:
    """Subchannel IDs of every company the worker may read."""
    result = await db.execute(
        select(models.Company.subchannel_id).where(
            models.Company.subchannel_id.is_not(None),
            *_ingestible_company_filters(environments),
        )
    )
    return {int(chat_id) for chat_id in result.scalars().all()}


async def is_company_ingestible(
    db: AsyncSession, company_id: int, environments: Iterable[str]
) -> bool:
    result = await db.execute(
        select(models.Company.id).where(
            models.Company.id == company_id,
            *_ingestible_company_filters(environments),
        )
    )
    return result.first() is not None


async def get_company_by_subchannel_id(
    db: AsyncSession,
    subchannel_id: int,
) -> Optional[models.Company]:
    """Retrieve an active or linked company by its subchannel ID."""
    stmt = select(models.Company).where(models.Company.subchannel_id == subchannel_id)
    result = await db.execute(stmt)
    return result.scalars().first()


async def reschedule_event(
    db: AsyncSession,
    event_id: int,
    new_end_time: datetime,
    new_start_time: Optional[datetime] = None,
) -> Optional[models.TimelineEvent]:
    """Reschedule an event by updating both start_time and end_time."""
    values: dict = {"end_time": new_end_time}
    if new_start_time is not None:
        values["start_time"] = new_start_time

    stmt = (
        update(models.TimelineEvent)
        .where(models.TimelineEvent.id == event_id)
        .values(**values)
        .returning(models.TimelineEvent)
    )
    result = await db.execute(stmt)
    await db.commit()
    row = result.scalars().first()
    return await _load_event(db, row.id) if row is not None else None

