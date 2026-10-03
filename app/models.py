from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    main_feed_msg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    subchannel_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, unique=True, index=True, nullable=True
    )
    tracking_mode: Mapped[str] = mapped_column(
        String(32), default="ELIGIBLE", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    role_tracks: Mapped[List["RoleTrack"]] = relationship(
        "RoleTrack", back_populates="company", cascade="all, delete-orphan"
    )
    timeline_events: Mapped[List["TimelineEvent"]] = relationship(
        "TimelineEvent", back_populates="company", cascade="all, delete-orphan"
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("tracking_mode", "ELIGIBLE")
        kwargs.setdefault("is_active", True)
        kwargs.setdefault("is_custom", False)
        kwargs.setdefault("created_at", datetime.now(timezone.utc))
        super().__init__(**kwargs)


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
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
    start_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_status: Mapped[str] = mapped_column(
        String(32), default="PENDING", nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    company: Mapped["Company"] = relationship(
        "Company", back_populates="timeline_events"
    )
    role_track: Mapped[Optional["RoleTrack"]] = relationship(
        "RoleTrack", back_populates="timeline_events"
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("user_status", "PENDING")
        kwargs.setdefault("is_custom", False)
        kwargs.setdefault("version", 1)
        kwargs.setdefault("created_at", datetime.now(timezone.utc))
        super().__init__(**kwargs)

    @property
    def display_state(self) -> str:
        now = datetime.now(timezone.utc)
        # SQLite (via aiosqlite) may return tz-naive datetimes; normalise to UTC.
        end = (
            self.end_time
            if self.end_time.tzinfo is not None
            else self.end_time.replace(tzinfo=timezone.utc)
        )
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
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("updated_at", datetime.now(timezone.utc))
        super().__init__(**kwargs)
