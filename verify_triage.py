"""
verify_triage.py
Comprehensive verification test suite for Triage Hub backend logic and role-level tracking filters.

Tests:
  1. RoleTrack schema defaults (tracking_mode="SILENT_TRACK", is_triaged=False, created_at set).
  2. GET /triage/pending query returns untriaged role tracks joined with Company.name.
  3. POST /triage/{role_track_id} accepts TriageActionPayload, mutates tracking_mode, and sets is_triaged=True.
  4. GET /events/timeline strictly requires RoleTrack.tracking_mode == 'ELIGIBLE'.
  5. Re-triage logic (invariant 6): only a CRITERIA_RELAXATION note re-opens a SILENT_TRACK role;
     GENERAL notes and ELIGIBLE roles never re-open Triage; IGNORED roles are never touched.
  6. Endpoint verification via FastAPI test client (HTTP 200, 404).

Run: python verify_triage.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Override database engine for tests
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

from sqlalchemy import select
import warnings

from app import crud, models, schemas
from app.models import Base, NaiveDatetimeWarning
from app.routers.triage import flag_for_retriage

# Every datetime handed to the ORM must be timezone-aware.
warnings.simplefilter("error", NaiveDatetimeWarning)


def utc(offset_days: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=offset_days)


async def test_triage_flow() -> None:
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with _TestSession() as db:
        # ── 1. Create Company and newly discovered RoleTracks (default: SILENT_TRACK, is_triaged=False)
        company = await crud.create_company(
            db,
            schemas.CompanyCreate(name="Stripe", tracking_mode="SILENT_TRACK", is_custom=False),
        )
        assert company.tracking_mode == "SILENT_TRACK", f"Expected SILENT_TRACK, got {company.tracking_mode}"

        role_swe = await crud.create_role_track(
            db,
            schemas.RoleTrackCreate(
                company_id=company.id,
                role_name="Software Engineer",
                drive_type="FTE",
                ctc_stipend_info="55 LPA",
            ),
        )
        assert role_swe.tracking_mode == "SILENT_TRACK", f"Expected SILENT_TRACK, got {role_swe.tracking_mode}"
        assert role_swe.is_triaged is False, f"Expected is_triaged=False, got {role_swe.is_triaged}"
        assert role_swe.created_at is not None, "Expected created_at to be populated"

        role_intern = await crud.create_role_track(
            db,
            schemas.RoleTrackCreate(
                company_id=company.id,
                role_name="Software Engineering Intern",
                drive_type="INTERNSHIP",
                ctc_stipend_info="1.25L/month",
            ),
        )
        assert role_intern.tracking_mode == "SILENT_TRACK"
        assert role_intern.is_triaged is False

        role_pm = await crud.create_role_track(
            db,
            schemas.RoleTrackCreate(
                company_id=company.id,
                role_name="Product Manager",
                drive_type="FTE",
                ctc_stipend_info="40 LPA",
            ),
        )
        assert role_pm.tracking_mode == "SILENT_TRACK"
        assert role_pm.is_triaged is False

        print("  [1] ✓ RoleTrack defaults (SILENT_TRACK, is_triaged=False, created_at) verified")

        # ── 2. GET /triage/pending: returns untriaged role tracks joined with company
        pending = await crud.get_pending_triage_items(db)
        pending_ids = [p.id for p in pending]
        assert role_swe.id in pending_ids
        assert role_intern.id in pending_ids
        assert role_pm.id in pending_ids
        assert len(pending) == 3

        # Validate schema serialisation
        item_schema = schemas.TriageItemResponse(
            id=role_swe.id,
            company_name=role_swe.company.name,
            role_name=role_swe.role_name,
            drive_type=role_swe.drive_type,
            ctc_stipend_info=role_swe.ctc_stipend_info,
            latest_status_note=role_swe.latest_status_note,
            created_at=role_swe.created_at,
        )
        assert item_schema.company_name == "Stripe"
        assert item_schema.drive_type == "FTE"
        print("  [2] ✓ Pending triage query & TriageItemResponse schema verified")

        # ── 3. Attach timeline events to roles before triage
        ev_swe = await crud.create_custom_event(
            db,
            schemas.TimelineEventCreate(
                company_id=company.id,
                role_track_id=role_swe.id,
                event_type="OA_WINDOW",
                title="Stripe SWE Coding Round",
                end_time=utc(+2),
            ),
        )
        ev_pm = await crud.create_custom_event(
            db,
            schemas.TimelineEventCreate(
                company_id=company.id,
                role_track_id=role_pm.id,
                event_type="INTERVIEW",
                title="Stripe PM Case Round",
                end_time=utc(+3),
            ),
        )

        # Before triage, neither role is ELIGIBLE, so timeline feed must be EMPTY
        feed_before = await crud.get_timeline_events(db, drive_type="FTE")
        assert len(feed_before) == 0, f"Expected 0 events in timeline before triage, got {len(feed_before)}"
        print("  [3] ✓ Untriaged (SILENT_TRACK) events excluded from timeline feed")

        # ── 4. Triage actions: Accept SWE into ELIGIBLE, Discard PM into IGNORED
        triaged_swe = await crud.triage_role_track(db, role_swe.id, action="ELIGIBLE")
        assert triaged_swe.tracking_mode == "ELIGIBLE"
        assert triaged_swe.is_triaged is True

        # Explicit assertion: parent Company.tracking_mode promoted from SILENT_TRACK to ELIGIBLE
        refreshed_company = (
            await db.execute(select(models.Company).where(models.Company.id == company.id))
        ).scalars().first()
        assert refreshed_company.tracking_mode == "ELIGIBLE", (
            f"Expected parent Company.tracking_mode promoted to ELIGIBLE, got {refreshed_company.tracking_mode}"
        )

        triaged_pm = await crud.triage_role_track(db, role_pm.id, action="IGNORED")
        assert triaged_pm.tracking_mode == "IGNORED"
        assert triaged_pm.is_triaged is True

        # Triage intern explicitly as SILENT_TRACK (user reviewed and opted for silent tracking)
        triaged_intern = await crud.triage_role_track(db, role_intern.id, action="SILENT_TRACK")
        assert triaged_intern.tracking_mode == "SILENT_TRACK"
        assert triaged_intern.is_triaged is True

        # Pending queue should now be empty
        pending_after = await crud.get_pending_triage_items(db)
        assert len(pending_after) == 0, f"Expected 0 pending items, got {len(pending_after)}"

        # Timeline feed must now contain ONLY the ELIGIBLE SWE event
        feed_after = await crud.get_timeline_events(db, drive_type="FTE")
        feed_ids = [e.id for e in feed_after]
        assert ev_swe.id in feed_ids, "ELIGIBLE event must appear in timeline feed"
        assert ev_pm.id not in feed_ids, "IGNORED event must NOT appear in timeline feed"
        print("  [4] ✓ Triage mutations, Company tracking cascade & ELIGIBLE timeline filtering verified")

        # ── 5. Re-triage Logic: flag_for_retriage (PROJECT_STATE §0.1 invariant 6)
        # Test Case A: SILENT_TRACK + GENERAL note -> note stored, role stays triaged
        updated_intern = await flag_for_retriage(db, role_intern.id, "Shortlist expected next week")
        assert updated_intern.latest_status_note == "Shortlist expected next week"
        assert updated_intern.latest_note_kind == "GENERAL"
        assert updated_intern.is_triaged is True, "A GENERAL note must not re-open Triage"

        # Test Case B: SILENT_TRACK + CRITERIA_RELAXATION -> re-opened as a proposal, mode unchanged
        updated_intern = await flag_for_retriage(
            db, role_intern.id, "Criteria relaxed: 6.5 CGPA allowed", note_kind="CRITERIA_RELAXATION"
        )
        assert updated_intern is not None
        assert updated_intern.latest_status_note == "Criteria relaxed: 6.5 CGPA allowed"
        assert updated_intern.latest_note_kind == "CRITERIA_RELAXATION"
        assert updated_intern.is_triaged is False, "SILENT_TRACK role MUST be re-opened on a criteria relaxation"
        assert updated_intern.tracking_mode == "SILENT_TRACK", "tracking_mode must never change automatically"

        # It must now re-appear in pending queue!
        pending_reopened = await crud.get_pending_triage_items(db)
        assert len(pending_reopened) == 1
        assert pending_reopened[0].id == role_intern.id
        assert pending_reopened[0].latest_status_note == "Criteria relaxed: 6.5 CGPA allowed"

        # Test Case C: ELIGIBLE + CRITERIA_RELAXATION -> note updated, stays triaged
        updated_swe = await flag_for_retriage(
            db, role_swe.id, "OA open to all branches", note_kind="CRITERIA_RELAXATION"
        )
        assert updated_swe is not None
        assert updated_swe.latest_status_note == "OA open to all branches"
        assert updated_swe.is_triaged is True, "ELIGIBLE role MUST stay is_triaged=True"

        # Test Case D: IGNORED role -> never touched (invariant 1)
        updated_pm = await flag_for_retriage(
            db, role_pm.id, "PM role open for all branches", note_kind="CRITERIA_RELAXATION"
        )
        assert updated_pm is not None
        assert updated_pm.latest_status_note is None, "An IGNORED role must not be changed"
        assert updated_pm.is_triaged is True and updated_pm.tracking_mode == "IGNORED"

        # Test Case E: Non-existent role track returns None
        not_found = await flag_for_retriage(db, 999999, "Note")
        assert not_found is None

        print("  [5] ✓ Re-triage: only CRITERIA_RELAXATION re-opens SILENT_TRACK (mode unchanged); GENERAL/ELIGIBLE stay triaged; IGNORED untouched")


async def test_fastapi_endpoints() -> None:
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.database import get_db

    async def override_get_db():
        async with _TestSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # Setup isolated test fixtures specifically for endpoint tests
    async with _TestSession() as db:
        co_accept = models.Company(
            name="Endpoint Accept Corp",
            normalized_name="endpoint accept corp",
            tracking_mode="SILENT_TRACK",
            is_active=True,
            is_custom=False,
            created_at=utc(),
        )
        co_ignore = models.Company(
            name="Endpoint Ignore Corp",
            normalized_name="endpoint ignore corp",
            tracking_mode="SILENT_TRACK",
            is_active=True,
            is_custom=False,
            created_at=utc(),
        )
        db.add_all([co_accept, co_ignore])
        await db.flush()

        track_accept = models.RoleTrack(
            company_id=co_accept.id,
            role_name="Accept SDE",
            drive_type="FTE",
            tracking_mode="SILENT_TRACK",
            is_triaged=False,
            created_at=utc(),
        )
        track_ignore = models.RoleTrack(
            company_id=co_ignore.id,
            role_name="Ignore SDE",
            drive_type="FTE",
            tracking_mode="SILENT_TRACK",
            is_triaged=False,
            created_at=utc(),
        )
        db.add_all([track_accept, track_ignore])
        await db.commit()
        await db.refresh(track_accept)
        await db.refresh(track_ignore)

        accept_track_id = track_accept.id
        ignore_track_id = track_ignore.id
        co_accept_id = co_accept.id
        co_ignore_id = co_ignore.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. GET /triage/pending includes our seeded untriaged items
        res = await client.get("/triage/pending")
        assert res.status_code == 200
        items = res.json()
        assert isinstance(items, list)
        pending_ids = [item["id"] for item in items]
        assert accept_track_id in pending_ids
        assert ignore_track_id in pending_ids

        # 2. GET /triage (alias)
        res_alias = await client.get("/triage")
        assert res_alias.status_code == 200
        alias_ids = [item["id"] for item in res_alias.json()]
        assert accept_track_id in alias_ids

        # 3. POST /triage/{id}/accept
        res_accept = await client.post(f"/triage/{accept_track_id}/accept")
        assert res_accept.status_code == 200
        assert res_accept.json()["id"] == accept_track_id

        # Verify acceptance and company cascade in DB
        async with _TestSession() as db:
            refreshed_track = (
                await db.execute(select(models.RoleTrack).where(models.RoleTrack.id == accept_track_id))
            ).scalars().first()
            assert refreshed_track.tracking_mode == "ELIGIBLE"
            assert refreshed_track.is_triaged is True

            refreshed_co = (
                await db.execute(select(models.Company).where(models.Company.id == co_accept_id))
            ).scalars().first()
            assert refreshed_co.tracking_mode == "ELIGIBLE", (
                f"Expected parent company tracking_mode promoted to ELIGIBLE on /accept, got {refreshed_co.tracking_mode}"
            )

        # 4. POST /triage/{id}/ignore
        res_ignore = await client.post(f"/triage/{ignore_track_id}/ignore")
        assert res_ignore.status_code == 200
        assert res_ignore.json()["id"] == ignore_track_id

        # Verify discard status and parent company remains unchanged
        async with _TestSession() as db:
            refreshed_ignored_track = (
                await db.execute(select(models.RoleTrack).where(models.RoleTrack.id == ignore_track_id))
            ).scalars().first()
            assert refreshed_ignored_track.tracking_mode == "IGNORED"
            assert refreshed_ignored_track.is_triaged is True

            refreshed_co_ignore = (
                await db.execute(select(models.Company).where(models.Company.id == co_ignore_id))
            ).scalars().first()
            assert refreshed_co_ignore.tracking_mode != "ELIGIBLE", (
                f"Parent company should NOT be promoted to ELIGIBLE on /ignore, got {refreshed_co_ignore.tracking_mode}"
            )

        # 5. POST /triage/999999 -> 404
        res_404 = await client.post("/triage/999999", json={"action": "ELIGIBLE"})
        assert res_404.status_code == 404

    app.dependency_overrides.clear()
    print("  [6] ✓ FastAPI REST HTTP endpoints (/triage/pending, /accept cascade, /ignore, 404) verified")


async def main() -> None:
    print("\nRunning Triage Hub & Role Tracking Verification Suite…\n")
    try:
        await test_triage_flow()
        await test_fastapi_endpoints()
        print("\n✅ Triage Hub Backend Logic & Role-Level Tracking Verified Successfully!")
    finally:
        await _test_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
