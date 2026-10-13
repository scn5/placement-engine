"""
app/ai/dispatcher.py
State-machine dispatcher: resolves Company, builds context, calls the LLM
extractor, then executes the appropriate CRUD mutations per extracted action.

Entry point: process_and_dispatch(chat_id, context_header, messages, db)
Called by the Telegram payload handler registered in app/main.py lifespan.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models
from app import schemas as app_schemas
from app.ai import extractor
from app.ai.schemas import ExtractedEvent, LLMExtractionResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Company resolution
# ---------------------------------------------------------------------------


async def _find_company_by_subchannel(
    db: AsyncSession, chat_id: int
) -> Optional[models.Company]:
    return await crud.get_company_by_subchannel_id(db, chat_id)


async def _find_company_by_name(
    db: AsyncSession, name: str
) -> Optional[models.Company]:
    normalized = name.strip().lower()
    result = await db.execute(
        select(models.Company).where(models.Company.normalized_name == normalized)
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Schedule context builder
# ---------------------------------------------------------------------------


async def _build_context(
    db: AsyncSession, company: Optional[models.Company]
) -> dict:
    """Return the current schedule context dict for prompt injection."""
    if company is None:
        return {}

    result = await db.execute(
        select(models.TimelineEvent)
        .where(models.TimelineEvent.company_id == company.id)
        .order_by(models.TimelineEvent.end_time.asc())
    )
    events = result.scalars().all()

    return {
        "company_name": company.name,
        "company_id": company.id,
        "events": [
            {
                "id": e.id,
                "title": e.title,
                "event_type": e.event_type,
                "end_time": (
                    e.end_time.isoformat()
                    if e.end_time
                    else "unknown"
                ),
                "user_status": e.user_status,
            }
            for e in events
        ],
    }


# ---------------------------------------------------------------------------
# Role track resolution
# ---------------------------------------------------------------------------


async def _find_role_track(
    db: AsyncSession,
    company_id: int,
    role_name: Optional[str],
) -> Optional[models.RoleTrack]:
    stmt = select(models.RoleTrack).where(
        models.RoleTrack.company_id == company_id
    )
    if role_name:
        stmt = stmt.where(models.RoleTrack.role_name.ilike(f"%{role_name}%"))
    result = await db.execute(stmt)
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Matching event for mutations
# ---------------------------------------------------------------------------


async def _find_matching_event(
    db: AsyncSession,
    company_id: int,
    event_type: Optional[str],
    role_track_id: Optional[int],
) -> Optional[models.TimelineEvent]:
    """Return the most recent event matching the given criteria."""
    stmt = (
        select(models.TimelineEvent)
        .where(models.TimelineEvent.company_id == company_id)
        .order_by(models.TimelineEvent.end_time.desc())
    )
    if event_type:
        stmt = stmt.where(models.TimelineEvent.event_type == event_type)
    if role_track_id is not None:
        stmt = stmt.where(models.TimelineEvent.role_track_id == role_track_id)
    result = await db.execute(stmt)
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Per-action executors
# ---------------------------------------------------------------------------


async def _execute_action(
    db: AsyncSession,
    company: models.Company,
    extracted: ExtractedEvent,
) -> None:
    """Execute a single state-machine mutation for one extracted action."""
    action = extracted.action
    role_track = await _find_role_track(db, company.id, extracted.role_name)
    role_track_id = role_track.id if role_track else None

    # ── CREATE_EVENT ────────────────────────────────────────────────────────
    if action == "CREATE_EVENT":
        if not extracted.end_time:
            logger.warning(
                "CREATE_EVENT skipped for company=%s: no end_time extracted.",
                company.name,
            )
            return
        event_in = app_schemas.TimelineEventCreate(
            company_id=company.id,
            role_track_id=role_track_id,
            event_type=extracted.event_type or "CUSTOM",
            title=(
                extracted.title
                or f"{company.name} — {extracted.event_type or 'Event'}"
            ),
            action_link=extracted.action_link,
            start_time=extracted.start_time,
            end_time=extracted.end_time,
            is_custom=True,
        )
        new_ev = await crud.create_custom_event(db, event_in)
        logger.info(
            "CREATE_EVENT: id=%s company=%s type=%s",
            new_ev.id, company.name, new_ev.event_type,
        )

    # ── EXTEND_DEADLINE ─────────────────────────────────────────────────────
    elif action == "EXTEND_DEADLINE":
        if not extracted.end_time:
            logger.warning(
                "EXTEND_DEADLINE skipped for company=%s: no end_time.", company.name
            )
            return
        target = await _find_matching_event(
            db, company.id, extracted.event_type, role_track_id
        )
        if target:
            updated = await crud.extend_event_deadline(db, target.id, extracted.end_time)
            logger.info(
                "EXTEND_DEADLINE: event=%s company=%s new_end=%s display=%s",
                target.id, company.name, extracted.end_time,
                updated.display_state if updated else "unknown",
            )
        else:
            logger.warning(
                "EXTEND_DEADLINE: no matching event for company=%s type=%s",
                company.name, extracted.event_type,
            )

    # ── RESCHEDULE_EVENT ────────────────────────────────────────────────────
    elif action == "RESCHEDULE_EVENT":
        if not extracted.end_time:
            logger.warning(
                "RESCHEDULE_EVENT skipped for company=%s: no end_time.", company.name
            )
            return
        target = await _find_matching_event(
            db, company.id, extracted.event_type, role_track_id
        )
        if target:
            updated = await crud.reschedule_event(
                db, target.id, extracted.end_time, extracted.start_time
            )
            logger.info(
                "RESCHEDULE_EVENT: event=%s company=%s start=%s end=%s",
                target.id, company.name, extracted.start_time, extracted.end_time,
            )
        else:
            logger.warning(
                "RESCHEDULE_EVENT: no matching event for company=%s type=%s",
                company.name, extracted.event_type,
            )

    # ── REPLACE_FORM ────────────────────────────────────────────────────────
    elif action == "REPLACE_FORM":
        if not extracted.end_time or not extracted.action_link:
            logger.warning(
                "REPLACE_FORM skipped for company=%s: missing end_time or action_link.",
                company.name,
            )
            return
        payload = app_schemas.ReplaceFormPayload(
            new_form_link=extracted.action_link,
            new_end_time=extracted.end_time,
            title=extracted.title or "Google Form Submission",
        )
        new_form = await crud.replace_form_event(
            db, company.id, role_track_id, payload
        )
        logger.info(
            "REPLACE_FORM: new event id=%s version=%s company=%s",
            new_form.id, new_form.version, company.name,
        )

    # ── STATUS_NOTE ─────────────────────────────────────────────────────────
    elif action == "STATUS_NOTE":
        if not extracted.status_note:
            logger.warning(
                "STATUS_NOTE skipped for company=%s: no status_note.", company.name
            )
            return
        target_track = role_track
        if target_track is None:
            # Fallback to primary role track of the resolved company
            primary_res = await db.execute(
                select(models.RoleTrack)
                .where(models.RoleTrack.company_id == company.id)
                .order_by(models.RoleTrack.id.asc())
            )
            target_track = primary_res.scalars().first()

        if target_track:
            await db.execute(
                update(models.RoleTrack)
                .where(models.RoleTrack.id == target_track.id)
                .values(latest_status_note=extracted.status_note)
            )
            await db.commit()
            logger.info(
                "STATUS_NOTE: company=%s role=%s note=%r",
                company.name, target_track.role_name, extracted.status_note,
            )
        else:
            # Company has no role tracks at all; create default track to persist note
            new_track = models.RoleTrack(
                company_id=company.id,
                role_name="Default Role",
                drive_type=extracted.drive_type or "FTE",
                tracking_mode="SILENT_TRACK",
                is_triaged=False,
                latest_status_note=extracted.status_note,
            )
            db.add(new_track)
            await db.commit()
            logger.info(
                "STATUS_NOTE: created default track for company=%s note=%r",
                company.name, extracted.status_note,
            )

    # ── NO_ACTION ───────────────────────────────────────────────────────────
    elif action == "NO_ACTION":
        logger.debug("NO_ACTION for company=%s", company.name)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def process_and_dispatch(
    chat_id: int,
    context_header: str,
    messages: List[dict],
    db: AsyncSession,
) -> None:
    """Pipeline entry point called by the Telegram payload handler.

    Steps:
      1. Normalize the raw message block into a single text string.
      2. Resolve Company by subchannel_id; fall back to LLM-extracted name.
      3. Build current schedule context for prompt temporal injection.
      4. Run LLM extraction → LLMExtractionResult.
      5. Execute state-machine mutations, silently dropping INDIVIDUAL-scope events.
    """
    # ── 1. Normalize text ───────────────────────────────────────────────────
    parts = []
    for m in messages:
        text = (m.get("text") or "").strip()
        if text:
            label = m.get("label", "")
            parts.append(f"{label} {text}".strip() if label else text)

    text_block = "\n".join(parts).strip()
    if not text_block:
        logger.debug("Empty text block for chat_id=%s — skipping.", chat_id)
        return

    # ── 2. Resolve company by subchannel_id ─────────────────────────────────
    company: Optional[models.Company] = await _find_company_by_subchannel(db, chat_id)

    # ── 3. Build schedule context (before LLM call so context is current) ───
    current_context = await _build_context(db, company)

    # ── 4. LLM extraction ───────────────────────────────────────────────────
    llm_result: LLMExtractionResult = await extractor.extract_placement_actions(
        text_block, current_context
    )

    # Fallback name resolution or creation if subchannel isn't mapped yet
    if company is None and llm_result.company_name:
        company = await _find_company_by_name(db, llm_result.company_name)
        if company is not None:
            if company.subchannel_id is None and context_header in ("subchannel", "backfill"):
                company.subchannel_id = chat_id
                await db.commit()
                await db.refresh(company)
        else:
            # Defensive check: ensure subchannel is not already linked before creating new
            existing_sub = await crud.get_company_by_subchannel_id(db, chat_id)
            if existing_sub is not None:
                company = existing_sub
            else:
                env_type = "TEST" if "test" in context_header.lower() else "MAIN"
                try:
                    company = models.Company(
                        name=llm_result.company_name.strip(),
                        normalized_name=llm_result.company_name.strip().lower(),
                        subchannel_id=chat_id if context_header in ("subchannel", "backfill") else None,
                        tracking_mode="SILENT_TRACK",
                        environment_type=env_type,
                        is_custom=False,
                    )
                    db.add(company)
                    await db.flush()

                    role_track = models.RoleTrack(
                        company_id=company.id,
                        role_name="Default Role",
                        drive_type="FTE",
                        tracking_mode="SILENT_TRACK",
                        is_triaged=False,
                        lifecycle_status="APPLICATION_WINDOW",
                    )
                    db.add(role_track)
                    await db.commit()
                    await db.refresh(company)
                    logger.info(
                        "Created new company=%s for chat_id=%s env=%s (is_triaged=False)",
                        company.name,
                        chat_id,
                        env_type,
                    )
                except Exception:
                    await db.rollback()
                    company = await crud.get_company_by_subchannel_id(db, chat_id)
                    if company is None:
                        company = await _find_company_by_name(db, llm_result.company_name)

    if company is None:
        # Attempt fallback to parent Company via subchannel_id
        company = await crud.get_company_by_subchannel_id(db, chat_id)

    if company is None and not any(getattr(a, "company_name", None) for a in llm_result.actions):
        logger.warning(
            "Company unresolved for chat_id=%s (LLM hint: %r) — aborting dispatch.",
            chat_id, llm_result.company_name,
        )
        return

    if not llm_result.actions:
        logger.debug("No actions extracted for company=%s.", company.name if company else "unknown")
        return

    # ── 5. Execute state machine ─────────────────────────────────────────────
    for extracted in llm_result.actions:
        # Drop individual-scoped events — they must never mutate the cohort timeline
        if extracted.audience_scope == "INDIVIDUAL":
            logger.info(
                "Dropped INDIVIDUAL event: action=%s company=%s",
                extracted.action, company.name if company else "unknown",
            )
            continue

        target_company = company
        extracted_comp_name = getattr(extracted, "company_name", None)
        if extracted_comp_name:
            resolved = await _find_company_by_name(db, extracted_comp_name)
            if resolved:
                target_company = resolved
        else:
            # Subchannel Context Fallback: when company_name is null, look up parent Company using chat_id
            if target_company is None:
                target_company = await crud.get_company_by_subchannel_id(db, chat_id)

        if target_company is None:
            logger.warning(
                "Company unresolved for action=%s chat_id=%s — skipping.",
                extracted.action, chat_id,
            )
            continue

        try:
            await _execute_action(db, target_company, extracted)
        except Exception:
            logger.exception(
                "Unhandled error executing action=%s for company=%s",
                extracted.action, target_company.name,
            )
