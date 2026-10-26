"""
app/ai/schemas.py
Strict Pydantic v2 models for LLM structured extraction output.

These schemas represent the contract between the prompt and the state-machine
dispatcher. The LLM must emit JSON that validates against LLMExtractionResult.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from dateutil import parser as date_parser
from pydantic import BaseModel, Field, field_validator, model_validator

_IST = timezone(timedelta(hours=5, minutes=30), name="IST")

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

EventType = Literal["IRIS_DEADLINE", "FORM_SUBMISSION", "PPT", "OA_WINDOW", "INTERVIEW", "CUSTOM"]
"""Canonical event types (the prompt's rule list and the strict JSON schema)."""

Confidence = Literal["HIGH", "MEDIUM", "LOW"]
"""How sure the model is; LOW results are kept but flagged needs_review."""

NoteKind = Literal["CRITERIA_RELAXATION", "GENERAL"]
"""STATUS_NOTE kind. Only CRITERIA_RELAXATION may re-open a SILENT_TRACK role in
Triage (PROJECT_STATE §0.1 invariant 6); a missing kind counts as GENERAL."""


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

    # Datetimes can be ISO strings or datetime objects; naive datetimes are anchored to IST.
    start_time: Optional[datetime] = Field(
        default=None,
        description="Event start time in IST/UTC (ISO-8601). None for point-in-time deadlines.",
    )
    end_time: Optional[datetime] = Field(
        default=None,
        description="Deadline / event end time in IST/UTC (ISO-8601). Required for time-bound actions.",
    )

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def parse_and_localize_datetime(cls, v):
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=_IST)
            return v
        if isinstance(v, str):
            val = v.strip()
            if not val or val.lower() in ("null", "none"):
                return None
            try:
                dt = date_parser.parse(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_IST)
                return dt
            except Exception:
                return None
        return v

    status_note: Optional[str] = Field(
        default=None,
        description="Free-text status update for STATUS_NOTE actions (e.g. 'Shortlists delayed by 2 days').",
    )
    note_kind: Optional[NoteKind] = Field(
        default=None,
        description="STATUS_NOTE only: CRITERIA_RELAXATION when eligibility was widened, else GENERAL.",
    )
    target_event_id: Optional[int] = Field(
        default=None,
        description="Id of the existing event (from the Known Schedule) this action updates; null for a new event.",
    )
    source_message_id: Optional[int] = Field(
        default=None,
        description="Id of the message (from its [msg <id> …] header) the action comes from.",
    )
    confidence: Optional[Confidence] = Field(
        default=None,
        description="HIGH, MEDIUM or LOW; LOW results are kept but flagged for review.",
    )

    @field_validator("note_kind", "confidence", mode="before")
    @classmethod
    def normalise_labels(cls, v, info):
        """Accept any letter case; an unknown label becomes None instead of
        failing the whole batch."""
        if not isinstance(v, str):
            return None
        value = v.strip().upper()
        allowed = ("CRITERIA_RELAXATION", "GENERAL") if info.field_name == "note_kind" else ("HIGH", "MEDIUM", "LOW")
        return value if value in allowed else None


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


# ---------------------------------------------------------------------------
# Strict JSON schema (Groq structured outputs, strict mode)
# ---------------------------------------------------------------------------
# Strict mode decodes only output that matches the schema: every property must
# be listed in "required", every object closed with "additionalProperties":
# false, and optional values are expressed as unions with null. Keep the
# properties in step with ExtractedEvent / LLMExtractionResult —
# verify_extraction.py checks they match.


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


_ACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "action": _enum(ActionType.__args__),
        "company_name": _nullable({"type": "string"}),
        "role_name": _nullable({"type": "string"}),
        "drive_type": _enum(DriveType.__args__),
        "audience_scope": _enum(AudienceScope.__args__),
        "event_type": _nullable(_enum(EventType.__args__)),
        "title": _nullable({"type": "string"}),
        "action_link": _nullable({"type": "string"}),
        "start_time": _nullable({"type": "string", "description": "ISO-8601 with a UTC offset"}),
        "end_time": _nullable({"type": "string", "description": "ISO-8601 with a UTC offset"}),
        "status_note": _nullable({"type": "string"}),
        "note_kind": _nullable(_enum(NoteKind.__args__)),
        "target_event_id": _nullable({"type": "integer"}),
        "source_message_id": _nullable({"type": "integer"}),
        "confidence": _enum(Confidence.__args__),
    },
    "additionalProperties": False,
}
_ACTION_JSON_SCHEMA["required"] = list(_ACTION_JSON_SCHEMA["properties"])

RESULT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": _nullable({"type": "string"}),
        "actions": {"type": "array", "items": _ACTION_JSON_SCHEMA},
    },
    "required": ["company_name", "actions"],
    "additionalProperties": False,
}
