"""Dashboard, timeline, coverage, and frequency-monitoring schemas (§16, §19, §20, §21)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.core.enums import (
    CaptureFrequencyStatus,
    CaptureSessionStatus,
    DataProvenance,
    MappingConfidence,
    QualityVerdict,
)


# ---- timeline ----------------------------------------------------------------
class TimelineEntry(BaseModel):
    capture_date: date
    session_ids: list[str]
    session_count: int
    frame_count: int
    located_frame_count: int
    #: Share of the floor's known capture locations visited on this date, 0–1.
    coverage_ratio: float
    coverage_provenance: DataProvenance
    floors: list[str] = []
    operators: list[str] = []


class TimelineOut(BaseModel):
    project_id: str
    floor_id: str | None = None
    entries: list[TimelineEntry]
    #: Denominator used for every ``coverage_ratio`` above, stated so the number is auditable.
    location_total: int
    note: str | None = None


# ---- coverage ----------------------------------------------------------------
class ZoneCoverage(BaseModel):
    zone_id: str
    zone_name: str
    area_sqft: float | None
    expected_capture_points: int
    actual_capture_points: int
    captured_points_recent: int
    coverage_ratio: float
    covered_area_sqft: float | None
    pending_area_sqft: float | None


class FloorCoverage(BaseModel):
    floor_id: str
    floor_name: str
    level: int
    area_sqft: float | None
    expected_capture_points: int
    actual_capture_points: int
    visited_locations: int
    coverage_ratio: float
    covered_area_sqft: float | None
    pending_area_sqft: float | None
    last_capture_at: datetime | None
    zones: list[ZoneCoverage] = []


class CoverageOut(BaseModel):
    project_id: str
    coverage_ratio: float
    total_area_sqft: float | None
    covered_area_sqft: float | None
    pending_area_sqft: float | None
    #: Always ``manual`` for area figures: zones and expected points are administrator-declared.
    #: The platform does not infer square footage from imagery (§19, §46).
    provenance: DataProvenance
    method: str
    floors: list[FloorCoverage] = []


# ---- capture frequency -------------------------------------------------------
class FrequencyStatusOut(BaseModel):
    project_id: str
    project_name: str
    floor_id: str | None = None
    floor_name: str | None = None
    expected_per_week: int
    last_capture_at: datetime | None
    days_since_last_capture: int | None
    next_expected_date: date | None
    status: CaptureFrequencyStatus
    captures_this_week: int


# ---- dashboard ---------------------------------------------------------------
class CaptureHealth(BaseModel):
    total_sessions: int
    completed: int
    in_progress: int
    pending: int
    failed: int


class FloorStatus(BaseModel):
    floor_id: str
    floor_name: str
    level: int
    coverage_ratio: float
    location_count: int
    visited_locations: int
    last_capture_at: datetime | None


class RecentCapture(BaseModel):
    session_id: str
    captured_at: datetime
    operator_name: str | None
    floor_name: str
    zone_name: str | None
    frame_count: int
    quality_verdict: QualityVerdict | None
    status: CaptureSessionStatus
    mapping_confidence: MappingConfidence


class QualityBreakdown(BaseModel):
    good: int = 0
    warning: int = 0
    poor: int = 0
    unusable: int = 0
    unscored: int = 0


class ProjectDashboard(BaseModel):
    project_id: str
    project_name: str
    project_code: str
    building_count: int
    floor_count: int
    zone_count: int
    location_count: int
    session_count: int
    frame_count: int
    annotation_open_count: int
    last_capture_at: datetime | None
    coverage: CoverageOut
    capture_health: CaptureHealth
    quality: QualityBreakdown
    frequency: FrequencyStatusOut
    floors: list[FloorStatus]
    recent_captures: list[RecentCapture]
    #: True when no capture has been processed yet, so the UI can show an empty state instead of
    #: a wall of zeroes that reads like a broken dashboard.
    is_empty: bool = False


class PortfolioRow(BaseModel):
    project_id: str
    project_name: str
    project_code: str
    status: str
    floor_count: int
    session_count: int
    coverage_ratio: float
    last_capture_at: datetime | None
    frequency_status: CaptureFrequencyStatus
    open_annotations: int


class PortfolioOut(BaseModel):
    projects: list[PortfolioRow]
    total_projects: int
    total_sessions: int
    total_frames: int
    overdue_count: int


# ---- stale-area detection (§ product vision item 11) -------------------------
class StaleLocation(BaseModel):
    location_id: str
    code: str
    label: str | None
    floor_id: str
    floor_name: str
    zone_name: str | None
    last_captured_at: datetime | None
    days_since_capture: int | None


class StaleAreasOut(BaseModel):
    project_id: str
    threshold_days: int
    never_captured: list[StaleLocation]
    stale: list[StaleLocation]
