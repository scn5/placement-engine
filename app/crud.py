from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import and_, false, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import models, schemas

NOTE_KINDS = ("CRITERIA_RELAXATION", "GENERAL")
_REVIEW_REASON_LIMIT = 500


class TrackingLocked(Exception):
    """The change would touch an IGNORED company or role. IGNORED is terminal
    and human-only: only PATCH /companies/{id}/tracking (the Companies-screen
    toggle) can reverse it (PROJECT_STATE §0.1 invariants 1, 2 and 5)."""


async def is_effectively_ignored(db: AsyncSession, company_id: int) -> bool:
    """IGNORED at company level, or every one of its roles IGNORED (invariant 2)."""
    company = await db.get(models.Company, company_id)
    if company is None:
        return False
    if company.tracking_mode == "IGNORED":
        return True
    result = await db.execute(
        select(models.RoleTrack.tracking_mode).where(models.RoleTrack.company_id == company_id)
    )
    modes = list(result.scalars().all())
    return bool(modes) and all(mode == "IGNORED" for mode in modes)


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
    This is the only path that moves a company out of IGNORED (invariant 1). An
    explicit company-level choice also marks every role triaged, so a company
    never shows in Triage and on the Timeline at once (invariant 9).
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
        .values(tracking_mode=mode, is_triaged=True)
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
    """Add a new role track to an existing company. Refused for an effectively
    IGNORED company: a new role would bring it back without the Companies-screen
    toggle (invariant 1)."""
    if await is_effectively_ignored(db, track_in.company_id):
        raise TrackingLocked("This company is IGNORED. Change it from the Companies screen first.")
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
            models.Company.tracking_mode != "IGNORED",
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
            .order_by((models.RoleTrack.tracking_mode == "IGNORED").asc(), models.RoleTrack.id.asc())
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
    """Extend an event's end_time. A point deadline (start == end) moves its
    start with it; a window keeps its start. A MISSED event naturally recovers
    to PENDING when new_end_time > now (via the display_state property)."""
    event = await db.get(models.TimelineEvent, event_id)
    if event is None:
        return None
    if event.start_time is not None and event.start_time == event.end_time:
        event.start_time = new_end_time
    event.end_time = new_end_time
    await db.commit()
    return await _load_event(db, event_id)


async def replace_form_event(
    db: AsyncSession,
    company_id: int,
    role_track_id: Optional[int],
    payload: schemas.ReplaceFormPayload,
    supersedes: Optional[int] = None,
) -> models.TimelineEvent:
    """Supersede the current FORM_SUBMISSION event with a new versioned entry.

    Finds the max version of existing FORM_SUBMISSION events for this company
    (and optional role_track) and inserts a brand-new event with version+1.
    The superseded form — *supersedes*, or else every active form of the
    company (and role) — is soft-deleted, so only the new link is shown.
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

    superseded = update(models.TimelineEvent).where(
        models.TimelineEvent.company_id == company_id,
        models.TimelineEvent.is_deleted == False,  # noqa: E712
    )
    if supersedes is not None:
        superseded = superseded.where(models.TimelineEvent.id == supersedes)
    else:
        superseded = superseded.where(models.TimelineEvent.event_type == "FORM_SUBMISSION")
        if role_track_id is not None:
            superseded = superseded.where(models.TimelineEvent.role_track_id == role_track_id)
    await db.execute(superseded.values(is_deleted=True))

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


async def annotate_event(
    db: AsyncSession,
    event_id: int,
    *,
    source_chat_id: Optional[int] = None,
    source_message_id: Optional[int] = None,
    review_reasons: Iterable[str] = (),
) -> None:
    """Record which Telegram message last created or updated an event, and flag
    it for the user's review (needs_review) with the given reasons. A flag is
    never cleared here — only the user clears it."""
    event = await db.get(models.TimelineEvent, event_id)
    if event is None:
        return
    if source_message_id is not None:
        event.source_chat_id = source_chat_id
        event.source_message_id = source_message_id
    reasons = [r for r in review_reasons if r]
    if reasons:
        known = [r for r in (event.review_reason or "").split("; ") if r]
        merged = known + [r for r in reasons if r not in known]
        event.needs_review = True
        event.review_reason = "; ".join(merged)[:_REVIEW_REASON_LIMIT]
    await db.commit()


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
            models.Company.tracking_mode != "IGNORED",
            models.RoleTrack.tracking_mode != "IGNORED",
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
    """Update RoleTrack.tracking_mode = action and strictly set is_triaged = True.

    Raises TrackingLocked for an IGNORED role or an effectively IGNORED company
    (invariants 1 and 5). Accepting a role promotes its company to ELIGIBLE;
    discarding the last role that was not IGNORED marks the company IGNORED, so
    the Companies screen agrees (invariant 2)."""
    stmt = (
        select(models.RoleTrack)
        .options(selectinload(models.RoleTrack.company))
        .where(models.RoleTrack.id == role_track_id)
    )
    result = await db.execute(stmt)
    track = result.scalars().first()
    if track is None:
        return None
    if track.tracking_mode == "IGNORED" or await is_effectively_ignored(db, track.company_id):
        raise TrackingLocked("This role is IGNORED. Change it from the Companies screen.")

    track.tracking_mode = action
    track.is_triaged = True
    track.updated_at = datetime.now(timezone.utc)
    await db.flush()

    if action == "ELIGIBLE":
        await db.execute(
            update(models.Company)
            .where(models.Company.id == track.company_id)
            .values(tracking_mode="ELIGIBLE")
        )
    elif action == "IGNORED" and await is_effectively_ignored(db, track.company_id):
        await db.execute(
            update(models.Company)
            .where(models.Company.id == track.company_id)
            .values(tracking_mode="IGNORED")
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
    db: AsyncSession, role_track_id: int, note: str, note_kind: str = "GENERAL"
) -> Optional[models.RoleTrack]:
    """Record a status note on a RoleTrack (PROJECT_STATE §0.1 invariant 6).

    - The note text and its kind (CRITERIA_RELAXATION | GENERAL; anything else
      counts as GENERAL) are always stored.
    - Only a CRITERIA_RELAXATION note on a SILENT_TRACK role re-opens it in
      Triage (is_triaged = False) — a proposal; tracking_mode never changes.
    - An IGNORED role, or a role of an effectively IGNORED company, is never
      touched (invariant 1)."""
    stmt = (
        select(models.RoleTrack)
        .options(selectinload(models.RoleTrack.company))
        .where(models.RoleTrack.id == role_track_id)
    )
    result = await db.execute(stmt)
    track = result.scalars().first()
    if track is None:
        return None
    if track.tracking_mode == "IGNORED" or await is_effectively_ignored(db, track.company_id):
        return track

    kind = note_kind if note_kind in NOTE_KINDS else "GENERAL"
    track.latest_status_note = note
    track.latest_note_kind = kind
    track.updated_at = datetime.now(timezone.utc)
    if kind == "CRITERIA_RELAXATION" and track.tracking_mode == "SILENT_TRACK":
        track.is_triaged = False

    await db.commit()
    await db.refresh(track)
    return track


async def link_or_create_subchannel_company(
    db: AsyncSession,
    subchannel_id: int,
    company_name: str,
    environment_type: str = "MAIN",
    *,
    fallback_name: Optional[str] = None,
    invite_token: Optional[str] = None,
    main_feed_msg_id: Optional[int] = None,
) -> Tuple[models.Company, bool]:
    """Link a joined subchannel to its company; returns (company, newly_linked).

    1. A company already linked to this channel is returned as is — its
       environment is never changed.
    2. Otherwise *company_name*, then *fallback_name* (the channel title), is
       matched against active companies of the same environment that have no
       channel yet; a match is linked.
    3. Otherwise a SILENT_TRACK company with an untriaged default role is
       created under the first free name, or "<name> (<channel id>)" if every
       name is taken by a company that has its own channel.

    Linking never changes tracking_mode: a match that is IGNORED stays IGNORED
    (PROJECT_STATE §0.1 invariant 1). *invite_token* and *main_feed_msg_id*
    record the invite post, so a re-post is recognised and replies to it are
    attributed to the company.
    """
    env = (environment_type or "MAIN").strip().upper()
    if env not in ("MAIN", "TEST"):
        env = "MAIN"

    def remember_invite(company: models.Company) -> None:
        if invite_token:
            company.invite_token = invite_token
        if main_feed_msg_id is not None:
            company.main_feed_msg_id = main_feed_msg_id

    # 1. Existing company matching subchannel_id
    existing = await get_company_by_subchannel_id(db, subchannel_id)
    if existing:
        remember_invite(existing)
        await db.commit()
        await db.refresh(existing)
        return existing, False

    # 2. An active company of that name in this environment without a channel
    names = []
    for candidate in (company_name, fallback_name):
        candidate = (candidate or "").strip()
        if candidate and candidate.lower() not in [n.lower() for n in names]:
            names.append(candidate)
    if not names:
        names.append(f"Subchannel {subchannel_id}")

    create_as: Optional[str] = None
    for name in names:
        res = await db.execute(
            select(models.Company).where(
                models.Company.normalized_name == name.lower(),
                models.Company.environment_type == env,
            )
        )
        matched = res.scalars().first()
        if matched is None:
            create_as = name
            break
        if matched.is_active and matched.subchannel_id is None:
            matched.subchannel_id = subchannel_id
            remember_invite(matched)
            await db.commit()
            await db.refresh(matched)
            return matched, True
        # Taken: deleted, or linked to another channel (e.g. a second drive).
    if create_as is None:
        create_as = f"{names[-1]} ({subchannel_id})"

    # 3. Create new Company in SILENT_TRACK mode
    new_company = models.Company(
        name=create_as,
        normalized_name=create_as.lower(),
        subchannel_id=subchannel_id,
        tracking_mode="SILENT_TRACK",
        environment_type=env,
        is_custom=False,
    )
    remember_invite(new_company)
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
    return new_company, True


async def get_company_by_invite_token(
    db: AsyncSession, invite_token: str, environment_type: str
) -> Optional[models.Company]:
    """The company whose subchannel was joined from *invite_token* in this environment."""
    result = await db.execute(
        select(models.Company).where(
            models.Company.invite_token == invite_token,
            models.Company.environment_type == environment_type,
            models.Company.subchannel_id.is_not(None),
        )
    )
    return result.scalars().first()


async def get_company_by_main_feed_post(
    db: AsyncSession, environment_type: str, main_feed_msg_id: int
) -> Optional[models.Company]:
    """The company whose invite was posted as *main_feed_msg_id* in this
    environment's root feed (each environment has one root feed), or None if
    no single company was."""
    result = await db.execute(
        select(models.Company).where(
            models.Company.main_feed_msg_id == main_feed_msg_id,
            models.Company.environment_type == environment_type,
        ).limit(2)
    )
    matches = result.scalars().all()
    return matches[0] if len(matches) == 1 else None


async def record_invite_post(
    db: AsyncSession, company_id: int, invite_token: str, main_feed_msg_id: int
) -> None:
    """Remember the latest invite post of a company: replies to it are
    attributed to the company."""
    await db.execute(
        update(models.Company)
        .where(models.Company.id == company_id)
        .values(invite_token=invite_token, main_feed_msg_id=main_feed_msg_id)
    )
    await db.commit()


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


async def get_trackable_companies(db: AsyncSession, environment: str) -> List[models.Company]:
    """Active, not effectively IGNORED companies of one environment."""
    result = await db.execute(select(models.Company).where(*_ingestible_company_filters([environment])))
    return list(result.scalars().all())


async def is_company_trackable(db: AsyncSession, company_id: int) -> bool:
    """Active and not effectively IGNORED, in any environment: the dispatcher's
    re-check before it changes anything for a company (invariant 3)."""
    return await is_company_ingestible(db, company_id, ("MAIN", "TEST"))


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
    action_link: Optional[str] = None,
) -> Optional[models.TimelineEvent]:
    """Reschedule an event by updating both start_time and end_time (and its
    link, when a new one is given)."""
    values: dict = {"end_time": new_end_time}
    if new_start_time is not None:
        values["start_time"] = new_start_time
    if action_link:
        values["action_link"] = action_link

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

