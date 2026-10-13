"""
app/ai/schemas.py
Strict Pydantic v2 models for LLM structured extraction output.

These schemas represent the contract between the prompt and the state-machine
dispatcher. The LLM must emit JSON that validates against LLMExtractionResult.
"""
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Enum-style literals — used in ExtractedEvent and downstream dispatch logic
# ---------------------------------------------------------------------------

AudienceScope = Literal["COHORT", "INDIVIDUAL"]
"""COHORT = cohort-wide announcement.  INDIVIDUAL = personal message (drop it)."""

DriveType = Literal["FTE", "INTERNSHIP", "COMBINED"]
"""Classification of the placement drive referenced in the message."""

ActionType = Literal[
    "CREATE_EVENT",
    "EXTEND_DEADLINE",
    "RESCHEDULE_EVENT",
    "REPLACE_FORM",
    "STATUS_NOTE",
    "NO_ACTION",
]
"""State machine verb the dispatcher will execute for this extraction."""


# ---------------------------------------------------------------------------
# Core extraction models
# ---------------------------------------------------------------------------

class ExtractedEvent(BaseModel):
    """A single actionable placement event extracted from a message block."""

    action: ActionType

    company_name: Optional[str] = Field(
        default=None,
        description="Company name if extracted per-event.",
    )
    role_name: Optional[str] = Field(
        default=None,
        description="Specific role targeted (e.g. 'DAA', 'BTSA', 'Software Engineer'). "
                    "None means applies to all roles for the company.",
    )
    drive_type: Optional[DriveType] = "FTE"
    audience_scope: Optional[AudienceScope] = "COHORT"

    event_type: Optional[str] = Field(
        default="CUSTOM",
        description="One of: IRIS_DEADLINE, FORM_SUBMISSION, PPT, OA_WINDOW, INTERVIEW, CUSTOM.",
    )
    title: Optional[str] = Field(
        default=None,
        description="Human-readable title to store in the TimelineEvent.title column.",
    )
    action_link: Optional[str] = Field(
        default=None,
        description="URL to associate with the event (form link, OA portal, etc.).",
    )

    @field_validator("drive_type", mode="before")
    @classmethod
    def default_drive_type(cls, v):
        return "FTE" if v is None else v

    @field_validator("audience_scope", mode="before")
    @classmethod
    def default_audience_scope(cls, v):
        return "COHORT" if v is None else v

    @field_validator("event_type", mode="before")
    @classmethod
    def default_event_type(cls, v):
        return "CUSTOM" if not v else v

    @model_validator(mode="after")
    def default_title_to_event_type(self) -> "ExtractedEvent":
        if not self.title or not self.title.strip():
            etype = self.event_type or "CUSTOM"
            self.title = etype.replace("_", " ").title()
        return self

    # All datetimes must be UTC-aware; the LLM is instructed to emit ISO-8601 + Z.
    start_time: Optional[datetime] = Field(
        default=None,
        description="Event start time in UTC (ISO-8601). None for point-in-time deadlines.",
    )
    end_time: Optional[datetime] = Field(
        default=None,
        description="Deadline / event end time in UTC (ISO-8601). Required for time-bound actions.",
    )

    status_note: Optional[str] = Field(
        default=None,
        description="Free-text status update for STATUS_NOTE actions (e.g. 'Shortlists delayed by 2 days').",
    )


class LLMExtractionResult(BaseModel):
    """Top-level container returned by the LLM for a single message block."""

    company_name: Optional[str] = Field(
        default=None,
        description="Company name as it appears in the message; used for DB lookup fallback.",
    )
    actions: List[ExtractedEvent] = Field(
        default_factory=list,
        description="Ordered list of extracted state-machine actions.",
    )
