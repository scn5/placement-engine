"""
app/ai/dispatcher.py
State-machine dispatcher: resolves the company, builds the schedule context,
calls the LLM extractor, then applies each extracted action.

Entry point: process_and_dispatch(chat_id, context_header, messages, db, ...)
Called by the ingestion worker's payload handler (app/worker.py). An
ExtractionFailed from the extractor propagates, so the batch is recorded FAILED
in the message ledger and retried.

Rules (PROJECT_STATE §0.1, §8.4):
  • Nothing is changed for a deleted or effectively-IGNORED company, or for an
    IGNORED role (invariants 1–3).
  • A company known from the source — the subchannel, a reply to or re-post of
    its invite post, the channel an invite post just joined — is authoritative
    for every action. Root-feed posts are matched to companies by name, within
    the feed's environment only.
  • Updates target existing events by id; an edited message updates the events
    it created instead of adding new ones; an identical event is never created
    twice (gaps H3, H4).
  • Uncertain results are applied but flagged needs_review (recall-first).
  • Every created or updated event records its source message.
  • Only a CRITERIA_RELAXATION note re-opens a SILENT_TRACK role in Triage;
    tracking_mode is never changed here (invariant 6).
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models
from app import schemas as app_schemas
from app.ai import extractor
from app.ai.prompts import format_message_block
from app.ai.schemas import ExtractedEvent
from app.sources import SubchannelLink

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30), name="IST")

_TEMPORAL_ACTIONS = ("CREATE_EVENT", "EXTEND_DEADLINE", "RESCHEDULE_EVENT", "REPLACE_FORM")
# Two events of one company and type this close in time are the same event.
_SAME_TIME = timedelta(minutes=1)
# A date this far before its own announcement was probably resolved wrongly.
_BEFORE_POST_SLACK = timedelta(hours=1)
_FAR_FUTURE = timedelta(days=365)
_MAX_CONTEXT_EVENTS = 50
_MAX_MENTIONED_COMPANIES = 5
_MIN_MENTION_LENGTH = 3


# ---------------------------------------------------------------------------
# The batch being dispatched
# ---------------------------------------------------------------------------


@dataclass
class _Batch:
    """The messages of one dispatch: their ids, post times and edits."""

    chat_id: int
    ids: Set[int] = field(default_factory=set)
    posted_at: Dict[int, datetime] = field(default_factory=dict)
    edited: Set[int] = field(default_factory=set)

    @classmethod
    def of(cls, chat_id: int, messages: List[dict]) -> "_Batch":
        batch = cls(chat_id)
        for message in messages:
            message_id = message.get("id")
            if message_id is None:
                continue
            batch.ids.add(message_id)
            if message.get("date"):
                batch.posted_at[message_id] = datetime.fromtimestamp(message["date"], timezone.utc)
            if message.get("is_edit"):
                batch.edited.add(message_id)
        return batch

    def source_of(self, extracted: ExtractedEvent) -> Optional[int]:
        """The message an action came from: the id the LLM named if it is in
        this batch, else the only message of a one-message batch."""
        if extracted.source_message_id in self.ids:
            return extracted.source_message_id
        if len(self.ids) == 1:
            return next(iter(self.ids))
        return None


def _feed_environment(context_header: str) -> str:
    return "TEST" if "test" in (context_header or "").lower() else "MAIN"


def _ist_iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(_IST).isoformat() if value else None


# ---------------------------------------------------------------------------
# Companies, roles and schedule context
# ---------------------------------------------------------------------------


async def _roles(db: AsyncSession, company_id: int) -> List[models.RoleTrack]:
    result = await db.execute(
        select(models.RoleTrack)
        .where(models.RoleTrack.company_id == company_id)
        .order_by(models.RoleTrack.id.asc())
    )
    return list(result.scalars().all())


def _match_role(
    roles: List[models.RoleTrack], role_name: Optional[str]
) -> Tuple[Optional[models.RoleTrack], bool]:
    """(role, named): the role *role_name* refers to (exact, then partial match),
    else the company's first role that is not IGNORED."""
    wanted = (role_name or "").strip().lower()
    if wanted:
        exact = [r for r in roles if r.role_name.lower() == wanted]
        partial = [r for r in roles if wanted in r.role_name.lower()]
        for role in exact or partial:
            return role, True
    for role in roles:
        if role.tracking_mode != "IGNORED":
            return role, False
    return None, False


async def _company_schedule(db: AsyncSession, company: models.Company, chat_id: int) -> dict:
    """A company's roles and its non-deleted events (not those of IGNORED
    roles), with ids, for the prompt."""
    roles = await _roles(db, company.id)
    ignored_roles = {r.id for r in roles if r.tracking_mode == "IGNORED"}
    role_names = {r.id: r.role_name for r in roles}
    result = await db.execute(
        select(models.TimelineEvent)
        .where(
            models.TimelineEvent.company_id == company.id,
            models.TimelineEvent.is_deleted == False,  # noqa: E712
        )
        .order_by(models.TimelineEvent.end_time.desc())
        .limit(_MAX_CONTEXT_EVENTS)
    )
    events = [e for e in reversed(result.scalars().all()) if e.role_track_id not in ignored_roles]
    return {
        "company_id": company.id,
        "company_name": company.name,
        "roles": [r.role_name for r in roles if r.tracking_mode != "IGNORED"],
        "events": [
            {
                "id": e.id,
                "title": e.title,
                "event_type": e.event_type,
                "role_name": role_names.get(e.role_track_id),
                # Stored as UTC; shown to the LLM in IST like the announcements.
                "start_time": _ist_iso(e.start_time),
                "end_time": _ist_iso(e.end_time) or "unknown",
                "action_link": e.action_link,
                "user_status": e.user_status,
                "source_message_id": e.source_message_id if e.source_chat_id == chat_id else None,
            }
            for e in events
        ],
    }


async def _build_context(
    db: AsyncSession,
    chat_id: int,
    source_company: Optional[models.Company],
    mentioned: List[models.Company],
) -> dict:
    """The schedule context for the prompt: the source company (authoritative,
    also exposed as company_id / company_name / events), else the tracked
    companies a root-feed post mentions. Empty when there are none."""
    companies = [source_company] if source_company is not None else list(mentioned)
    if not companies:
        return {}
    schedules = [await _company_schedule(db, c, chat_id) for c in companies]
    context: dict = {"companies": schedules}
    if source_company is not None:
        context.update(
            company_id=source_company.id,
            company_name=source_company.name,
            events=schedules[0]["events"],
        )
    return context


async def _companies_mentioned(db: AsyncSession, text: str, environment: str) -> List[models.Company]:
    """Tracked companies of *environment* named in a root-feed post, so their
    schedules (and event ids) can be shown to the LLM."""
    lowered = text.lower()
    found = []
    for company in await crud.get_trackable_companies(db, environment):
        name = company.normalized_name
        if len(name) >= _MIN_MENTION_LENGTH and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered):
            found.append(company)
    found.sort(key=lambda c: len(c.normalized_name), reverse=True)
    return found[:_MAX_MENTIONED_COMPANIES]


async def _company_named(
    db: AsyncSession, name: str, environment: str, cache: Dict[str, models.Company]
) -> Optional[models.Company]:
    """The company called *name* in *environment* — deleted and IGNORED ones
    included, so the caller can drop the action — or a new SILENT_TRACK company
    with an untriaged default role, which then waits in Triage."""
    normalized = name.strip().lower()
    if not normalized:
        return None
    if normalized in cache:
        return cache[normalized]
    query = select(models.Company).where(
        models.Company.normalized_name == normalized,
        models.Company.environment_type == environment,
    )
    company = (await db.execute(query)).scalars().first()
    if company is None:
        company = models.Company(
            name=name.strip(),
            normalized_name=normalized,
            tracking_mode="SILENT_TRACK",
            environment_type=environment,
            is_custom=False,
        )
        db.add(company)
        try:
            await db.flush()
            db.add(models.RoleTrack(
                company_id=company.id,
                role_name="Default Role",
                drive_type="FTE",
                tracking_mode="SILENT_TRACK",
                is_triaged=False,
                lifecycle_status="APPLICATION_WINDOW",
            ))
            await db.commit()
            logger.info("Created company=%s (env=%s) from a root-feed post; it waits in Triage.", company.name, environment)
        except IntegrityError:  # created concurrently
            await db.rollback()
            company = (await db.execute(query)).scalars().first()
    if company is not None:
        cache[normalized] = company
    return company


# ---------------------------------------------------------------------------
# Applying actions
# ---------------------------------------------------------------------------


def _normalize_event_times(
    extracted: ExtractedEvent,
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Normalize start and end times for temporal events.
    - If both start_time and end_time are present, return (start_time, end_time).
    - If only start_time is present:
        • OA_WINDOW, PPT, INTERVIEW: end_time = start_time + 2 hours
        • FORM_SUBMISSION, IRIS_DEADLINE, CUSTOM: end_time = start_time
    - If only end_time is present:
        • start_time = end_time
    - If neither is present, return (None, None).
    """
    st = extracted.start_time
    et = extracted.end_time

    if st is not None and et is not None:
        return st, et

    if st is not None and et is None:
        etype = (extracted.event_type or "").upper()
        if etype in ("OA_WINDOW", "PPT", "INTERVIEW"):
            return st, st + timedelta(hours=2)
        else:
            return st, st

    if st is None and et is not None:
        return et, et

    return None, None


async def _active_events(db: AsyncSession, company_id: int) -> List[models.TimelineEvent]:
    """Non-deleted events of a company, except those of IGNORED roles."""
    result = await db.execute(
        select(models.TimelineEvent)
        .outerjoin(models.RoleTrack, models.TimelineEvent.role_track_id == models.RoleTrack.id)
        .where(
            models.TimelineEvent.company_id == company_id,
            models.TimelineEvent.is_deleted == False,  # noqa: E712
            (models.RoleTrack.tracking_mode.is_(None)) | (models.RoleTrack.tracking_mode != "IGNORED"),
        )
    )
    return list(result.scalars().all())


async def _find_target(
    db: AsyncSession,
    batch: _Batch,
    company: models.Company,
    role: Optional[models.RoleTrack],
    role_named: bool,
    extracted: ExtractedEvent,
    event_type: str,
    source_id: Optional[int],
) -> Tuple[Optional[models.TimelineEvent], List[str]]:
    """The existing event an action updates, and any doubts about the choice.

    1. target_event_id, if it names a live event of this company.
    2. For an edited message: the event that message created earlier.
    3. EXTEND / RESCHEDULE / REPLACE_FORM only: the company's active event of the
       same type (and named role); with several, the latest — flagged.
    A CREATE_EVENT with no target from 1–2 creates a new event."""
    reasons: List[str] = []
    active = await _active_events(db, company.id)
    by_id = {e.id: e for e in active}

    if extracted.target_event_id is not None:
        if extracted.target_event_id in by_id:
            return by_id[extracted.target_event_id], reasons
        reasons.append(f"target event {extracted.target_event_id} not found")

    if source_id is not None and source_id in batch.edited:
        from_message = [
            e for e in active
            if e.source_chat_id == batch.chat_id and e.source_message_id == source_id
        ]
        same_type = [e for e in from_message if e.event_type == event_type]
        pick = same_type if same_type else (from_message if len(from_message) == 1 else [])
        if len(pick) == 1:
            return pick[0], reasons
        if from_message:
            reasons.append("edited message: the event it created earlier could not be identified")

    if extracted.action == "CREATE_EVENT":
        return None, reasons

    candidates = [
        e for e in active
        if e.event_type == event_type and (not role_named or e.role_track_id in (role.id, None))
    ]
    if len(candidates) > 1:
        reasons.append("several matching events; the latest one was updated")
    if candidates:
        return max(candidates, key=lambda e: e.end_time), reasons
    return None, reasons


async def _find_duplicate(
    db: AsyncSession, company_id: int, event_type: str, end_time: datetime
) -> Optional[models.TimelineEvent]:
    result = await db.execute(
        select(models.TimelineEvent).where(
            models.TimelineEvent.company_id == company_id,
            models.TimelineEvent.event_type == event_type,
            models.TimelineEvent.is_deleted == False,  # noqa: E712
            models.TimelineEvent.end_time >= end_time - _SAME_TIME,
            models.TimelineEvent.end_time <= end_time + _SAME_TIME,
        )
    )
    return result.scalars().first()


async def _apply_temporal(
    db: AsyncSession,
    batch: _Batch,
    company: models.Company,
    role: Optional[models.RoleTrack],
    role_named: bool,
    extracted: ExtractedEvent,
) -> None:
    action = extracted.action
    start, end = _normalize_event_times(extracted)
    if end is None:
        logger.warning("%s skipped for company=%s: no start_time or end_time extracted.", action, company.name)
        return

    source_id = batch.source_of(extracted)
    reasons: List[str] = []
    if extracted.confidence == "LOW":
        reasons.append("low confidence")
    if start is not None and start > end:
        reasons.append("start is after the end")
        start = end
    posted = batch.posted_at.get(source_id) if source_id is not None else None
    if posted is not None and end < posted - _BEFORE_POST_SLACK:
        reasons.append("date is before the announcement")
    if end > (posted or datetime.now(timezone.utc)) + _FAR_FUTURE:
        reasons.append("date is more than a year away")

    event_type = extracted.event_type or "CUSTOM"
    if action == "REPLACE_FORM" and event_type == "CUSTOM":
        event_type = "FORM_SUBMISSION"

    target, doubts = await _find_target(db, batch, company, role, role_named, extracted, event_type, source_id)
    reasons.extend(doubts)

    if target is None:
        duplicate = await _find_duplicate(db, company.id, event_type, end)
        if duplicate is not None:
            logger.info(
                "%s skipped for company=%s: event id=%s already has this %s at that time.",
                action, company.name, duplicate.id, event_type,
            )
            return
        event = await crud.create_custom_event(
            db,
            app_schemas.TimelineEventCreate(
                company_id=company.id,
                role_track_id=role.id if role else None,
                event_type=event_type,
                title=extracted.title or f"{company.name} — {event_type}",
                action_link=extracted.action_link,
                start_time=start,
                end_time=end,
                is_custom=True,
            ),
        )
        verb = "CREATE_EVENT" if action == "CREATE_EVENT" else f"CREATE_EVENT (no earlier event for {action})"
    elif action == "EXTEND_DEADLINE":
        if end < target.end_time:
            reasons.append("the extension moves the deadline earlier")
        event = await crud.extend_event_deadline(db, target.id, end)
        verb = action
    elif action == "REPLACE_FORM" and extracted.action_link:
        event = await crud.replace_form_event(
            db,
            company.id,
            target.role_track_id,
            app_schemas.ReplaceFormPayload(
                new_form_link=extracted.action_link,
                new_end_time=end,
                title=extracted.title or target.title,
            ),
            supersedes=target.id,
        )
        verb = action
    else:  # RESCHEDULE_EVENT, a REPLACE_FORM without a link, or an event that already exists
        event = await crud.reschedule_event(db, target.id, end, start, action_link=extracted.action_link)
        verb = action if action != "CREATE_EVENT" else "RESCHEDULE_EVENT (update of an existing event)"

    await crud.annotate_event(
        db,
        event.id,
        source_chat_id=batch.chat_id,
        source_message_id=source_id,
        review_reasons=reasons,
    )
    logger.info(
        "%s: event=%s company=%s type=%s end=%s%s",
        verb, event.id, company.name, event_type, end,
        f" (needs review: {'; '.join(reasons)})" if reasons else "",
    )


async def _apply_note(
    db: AsyncSession,
    company: models.Company,
    roles: List[models.RoleTrack],
    role: Optional[models.RoleTrack],
    role_named: bool,
    extracted: ExtractedEvent,
) -> None:
    if not extracted.status_note:
        logger.warning("STATUS_NOTE skipped for company=%s: no status_note.", company.name)
        return
    kind = extracted.note_kind or "GENERAL"
    if role_named:
        targets = [role]
    else:
        targets = [r for r in roles if r.tracking_mode != "IGNORED"]
    if not roles:
        # A company without roles: give the note a role in the company's mode.
        new_role = models.RoleTrack(
            company_id=company.id,
            role_name="Default Role",
            drive_type=extracted.drive_type or "FTE",
            tracking_mode=company.tracking_mode,
            is_triaged=True,
        )
        db.add(new_role)
        await db.commit()
        targets = [new_role]
    for target in targets:
        await crud.flag_for_retriage(db, target.id, note=extracted.status_note, note_kind=kind)
    logger.info(
        "STATUS_NOTE (%s): company=%s roles=%s note=%r",
        kind, company.name, [t.role_name for t in targets], extracted.status_note,
    )


async def _apply(db: AsyncSession, batch: _Batch, company: models.Company, extracted: ExtractedEvent) -> None:
    """Apply one extracted action to *company* (already checked as trackable)."""
    roles = await _roles(db, company.id)
    role, role_named = _match_role(roles, extracted.role_name)
    if role is not None and role.tracking_mode == "IGNORED":
        logger.info(
            "Dropped action=%s for company=%s: role %r is IGNORED.",
            extracted.action, company.name, role.role_name,
        )
        return
    if extracted.action == "STATUS_NOTE":
        await _apply_note(db, company, roles, role, role_named, extracted)
    elif extracted.action in _TEMPORAL_ACTIONS:
        await _apply_temporal(db, batch, company, role, role_named, extracted)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def process_and_dispatch(
    chat_id: int,
    context_header: str,
    messages: List[dict],
    db: AsyncSession,
    company_id: Optional[int] = None,
    subchannel: Optional[SubchannelLink] = None,
) -> None:
    """Pipeline entry point called by the Telegram payload handler.

    Steps:
      1. Format the messages under their [msg <id> · posted <IST> · edited] headers.
      2. Resolve the source company — authoritative for every action:
         *company_id* (a reply to, or re-post of, its invite post), the channel
         an invite post just joined (*subchannel*), or the subchannel itself.
         A subchannel without a company, or an IGNORED/deleted company, stops here.
      3. Build the schedule context: the source company's events with ids, or
         for a root-feed post the tracked companies it mentions.
      4. Run LLM extraction (raises ExtractionFailed → the batch is retried). An
         invite post's channel is then linked to the extracted company name,
         else to the channel title.
      5. Apply each action: INDIVIDUAL-scope actions are dropped; root-feed
         actions are matched to companies by name within the environment;
         nothing is applied to an IGNORED or deleted company or role. A failed
         action is rolled back without affecting the others.
    """
    # ── 1. Format the message block ──────────────────────────────────────────
    text_block = format_message_block(messages)
    if not text_block:
        logger.debug("Empty text block for chat_id=%s — skipping.", chat_id)
        return
    batch = _Batch.of(chat_id, messages)

    # ── 2. Resolve the source company ────────────────────────────────────────
    source_company: Optional[models.Company] = None
    authoritative = False
    if company_id is not None:
        source_company = await db.get(models.Company, company_id)
        if source_company is None:
            logger.warning("Company id=%s for chat_id=%s no longer exists — skipping.", company_id, chat_id)
            return
        authoritative = True
    elif subchannel is not None:
        source_company = await crud.get_company_by_subchannel_id(db, subchannel.chat_id)
        authoritative = True
    elif context_header in ("subchannel", "backfill"):
        source_company = await crud.get_company_by_subchannel_id(db, chat_id)
        if source_company is None:
            logger.warning("Subchannel chat_id=%s is not linked to a company — skipping.", chat_id)
            return
        authoritative = True

    if source_company is not None and not await crud.is_company_trackable(db, source_company.id):
        logger.info("Skipped chat_id=%s: company=%s is IGNORED or deleted.", chat_id, source_company.name)
        return

    # ── 3. Schedule context ─────────────────────────────────────────────────
    environment = source_company.environment_type if source_company else _feed_environment(context_header)
    mentioned = [] if authoritative else await _companies_mentioned(db, text_block, environment)
    current_context = await _build_context(db, chat_id, source_company, mentioned)

    # ── 4. LLM extraction ───────────────────────────────────────────────────
    llm_result = await extractor.extract_placement_actions(text_block, current_context)

    # An invite post's channel is linked to the company the post is about.
    if subchannel is not None and source_company is None:
        source_company, newly_linked = await crud.link_or_create_subchannel_company(
            db,
            subchannel_id=subchannel.chat_id,
            company_name=llm_result.company_name or subchannel.title,
            environment_type=subchannel.environment,
            fallback_name=subchannel.title,
            invite_token=subchannel.invite_token,
            main_feed_msg_id=subchannel.main_feed_msg_id,
        )
        logger.info(
            "Linked subchannel_id=%s to company=%s (env=%s, newly linked=%s)",
            subchannel.chat_id, source_company.name, source_company.environment_type, newly_linked,
        )
        if not await crud.is_company_trackable(db, source_company.id):
            logger.info("Company=%s is IGNORED or deleted: its invite post changes nothing.", source_company.name)
            return

    if not llm_result.actions:
        logger.debug("No actions extracted for chat_id=%s.", chat_id)
        return

    # ── 5. Apply the actions ─────────────────────────────────────────────────
    # Companies are re-loaded by id for every action: a rollback after a failed
    # action expires every loaded object.
    source_company_id = source_company.id if source_company is not None else None
    only_mentioned_id = mentioned[0].id if len(mentioned) == 1 else None
    named: Dict[str, models.Company] = {}
    for extracted in llm_result.actions:
        # Drop individual-scoped events — they must never mutate the cohort timeline
        if extracted.audience_scope == "INDIVIDUAL":
            logger.info("Dropped INDIVIDUAL action=%s for chat_id=%s", extracted.action, chat_id)
            continue
        if extracted.action == "NO_ACTION":
            continue

        company: Optional[models.Company] = None
        if authoritative:
            company = await db.get(models.Company, source_company_id)
        else:
            name = extracted.company_name or llm_result.company_name
            if name:
                company = await _company_named(db, name, environment, named)
            elif only_mentioned_id is not None:
                company = await db.get(models.Company, only_mentioned_id)
        if company is None:
            logger.warning("Company unresolved for action=%s chat_id=%s — skipping.", extracted.action, chat_id)
            continue
        company_name = company.name
        if not await crud.is_company_trackable(db, company.id):
            logger.info("Dropped action=%s for company=%s: IGNORED or deleted.", extracted.action, company_name)
            continue

        try:
            await _apply(db, batch, company, extracted)
        except Exception:
            await db.rollback()
            named.clear()
            logger.exception(
                "Unhandled error executing action=%s for company=%s",
                extracted.action, company_name,
            )
