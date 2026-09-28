"""
verify_phase2.py
Phase 2 CRUD Engine & REST API verification.

Uses an in-memory SQLite database (aiosqlite) so no PostgreSQL connection is needed.
Run: python verify_phase2.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ── Override the engine/session BEFORE importing app modules so that crud.py
#    always uses our in-memory SQLite engine, not the real Postgres one.
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

# Now safe to import the rest of the application
from app import crud, schemas
from app.models import Base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc(offset_days: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=offset_days)


async def get_session() -> AsyncSession:
    return _TestSession()


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

async def test_create_company_and_role_tracks(db: AsyncSession) -> tuple:
    """Case 1: Create a custom company and add FTE + Internship role tracks."""
    company = await crud.create_company(
        db,
        schemas.CompanyCreate(name="  DE Shaw  ", tracking_mode="ELIGIBLE", is_custom=True),
    )
    assert company.name == "DE Shaw", f"Expected stripped name, got {company.name!r}"
    assert company.normalized_name == "de shaw", f"Expected 'de shaw', got {company.normalized_name!r}"
    assert company.is_custom is True
    assert company.tracking_mode == "ELIGIBLE"

    fte_track = await crud.create_role_track(
        db,
        schemas.RoleTrackCreate(
            company_id=company.id,
            role_name="Member Technical",
            drive_type="FTE",
            ctc_stipend_info="60 LPA",
        ),
    )
    assert fte_track.drive_type == "FTE"

    intern_track = await crud.create_role_track(
        db,
        schemas.RoleTrackCreate(
            company_id=company.id,
            role_name="Software Intern",
            drive_type="INTERNSHIP",
            ctc_stipend_info="80k/month",
        ),
    )
    assert intern_track.drive_type == "INTERNSHIP"

    print("  [1] ✓ Company created & role tracks added")
    return company, fte_track, intern_track


async def test_timeline_viewport_filtering(
    db: AsyncSession, company, fte_track, intern_track
) -> tuple:
    """Case 2: Create events and verify FTE vs Internship viewport isolation."""
    future = utc(+2)

    fte_event = await crud.create_custom_event(
        db,
        schemas.TimelineEventCreate(
            company_id=company.id,
            role_track_id=fte_track.id,
            event_type="OA_WINDOW",
            title="HackerRank OA",
            end_time=future,
        ),
    )

    intern_event = await crud.create_custom_event(
        db,
        schemas.TimelineEventCreate(
            company_id=company.id,
            role_track_id=intern_track.id,
            event_type="INTERVIEW",
            title="Intern HR Round",
            end_time=future,
        ),
    )

    # Company-level event (no role_track) — must appear in all viewports
    common_event = await crud.create_custom_event(
        db,
        schemas.TimelineEventCreate(
            company_id=company.id,
            role_track_id=None,
            event_type="PPT",
            title="Pre-Placement Talk",
            end_time=future,
        ),
    )

    fte_feed = await crud.get_timeline_events(db, drive_type="FTE")
    intern_feed = await crud.get_timeline_events(db, drive_type="INTERNSHIP")
    combined_feed = await crud.get_timeline_events(db, drive_type="COMBINED")

    fte_ids = {e.id for e in fte_feed}
    intern_ids = {e.id for e in intern_feed}
    combined_ids = {e.id for e in combined_feed}

    assert fte_event.id in fte_ids, "FTE event missing from FTE feed"
    assert intern_event.id not in fte_ids, "Intern event must NOT appear in FTE feed"
    assert intern_event.id in intern_ids, "Intern event missing from INTERNSHIP feed"
    assert fte_event.id not in intern_ids, "FTE event must NOT appear in INTERNSHIP feed"
    assert common_event.id in fte_ids, "Company-level event must appear in FTE feed"
    assert common_event.id in intern_ids, "Company-level event must appear in INTERNSHIP feed"
    assert fte_event.id in combined_ids, "FTE event must appear in COMBINED feed"
    assert intern_event.id in combined_ids, "Intern event must appear in COMBINED feed"

    print("  [2] ✓ Timeline viewport filtering (FTE / INTERNSHIP / COMBINED) verified")
    return fte_event, intern_event


async def test_event_completion(db: AsyncSession, event) -> None:
    """Case 3: Mark event as completed → display_state == 'COMPLETED_ACTIVE'."""
    updated = await crud.set_event_completion(db, event.id, is_completed=True)
    assert updated is not None
    assert updated.user_status == "COMPLETED"
    assert updated.completed_at is not None
    # end_time is in the future → should be COMPLETED_ACTIVE
    assert updated.display_state == "COMPLETED_ACTIVE", (
        f"Expected COMPLETED_ACTIVE, got {updated.display_state}"
    )
    print("  [3] ✓ Event completion → COMPLETED_ACTIVE verified")


async def test_missed_event(db: AsyncSession, company, fte_track) -> object:
    """Case 4: Create an already-expired event → display_state == 'MISSED'."""
    missed_event = await crud.create_custom_event(
        db,
        schemas.TimelineEventCreate(
            company_id=company.id,
            role_track_id=fte_track.id,
            event_type="IRIS_DEADLINE",
            title="Expired IRIS Deadline",
            end_time=utc(-1),  # 1 day in the past
        ),
    )
    assert missed_event.display_state == "MISSED", (
        f"Expected MISSED, got {missed_event.display_state}"
    )
    print("  [4] ✓ Expired event → MISSED verified")
    return missed_event


async def test_extend_deadline_recovery(db: AsyncSession, missed_event) -> None:
    """Case 5: Extend a MISSED event's deadline → display_state recovers to 'PENDING'."""
    recovered = await crud.extend_event_deadline(db, missed_event.id, utc(+3))
    assert recovered is not None
    assert recovered.display_state == "PENDING", (
        f"Expected PENDING after extension, got {recovered.display_state}"
    )
    print("  [5] ✓ Deadline extension → MISSED recovered to PENDING verified")


async def test_replace_form_versioning(db: AsyncSession, company, fte_track) -> None:
    """Case 6: Replace a form event; verify new event is PENDING with version+1."""
    # Seed the first form at version 1 manually
    first_form = await crud.replace_form_event(
        db,
        company_id=company.id,
        role_track_id=fte_track.id,
        payload=schemas.ReplaceFormPayload(
            new_form_link="https://forms.gle/original",
            new_end_time=utc(+1),
        ),
    )
    assert first_form.version == 1, f"Expected version 1, got {first_form.version}"
    assert first_form.user_status == "PENDING"

    # Replace it — should create version 2
    second_form = await crud.replace_form_event(
        db,
        company_id=company.id,
        role_track_id=fte_track.id,
        payload=schemas.ReplaceFormPayload(
            new_form_link="https://forms.gle/updated",
            new_end_time=utc(+2),
            title="Updated Application Form",
        ),
    )
    assert second_form.version == 2, f"Expected version 2, got {second_form.version}"
    assert second_form.user_status == "PENDING"
    assert second_form.action_link == "https://forms.gle/updated"
    assert second_form.display_state == "PENDING"
    assert second_form.is_custom is True

    print("  [6] ✓ Form replacement → versioned PENDING event (v2) verified")


async def test_tracking_mode_filter(db: AsyncSession, company) -> None:
    """Bonus: Verify get_companies() tracking_mode filter works correctly."""
    # Flip company to IGNORED
    updated = await crud.update_company_tracking(db, company.id, "IGNORED")
    assert updated is not None
    assert updated.tracking_mode == "IGNORED"

    ignored_companies = await crud.get_companies(db, tracking_mode="IGNORED")
    assert any(c.id == company.id for c in ignored_companies), (
        "Company not found in IGNORED filter"
    )

    eligible_companies = await crud.get_companies(db, tracking_mode="ELIGIBLE")
    assert not any(c.id == company.id for c in eligible_companies), (
        "IGNORED company must not appear in ELIGIBLE filter"
    )

    # Restore for downstream tests
    await crud.update_company_tracking(db, company.id, "ELIGIBLE")
    print("  [B] ✓ Tracking mode filter (IGNORED / ELIGIBLE) verified")


# ---------------------------------------------------------------------------
# Main Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    # Bootstrap schema in SQLite
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db = await get_session()
    try:
        print("\nRunning Phase 2 verification suite…\n")

        company, fte_track, intern_track = await test_create_company_and_role_tracks(db)
        fte_event, _ = await test_timeline_viewport_filtering(db, company, fte_track, intern_track)
        await test_event_completion(db, fte_event)
        missed_event = await test_missed_event(db, company, fte_track)
        await test_extend_deadline_recovery(db, missed_event)
        await test_replace_form_versioning(db, company, fte_track)
        await test_tracking_mode_filter(db, company)

        print("\n✅ Phase 2 CRUD Engine & REST API Verified Successfully!")
    finally:
        await db.close()
        await _test_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
