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
    lifecycle_status: Mapped[str] = mapped_column(
        String(64), default="APPLICATION_WINDOW", nullable=False
    )
    is_targeted_by_user: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    latest_status_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    company: Mapped["Company"] = relationship("Company", back_populates="role_tracks")
    timeline_events: Mapped[List["TimelineEvent"]] = relationship(
        "TimelineEvent", back_populates="role_track"
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("drive_type", "FTE")
        kwargs.setdefault("lifecycle_status", "APPLICATION_WINDOW")
        kwargs.setdefault("is_targeted_by_user", True)
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
        if self.user_status == "COMPLETED":
            return "COMPLETED_ACTIVE" if now <= self.end_time else "COMPLETED_EXPIRED"
        if now > self.end_time:
            return "MISSED"
        return "PENDING"
