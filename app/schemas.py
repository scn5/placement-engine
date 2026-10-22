from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator


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
    # Inputs must carry a UTC offset; a naive time would otherwise be read as UTC.
    start_time: Optional[AwareDatetime] = None
    end_time: AwareDatetime


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

    @model_validator(mode="after")
    def sync_status_fields(self) -> "TimelineEventResponse":
        if self.display_state:
            self.status = self.display_state
        elif self.status:
            self.display_state = self.status
        return self


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
    name: Optional[str] = None
    tracking_mode: str = "ELIGIBLE"
    environment_type: str = "MAIN"


class CompanyCreate(CompanyBase):
    name: Optional[str] = None
    company_name: Optional[str] = None
    is_custom: bool = False
    role_name: Optional[str] = "Default Role"
    target_role: Optional[str] = "Default Role"
    drive_type: Optional[str] = "FTE"
    ctc_stipend_info: Optional[str] = None
    environment_type: Optional[str] = "MAIN"

    @model_validator(mode="before")
    @classmethod
    def resolve_create_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("name") and data.get("company_name"):
                data["name"] = data["company_name"]
            elif data.get("name") and not data.get("company_name"):
                data["company_name"] = data["name"]
            if not data.get("role_name") and data.get("target_role"):
                data["role_name"] = data["target_role"]
            elif data.get("role_name") and not data.get("target_role"):
                data["target_role"] = data["role_name"]
        return data

    @model_validator(mode="after")
    def sync_create_fields(self) -> "CompanyCreate":
        if not self.name and self.company_name:
            self.name = self.company_name
        elif not self.company_name and self.name:
            self.company_name = self.name
        if not self.role_name and self.target_role:
            self.role_name = self.target_role
        elif not self.target_role and self.role_name:
            self.target_role = self.role_name
        return self


class CompanyResponse(CompanyBase):
    id: int
    name: str
    company_name: str = ""
    normalized_name: str
    main_feed_msg_id: Optional[int] = None
    subchannel_id: Optional[int] = None
    is_active: bool
    is_custom: bool
    created_at: datetime
    role_tracks: List[RoleTrackResponse] = []

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def sync_response_names(self) -> "CompanyResponse":
        if not self.company_name and self.name:
            self.company_name = self.name
        elif not self.name and self.company_name:
            self.name = self.company_name
        return self


# ---------------------------------------------------------------------------
# Action Schemas
# ---------------------------------------------------------------------------
class ToggleTrackingPayload(BaseModel):
    tracking_mode: Literal["ELIGIBLE", "SILENT_TRACK", "IGNORED"]


class ExtendDeadlinePayload(BaseModel):
    new_end_time: AwareDatetime


class ReplaceFormPayload(BaseModel):
    new_form_link: str
    new_end_time: AwareDatetime
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
