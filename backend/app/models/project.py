"""Project hierarchy: Project → Building → Floor → Zone, plus floor plans (§3, §22)."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ProjectStatus
from app.database.base import (
    Base,
    EnumType,
    JSONType,
    PKType,
    TimestampMixin,
    UTCDateTime,
    pk_column,
)

if TYPE_CHECKING:
    from app.models.capture import CaptureLocation, CaptureSession
    from app.models.user import User


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = pk_column()
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    client: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        EnumType(ProjectStatus, 32), nullable=False, default=ProjectStatus.ACTIVE
    )
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expected_completion_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    project_manager_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: §20 — administrator-configured capture cadence used for due/overdue calculation.
    expected_captures_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    project_manager: Mapped[User | None] = relationship(
        "User", back_populates="managed_projects", foreign_keys=[project_manager_id]
    )
    buildings: Mapped[list[Building]] = relationship(
        "Building",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Building.name",
    )
    capture_sessions: Mapped[list[CaptureSession]] = relationship(
        "CaptureSession", back_populates="project", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Project {self.code} {self.name}>"


class Building(Base, TimestampMixin):
    __tablename__ = "buildings"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_building_project_name"),)

    id: Mapped[str] = pk_column()
    project_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    project: Mapped[Project] = relationship("Project", back_populates="buildings")
    floors: Mapped[list[Floor]] = relationship(
        "Floor",
        back_populates="building",
        cascade="all, delete-orphan",
        order_by="Floor.level",
    )


class Floor(Base, TimestampMixin):
    __tablename__ = "floors"
    __table_args__ = (UniqueConstraint("building_id", "name", name="uq_floor_building_name"),)

    id: Mapped[str] = pk_column()
    building_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("buildings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Ordering key. Ground = 0, basements negative, so the UI sorts without parsing names.
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Total floor area. Coverage percentages are relative to the *sum of zone areas*, never
    #: inferred from imagery (§19).
    area_sqft: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Height above project datum, in metres — reserved for future BIM alignment (§30).
    elevation_m: Mapped[float | None] = mapped_column(Float, nullable=True)

    building: Mapped[Building] = relationship("Building", back_populates="floors")
    zones: Mapped[list[Zone]] = relationship(
        "Zone", back_populates="floor", cascade="all, delete-orphan", order_by="Zone.name"
    )
    floor_plans: Mapped[list[FloorPlan]] = relationship(
        "FloorPlan", back_populates="floor", cascade="all, delete-orphan"
    )
    capture_locations: Mapped[list[CaptureLocation]] = relationship(
        "CaptureLocation", back_populates="floor", cascade="all, delete-orphan"
    )

    @property
    def project_id(self) -> str:
        return self.building.project_id


class Zone(Base, TimestampMixin):
    __tablename__ = "zones"
    __table_args__ = (UniqueConstraint("floor_id", "name", name="uq_zone_floor_name"),)

    id: Mapped[str] = pk_column()
    floor_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("floors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    area_sqft: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Administrator-declared number of capture points needed to consider the zone covered (§19).
    expected_capture_points: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    floor: Mapped[Floor] = relationship("Floor", back_populates="zones")
    capture_locations: Mapped[list[CaptureLocation]] = relationship(
        "CaptureLocation", back_populates="zone"
    )


class FloorPlan(Base, TimestampMixin):
    """An uploaded floor-plan raster plus its optional real-world calibration (§12)."""

    __tablename__ = "floor_plans"
    __table_args__ = (Index("ix_floor_plans_floor_active", "floor_id", "is_active"),)

    id: Mapped[str] = pk_column()
    floor_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("floors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    #: Pixel dimensions of the raster. Capture-location x/y are in these pixel coordinates.
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Optional two-point calibration: pixels per real-world metre. Needed later for BIM (§30).
    px_per_meter: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibration: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: How the raster was produced, when it was not uploaded as one — see `processing/pdf.py`.
    #:
    #: Its own column rather than a key inside ``calibration``, because the calibration endpoint
    #: replaces that value wholesale: recording the render there would mean calibrating a plan
    #: silently erased the record of which PDF sheet its coordinates belong to.
    render: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    uploaded_by_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    floor: Mapped[Floor] = relationship("Floor", back_populates="floor_plans")
    capture_locations: Mapped[list[CaptureLocation]] = relationship(
        "CaptureLocation", back_populates="floor_plan"
    )


class CaptureSchedule(Base, TimestampMixin):
    """Per-project (optionally per-floor) capture cadence for the frequency monitor (§20)."""

    __tablename__ = "capture_schedules"
    __table_args__ = (UniqueConstraint("project_id", "floor_id", name="uq_schedule_project_floor"),)

    id: Mapped[str] = pk_column()
    project_id: Mapped[str] = mapped_column(
        PKType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    floor_id: Mapped[str | None] = mapped_column(
        PKType, ForeignKey("floors.id", ondelete="CASCADE"), nullable=True
    )
    expected_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    #: Grace period before "due" becomes "overdue".
    grace_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_from: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
