import sys
import warnings
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.database import AsyncSessionLocal, Base, engine, get_db
from app.main import app
from app.models import Company, NaiveDatetimeWarning, RoleTrack, TimelineEvent

# Every datetime handed to the ORM must be timezone-aware.
warnings.simplefilter("error", NaiveDatetimeWarning)
from app.schemas import (
    CompanyCreate,
    CompanyResponse,
    ExtendDeadlinePayload,
    ReplaceFormPayload,
    RoleTrackCreate,
    RoleTrackResponse,
    TimelineEventCreate,
    TimelineEventResponse,
    ToggleTrackingPayload,
)


def run_verification() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    now = datetime.now(timezone.utc)
    future_time = now + timedelta(days=2)
    past_time = now - timedelta(days=2)

    # 1. Verify Config & Database setup
    assert settings.APP_ENV is not None
    assert (
        settings.DATABASE_URL.startswith("postgresql+asyncpg://")
        or settings.DATABASE_URL.startswith("sqlite+aiosqlite://")
    )
    assert engine is not None
    assert AsyncSessionLocal is not None
    assert callable(get_db)
    assert app is not None

    # 2. Instantiate Mock Relational Models
    company = Company(
        id=1,
        name="DE Shaw",
        normalized_name="de shaw",
        main_feed_msg_id=10001,
        subchannel_id=20001,
        tracking_mode="ELIGIBLE",
        is_active=True,
        is_custom=False,
        created_at=now,
    )

    role_track = RoleTrack(
        id=10,
        company_id=company.id,
        role_name="Member Technical",
        drive_type="FTE",
        ctc_stipend_info="60 LPA",
        lifecycle_status="APPLICATION_WINDOW",
        tracking_mode="ELIGIBLE",
        is_triaged=True,
        latest_status_note="Applications open on IRIS",
        updated_at=now,
    )
    company.role_tracks.append(role_track)

    # 3. Verify all 4 display_state branches on TimelineEvent
    event_pending = TimelineEvent(
        id=100,
        company_id=company.id,
        role_track_id=role_track.id,
        event_type="IRIS_DEADLINE",
        title="IRIS Application Deadline",
        action_link="https://iris.nitk.ac.in",
        start_time=now,
        end_time=future_time,
        user_status="PENDING",
    )
    assert event_pending.display_state == "PENDING", f"Expected PENDING, got {event_pending.display_state}"

    event_missed = TimelineEvent(
        id=101,
        company_id=company.id,
        role_track_id=role_track.id,
        event_type="FORM_SUBMISSION",
        title="Google Form Submission",
        end_time=past_time,
        user_status="PENDING",
    )
    assert event_missed.display_state == "MISSED", f"Expected MISSED, got {event_missed.display_state}"

    event_completed_active = TimelineEvent(
        id=102,
        company_id=company.id,
        role_track_id=role_track.id,
        event_type="OA_WINDOW",
        title="HackerRank Online Assessment",
        end_time=future_time,
        user_status="COMPLETED",
        completed_at=now,
    )
    assert (
        event_completed_active.display_state == "COMPLETED_ACTIVE"
    ), f"Expected COMPLETED_ACTIVE, got {event_completed_active.display_state}"

    event_completed_expired = TimelineEvent(
        id=103,
        company_id=company.id,
        role_track_id=role_track.id,
        event_type="PPT",
        title="Pre-Placement Talk",
        end_time=past_time,
        user_status="COMPLETED",
        completed_at=past_time,
    )
    assert (
        event_completed_expired.display_state == "COMPLETED_EXPIRED"
    ), f"Expected COMPLETED_EXPIRED, got {event_completed_expired.display_state}"

    # 4. Validate ORM-to-Pydantic serialization via model_validate
    company_schema = CompanyResponse.model_validate(company)
    assert company_schema.normalized_name == "de shaw"
    assert len(company_schema.role_tracks) == 1
    assert company_schema.role_tracks[0].role_name == "Member Technical"

    role_schema = RoleTrackResponse.model_validate(role_track)
    assert role_schema.id == 10
    assert role_schema.lifecycle_status == "APPLICATION_WINDOW"

    for ev, expected_state in [
        (event_pending, "PENDING"),
        (event_missed, "MISSED"),
        (event_completed_active, "COMPLETED_ACTIVE"),
        (event_completed_expired, "COMPLETED_EXPIRED"),
    ]:
        ev_schema = TimelineEventResponse.model_validate(ev)
        assert ev_schema.display_state == expected_state

    # 5. Validate Create and Action Payload Schemas
    _ = CompanyCreate(name="Google", tracking_mode="ELIGIBLE")
    _ = RoleTrackCreate(
        company_id=1,
        role_name="Software Engineer",
        drive_type="FTE",
        ctc_stipend_info="45 LPA",
        tracking_mode="ELIGIBLE",
    )
    _ = TimelineEventCreate(
        company_id=1,
        role_track_id=10,
        event_type="INTERVIEW",
        title="Round 1 Technical",
        end_time=future_time,
    )
    _ = ToggleTrackingPayload(tracking_mode="SILENT_TRACK")
    _ = ExtendDeadlinePayload(new_end_time=future_time)
    replace_payload = ReplaceFormPayload(
        new_form_link="https://forms.gle/example",
        new_end_time=future_time,
    )
    assert replace_payload.title == "Google Form Submission"

    print("✅ Phase 1 Architecture & Schemas Verified Successfully!")


if __name__ == "__main__":
    run_verification()
