"""Dashboard, timeline, coverage, frequency and stale-area routes (§16, §19, §20, §21, §26)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, RequireProjectRead, get_project_or_404
from app.schemas.monitoring import (
    CoverageOut,
    FrequencyStatusOut,
    PortfolioOut,
    ProjectDashboard,
    StaleAreasOut,
    TimelineOut,
)
from app.services import monitoring

router = APIRouter(tags=["monitoring"])


@router.get("/dashboard", response_model=PortfolioOut)
def portfolio_dashboard(
    db: DbSession,
    _: RequireProjectRead,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> PortfolioOut:
    """Cross-project overview: coverage, activity and overdue count."""
    return monitoring.build_portfolio(db, limit=limit)


@router.get("/projects/{project_id}/dashboard", response_model=ProjectDashboard)
def project_dashboard(project_id: str, db: DbSession, _: RequireProjectRead) -> ProjectDashboard:
    """Everything the project dashboard shows, in one request (§21)."""
    get_project_or_404(db, project_id)
    return monitoring.build_dashboard(db, project_id)


@router.get("/projects/{project_id}/timeline", response_model=TimelineOut)
def project_timeline(
    project_id: str,
    db: DbSession,
    _: RequireProjectRead,
    floor_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=365)] = 60,
) -> TimelineOut:
    """Capture activity by date (§16)."""
    return monitoring.compute_timeline(db, project_id, floor_id=floor_id, limit=limit)


@router.get("/projects/{project_id}/coverage", response_model=CoverageOut)
def project_coverage(
    project_id: str,
    db: DbSession,
    _: RequireProjectRead,
    recent_days: Annotated[
        int | None,
        Query(ge=1, le=365, description="Only count locations captured within this window"),
    ] = None,
) -> CoverageOut:
    """Coverage by floor and zone (§19).

    The response carries ``provenance`` and ``method`` because these numbers are derived from
    administrator-declared expectations, not measured from imagery.
    """
    return monitoring.compute_coverage(db, project_id, recent_days=recent_days)


@router.get("/projects/{project_id}/frequency", response_model=FrequencyStatusOut)
def project_frequency(
    project_id: str,
    db: DbSession,
    _: RequireProjectRead,
    floor_id: Annotated[str | None, Query()] = None,
) -> FrequencyStatusOut:
    """Last capture, days since, and due/overdue status (§20)."""
    return monitoring.compute_frequency(db, project_id, floor_id=floor_id)


@router.get("/projects/{project_id}/stale-areas", response_model=StaleAreasOut)
def project_stale_areas(
    project_id: str,
    db: DbSession,
    _: RequireProjectRead,
    threshold_days: Annotated[int, Query(ge=1, le=365)] = 14,
) -> StaleAreasOut:
    """Locations never captured, or not captured recently."""
    return monitoring.find_stale_areas(db, project_id, threshold_days=threshold_days)
