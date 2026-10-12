from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# TimelineEvent Schemas
# ---------------------------------------------------------------------------
class TimelineEventBase(BaseModel):
    event_type: str
    title: str
    action_link: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: datetime


class TimelineEventCreate(TimelineEventBase):
    company_id: int
    role_track_id: Optional[int] = None
    is_custom: bool = False


class TimelineEventResponse(TimelineEventBase):
    id: int
    company_id: int
    role_track_id: Optional[int] = None
    user_status: str
    display_state: str
    company_name: str = ""
    drive_type: str = "FTE"
    environment_type: str = "MAIN"
    status: str = "PENDING"
    completed_at: Optional[datetime] = None
    is_custom: bool
    version: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# RoleTrack Schemas
# ---------------------------------------------------------------------------
class RoleTrackBase(BaseModel):
    role_name: str
    drive_type: str = "FTE"
    ctc_stipend_info: Optional[str] = None
    tracking_mode: str = "SILENT_TRACK"
    is_triaged: bool = False


class RoleTrackCreate(RoleTrackBase):
    company_id: int


class RoleTrackResponse(RoleTrackBase):
    id: int
    company_id: int
    lifecycle_status: str
    latest_status_note: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Company Schemas
# ---------------------------------------------------------------------------
class CompanyBase(BaseModel):
    name: str
    tracking_mode: str = "ELIGIBLE"
    environment_type: str = "MAIN"


class CompanyCreate(CompanyBase):
    is_custom: bool = False
    role_name: Optional[str] = "Default Role"
    drive_type: Optional[str] = "FTE"
    ctc_stipend_info: Optional[str] = None
    environment_type: Optional[str] = "MAIN"


class CompanyResponse(CompanyBase):
    id: int
    normalized_name: str
    main_feed_msg_id: Optional[int] = None
    subchannel_id: Optional[int] = None
    is_active: bool
    is_custom: bool
    created_at: datetime
    role_tracks: List[RoleTrackResponse] = []

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Action Schemas
# ---------------------------------------------------------------------------
class ToggleTrackingPayload(BaseModel):
    tracking_mode: Literal["ELIGIBLE", "SILENT_TRACK", "IGNORED"]


class ExtendDeadlinePayload(BaseModel):
    new_end_time: datetime


class ReplaceFormPayload(BaseModel):
    new_form_link: str
    new_end_time: datetime
    title: Optional[str] = "Google Form Submission"


# ---------------------------------------------------------------------------
# Triage Schemas
# ---------------------------------------------------------------------------
class TriageItemResponse(BaseModel):
    id: int
    company_name: str
    role_name: str
    drive_type: str
    environment_type: str = "MAIN"
    ctc_stipend_info: Optional[str] = None
    latest_status_note: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TriageActionPayload(BaseModel):
    action: Literal["ELIGIBLE", "SILENT_TRACK", "IGNORED"]


# ---------------------------------------------------------------------------
# SystemConfig Schemas
# ---------------------------------------------------------------------------
class SystemConfigResponse(BaseModel):
    id: int
    student_name: Optional[str] = None
    roll_number: Optional[str] = None
    personal_email: Optional[str] = None
    college_email: Optional[str] = None
    main_feed_id: Optional[int] = None
    test_feed_id: Optional[int] = None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SystemConfigUpdate(BaseModel):
    student_name: Optional[str] = None
    roll_number: Optional[str] = None
    personal_email: Optional[str] = None
    college_email: Optional[str] = None
    main_feed_id: Optional[int] = None
    test_feed_id: Optional[int] = None
