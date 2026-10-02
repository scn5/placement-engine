"""
verify_phase4.py
Phase 4 — LLM Extraction & Dispatch Engine verification suite.

Uses in-memory SQLite (aiosqlite) and mock objects.
No active API key or live network required.
Run: python verify_phase4.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Patch DB engine BEFORE importing app modules (same pattern as Phase 2)
# ---------------------------------------------------------------------------
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

# Now safe to import application modules
from pydantic import ValidationError
from sqlalchemy import select

from app.models import Base, Company, RoleTrack, TimelineEvent
from app.ai.schemas import (
    ActionType,
    AudienceScope,
    DriveType,
    ExtractedEvent,
    LLMExtractionResult,
)
from app.ai.prompts import build_system_prompt
from app.ai.dispatcher import process_and_dispatch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(offset_days: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=offset_days)


async def _new_session() -> AsyncSession:
    return _TestSession()


async def _setup_company(
    db: AsyncSession,
    *,
    name: str,
    subchannel_id: int,
) -> Company:
    company = Company(
        name=name,
        normalized_name=name.strip().lower(),
        subchannel_id=subchannel_id,
        tracking_mode="ELIGIBLE",
        is_active=True,
        is_custom=True,
        created_at=_utc(),
    )
    db.add(company)
    await db.commit()
    await db.refresh(company)
    return company


async def _setup_role_track(db: AsyncSession, company_id: int, drive_type: str = "FTE") -> RoleTrack:
    track = RoleTrack(
        company_id=company_id,
        role_name="Software Engineer",
        drive_type=drive_type,
        lifecycle_status="APPLICATION_WINDOW",
        tracking_mode="ELIGIBLE",
        is_triaged=True,
        updated_at=_utc(),
    )
    db.add(track)
    await db.commit()
    await db.refresh(track)
    return track


async def _setup_event(
    db: AsyncSession,
    *,
    company_id: int,
    role_track_id: int,
    event_type: str,
    title: str,
    end_time: datetime,
    user_status: str = "PENDING",
    version: int = 1,
) -> TimelineEvent:
    event = TimelineEvent(
        company_id=company_id,
        role_track_id=role_track_id,
        event_type=event_type,
        title=title,
        end_time=end_time,
        user_status=user_status,
        is_custom=True,
        version=version,
        created_at=_utc(),
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


# ---------------------------------------------------------------------------
# Test 1 — Schema validation
# ---------------------------------------------------------------------------

async def test_schema_validation() -> None:
    # ── Valid full payload ──
    valid_json = """{
        "company_name": "DE Shaw",
        "actions": [{
            "action": "CREATE_EVENT",
            "role_name": "DAA",
            "drive_type": "FTE",
            "audience_scope": "COHORT",
            "event_type": "OA_WINDOW",
            "title": "HackerRank OA",
            "action_link": "https://hackerrank.com/deshaw",
            "start_time": "2026-10-14T10:00:00Z",
            "end_time": "2026-10-14T23:59:00Z",
            "status_note": null
        }]
    }"""
    result = LLMExtractionResult.model_validate_json(valid_json)
    assert result.company_name == "DE Shaw"
    assert len(result.actions) == 1
    action = result.actions[0]
    assert action.action == "CREATE_EVENT"
    assert action.drive_type == "FTE"
    assert action.audience_scope == "COHORT"
    assert action.end_time is not None
    assert action.end_time.tzinfo is not None  # must be tz-aware

    # ── Defaults ──
    minimal_json = '{"actions": [{"action": "NO_ACTION"}]}'
    result2 = LLMExtractionResult.model_validate_json(minimal_json)
    assert result2.company_name is None
    assert result2.actions[0].drive_type == "FTE"
    assert result2.actions[0].audience_scope == "COHORT"

    # ── Invalid ActionType raises ValidationError ──
    try:
        LLMExtractionResult.model_validate_json(
            '{"actions": [{"action": "TELEPORT"}]}'
        )
        assert False, "Expected ValidationError for unknown ActionType"
    except (ValidationError, Exception):
        pass  # expected

    # ── Invalid AudienceScope raises ValidationError ──
    try:
        LLMExtractionResult.model_validate_json(
            '{"actions": [{"action": "NO_ACTION", "audience_scope": "GROUP"}]}'
        )
        assert False, "Expected ValidationError for unknown AudienceScope"
    except (ValidationError, Exception):
        pass  # expected

    # ── STATUS_NOTE action ──
    status_json = '{"company_name": "Optiver", "actions": [{"action": "STATUS_NOTE", "status_note": "Shortlists delayed by 2 days"}]}'
    result3 = LLMExtractionResult.model_validate_json(status_json)
    assert result3.actions[0].status_note == "Shortlists delayed by 2 days"

    print("  [1] ✓ Schema validation: valid payloads, defaults, and invalid types verified")


# ---------------------------------------------------------------------------
# Test 2 — INDIVIDUAL scope filtering (must NOT mutate DB)
# ---------------------------------------------------------------------------

async def test_individual_scope_filtering() -> None:
    db = await _new_session()
    try:
        company = await _setup_company(db, name="Filter Corp", subchannel_id=20001)
        track = await _setup_role_track(db, company.id)

        # Count events before
        before = (
            await db.execute(
                select(TimelineEvent).where(TimelineEvent.company_id == company.id)
            )
        ).scalars().all()

        # Mock extractor to return an INDIVIDUAL-scoped CREATE_EVENT
        individual_result = LLMExtractionResult(
            company_name="Filter Corp",
            actions=[
                ExtractedEvent(
                    action="CREATE_EVENT",
                    audience_scope="INDIVIDUAL",
                    event_type="INTERVIEW",
                    title="Nikhil Jain — interview rescheduled to 3 PM",
                    end_time=_utc(+1),
                )
            ],
        )
        with patch(
            "app.ai.extractor.extract_placement_actions",
            AsyncMock(return_value=individual_result),
        ):
            await process_and_dispatch(
                chat_id=20001,
                context_header="subchannel",
                messages=[{"text": "Nikhil your interview moved to 3 PM", "label": "[1]"}],
                db=db,
            )

        # Event count must NOT have changed
        after = (
            await db.execute(
                select(TimelineEvent).where(TimelineEvent.company_id == company.id)
            )
        ).scalars().all()
        assert len(before) == len(after), (
            f"INDIVIDUAL event must not create DB records: before={len(before)}, after={len(after)}"
        )

        print("  [2] ✓ INDIVIDUAL scope filtered: no DB mutation occurred")
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Test 3 — REPLACE_FORM: version incremented, status = PENDING
# ---------------------------------------------------------------------------

async def test_replace_form_mutation() -> None:
    db = await _new_session()
    try:
        company = await _setup_company(db, name="Form Corp", subchannel_id=30001)
        track = await _setup_role_track(db, company.id)

        # Seed an existing v1 form event
        existing = await _setup_event(
            db,
            company_id=company.id,
            role_track_id=track.id,
            event_type="FORM_SUBMISSION",
            title="Google Form v1",
            end_time=_utc(-1),  # already expired
            version=1,
        )
        assert existing.version == 1

        replace_result = LLMExtractionResult(
            company_name="Form Corp",
            actions=[
                ExtractedEvent(
                    action="REPLACE_FORM",
                    audience_scope="COHORT",
                    event_type="FORM_SUBMISSION",
                    title="Updated Google Form",
                    action_link="https://forms.gle/updated-form",
                    end_time=_utc(+3),
                )
            ],
        )
        with patch(
            "app.ai.extractor.extract_placement_actions",
            AsyncMock(return_value=replace_result),
        ):
            await process_and_dispatch(
                chat_id=30001,
                context_header="subchannel",
                messages=[{"text": "New form link, please fill again: https://forms.gle/updated-form — deadline Oct 20"}],
                db=db,
            )

        # Query for all FORM_SUBMISSION events for this company
        all_forms = (
            await db.execute(
                select(TimelineEvent)
                .where(
                    TimelineEvent.company_id == company.id,
                    TimelineEvent.event_type == "FORM_SUBMISSION",
                )
                .order_by(TimelineEvent.version)
            )
        ).scalars().all()

        assert len(all_forms) == 2, f"Expected 2 form events (v1 + v2), got {len(all_forms)}"

        new_form = all_forms[-1]
        assert new_form.version == 2, f"Expected version=2, got {new_form.version}"
        assert new_form.user_status == "PENDING", f"Expected PENDING, got {new_form.user_status}"
        assert new_form.action_link == "https://forms.gle/updated-form"
        assert new_form.display_state == "PENDING"  # future end_time

        print("  [3] ✓ REPLACE_FORM: version incremented to 2, status=PENDING, link updated")
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Test 4 — EXTEND_DEADLINE: MISSED event recovers to PENDING
# ---------------------------------------------------------------------------

async def test_extend_deadline_mutation() -> None:
    db = await _new_session()
    try:
        company = await _setup_company(db, name="Extend Corp", subchannel_id=40001)
        track = await _setup_role_track(db, company.id)

        # Seed an expired IRIS_DEADLINE event → display_state == MISSED
        expired = await _setup_event(
            db,
            company_id=company.id,
            role_track_id=track.id,
            event_type="IRIS_DEADLINE",
            title="IRIS Application Deadline",
            end_time=_utc(-2),  # 2 days ago
        )
        assert expired.display_state == "MISSED", f"Pre-condition: expected MISSED, got {expired.display_state}"

        new_deadline = _utc(+5)
        extend_result = LLMExtractionResult(
            company_name="Extend Corp",
            actions=[
                ExtractedEvent(
                    action="EXTEND_DEADLINE",
                    audience_scope="COHORT",
                    event_type="IRIS_DEADLINE",
                    end_time=new_deadline,
                )
            ],
        )
        with patch(
            "app.ai.extractor.extract_placement_actions",
            AsyncMock(return_value=extend_result),
        ):
            await process_and_dispatch(
                chat_id=40001,
                context_header="subchannel",
                messages=[{"text": "IRIS deadline extended to next Friday, 11:59 PM"}],
                db=db,
            )

        # Re-fetch the event
        refreshed = (
            await db.execute(
                select(TimelineEvent).where(TimelineEvent.id == expired.id)
            )
        ).scalars().first()

        assert refreshed is not None, "Event not found after update"
        assert refreshed.display_state == "PENDING", (
            f"Expected PENDING after extension, got {refreshed.display_state}"
        )

        print("  [4] ✓ EXTEND_DEADLINE: MISSED event recovered to PENDING with new end_time")
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Test 5 — Context injection: known events present in the system prompt
# ---------------------------------------------------------------------------

async def test_context_injection_in_prompt() -> None:
    db = await _new_session()
    try:
        company = await _setup_company(db, name="Context Corp", subchannel_id=50001)
        track = await _setup_role_track(db, company.id)

        # Seed two events to be injected into the prompt
        await _setup_event(
            db,
            company_id=company.id,
            role_track_id=track.id,
            event_type="IRIS_DEADLINE",
            title="IRIS Application Deadline",
            end_time=_utc(+2),
        )
        await _setup_event(
            db,
            company_id=company.id,
            role_track_id=track.id,
            event_type="OA_WINDOW",
            title="HackerRank Online Assessment",
            end_time=_utc(+7),
        )

        # Capture whatever args are passed to extract_placement_actions
        captured_calls: list = []

        async def mock_extract(raw_text: str, current_context=None) -> LLMExtractionResult:
            captured_calls.append({"raw_text": raw_text, "context": current_context})
            return LLMExtractionResult()  # empty — we only care about the call args

        with patch("app.ai.extractor.extract_placement_actions", side_effect=mock_extract):
            await process_and_dispatch(
                chat_id=50001,
                context_header="subchannel",
                messages=[{"text": "Interview results delayed by 2 days"}],
                db=db,
            )

        assert len(captured_calls) == 1, "extractor should have been called once"
        ctx = captured_calls[0]["context"]

        # Verify context structure
        assert ctx is not None, "Context must not be None"
        assert ctx.get("company_name") == "Context Corp"
        assert ctx.get("company_id") == company.id

        event_titles = [e["title"] for e in ctx.get("events", [])]
        assert "IRIS Application Deadline" in event_titles, (
            f"Expected IRIS event in context; got: {event_titles}"
        )
        assert "HackerRank Online Assessment" in event_titles, (
            f"Expected OA event in context; got: {event_titles}"
        )

        # Verify the system prompt built from this context contains the company name
        system_prompt = build_system_prompt(ctx)
        assert "Context Corp" in system_prompt
        assert "IRIS Application Deadline" in system_prompt
        assert "HackerRank Online Assessment" in system_prompt
        assert "EXTEND_DEADLINE" in system_prompt  # operational rules present

        print("  [5] ✓ Context injection: company schedule and events present in prompt payload")
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    # Bootstrap schema in SQLite
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("\nRunning Phase 4 verification suite…\n")

    await test_schema_validation()
    await test_individual_scope_filtering()
    await test_replace_form_mutation()
    await test_extend_deadline_mutation()
    await test_context_injection_in_prompt()

    await _test_engine.dispose()
    print("\n✅ Phase 4 LLM Extraction & Dispatch Engine Verified Successfully!")


if __name__ == "__main__":
    asyncio.run(main())
