import warnings
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base


class NaiveDatetimeWarning(UserWarning):
    """A naive datetime reached the persistence layer and was assumed to be UTC."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Return *value* as an aware UTC datetime.

    Application code must pass aware datetimes. A naive value is assumed to be
    UTC and raises NaiveDatetimeWarning (the verify suites turn it into an error).
    """
    if value.tzinfo is None:
        warnings.warn(
            f"Naive datetime {value.isoformat()} assumed to be UTC",
            NaiveDatetimeWarning,
            stacklevel=3,
        )
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime, stored as UTC on every backend.

    SQLite keeps no offset (SQLAlchemy writes the wall-clock fields as-is), so
    values are converted to UTC before writing and come back as aware UTC.
    On PostgreSQL the column is TIMESTAMP WITH TIME ZONE.
    """

    impl = DateTime
    cache_ok = True

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("timezone", True)
        super().__init__(*args, **kwargs)

    def process_bind_param(self, value, dialect):
        return None if value is None else as_utc(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:  # SQLite returns the stored UTC wall-clock without an offset
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Company(Base):
    __tablename__ = "companies"
    # A name may exist once per environment (MAIN and TEST stay independent).
    __table_args__ = (UniqueConstraint("normalized_name", "environment_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    main_feed_msg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Invite hash or public username this company's subchannel was joined from.
    invite_token: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    subchannel_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, unique=True, index=True, nullable=True
    )
    tracking_mode: Mapped[str] = mapped_column(
        String(32), default="ELIGIBLE", nullable=False
    )
    environment_type: Mapped[str] = mapped_column(
        String(16), default="MAIN", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    role_tracks: Mapped[List["RoleTrack"]] = relationship(
        "RoleTrack", back_populates="company", cascade="all, delete-orphan"
    )
    timeline_events: Mapped[List["TimelineEvent"]] = relationship(
        "TimelineEvent", back_populates="company", cascade="all, delete-orphan"
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("tracking_mode", "ELIGIBLE")
        kwargs.setdefault("environment_type", "MAIN")
        kwargs.setdefault("is_active", True)
        kwargs.setdefault("is_custom", False)
        kwargs.setdefault("created_at", datetime.now(timezone.utc))
        super().__init__(**kwargs)

    @property
    def company_name(self) -> str:
        """Alias for name for frontend contract compatibility."""
        return self.name


class RoleTrack(Base):
    __tablename__ = "role_tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    role_name: Mapped[str] = mapped_column(String(255), nullable=False)
    drive_type: Mapped[str] = mapped_column(String(32), default="FTE", nullable=False)
    ctc_stipend_info: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tracking_mode: Mapped[str] = mapped_column(
        String(32), default="SILENT_TRACK", nullable=False
    )
    is_triaged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(64), default="APPLICATION_WINDOW", nullable=False
    )
    latest_status_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # CRITERIA_RELAXATION | GENERAL — only relaxations may re-open a SILENT_TRACK role.
    latest_note_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now
    )

    company: Mapped["Company"] = relationship("Company", back_populates="role_tracks")
    timeline_events: Mapped[List["TimelineEvent"]] = relationship(
        "TimelineEvent", back_populates="role_track"
    )

    def __init__(self, **kwargs):
        kwargs.pop("is_targeted_by_user", None)
        kwargs.setdefault("drive_type", "FTE")
        kwargs.setdefault("tracking_mode", "SILENT_TRACK")
        kwargs.setdefault("is_triaged", False)
        kwargs.setdefault("lifecycle_status", "APPLICATION_WINDOW")
        kwargs.setdefault("created_at", datetime.now(timezone.utc))
        kwargs.setdefault("updated_at", datetime.now(timezone.utc))
        super().__init__(**kwargs)


class TimelineEvent(Base):
    __tablename__ = "timeline_events"
    __table_args__ = (
        Index("ix_timeline_events_source", "source_chat_id", "source_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    role_track_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("role_tracks.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    action_link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_time: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    end_time: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    user_status: Mapped[str] = mapped_column(
        String(32), default="PENDING", nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Uncertain extractions are kept but flagged for the user (recall-first).
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Telegram message that last created or updated this event (provenance / deep links).
    source_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    company: Mapped["Company"] = relationship(
        "Company", back_populates="timeline_events"
    )
    role_track: Mapped[Optional["RoleTrack"]] = relationship(
        "RoleTrack", back_populates="timeline_events"
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("user_status", "PENDING")
        kwargs.setdefault("is_custom", False)
        kwargs.setdefault("is_deleted", False)
        kwargs.setdefault("needs_review", False)
        kwargs.setdefault("version", 1)
        kwargs.setdefault("created_at", datetime.now(timezone.utc))
        super().__init__(**kwargs)

    @property
    def display_state(self) -> str:
        now = datetime.now(timezone.utc)
        end = as_utc(self.end_time)
        if self.user_status == "COMPLETED":
            return "COMPLETED_ACTIVE" if now <= end else "COMPLETED_EXPIRED"
        if now > end:
            return "MISSED"
        return "PENDING"

    # -- Response enrichment (async-safe: never triggers a lazy load) ---------
    @property
    def status(self) -> str:
        """Alias of display_state for the mobile client contract."""
        return self.display_state

    @property
    def company_name(self) -> str:
        company = self.__dict__.get("company")
        return company.name if company is not None else ""

    @property
    def drive_type(self) -> str:
        track = self.__dict__.get("role_track")
        return track.drive_type if track is not None else "FTE"

    @property
    def environment_type(self) -> str:
        company = self.__dict__.get("company")
        return (
            company.environment_type
            if company is not None and getattr(company, "environment_type", None)
            else "MAIN"
        )


class SystemConfig(Base):
    __tablename__ = "system_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    student_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    roll_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    personal_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    college_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    main_feed_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    test_feed_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("updated_at", datetime.now(timezone.utc))
        super().__init__(**kwargs)


class MessageLedger(Base):
    """One row per processed Telegram message: idempotency, catch-up cursor
    (max message_id per chat) and retry scheduling for failed extractions."""

    __tablename__ = "message_ledger"
    __table_args__ = (
        CheckConstraint("status IN ('DONE', 'FAILED', 'SKIPPED')", name="status"),
        Index("ix_message_ledger_retry", "status", "next_attempt_at"),
    )

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("attempts", 0)
        kwargs.setdefault("processed_at", datetime.now(timezone.utc))
        super().__init__(**kwargs)


class WorkerStatus(Base):
    """Single-row liveness record written by the ingestion worker and read by
    the API's status endpoint."""

    __tablename__ = "worker_status"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    telegram_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_update_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    last_llm_ok_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    catchup_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)

    def __init__(self, **kwargs):
        kwargs.setdefault("id", 1)
        super().__init__(**kwargs)
