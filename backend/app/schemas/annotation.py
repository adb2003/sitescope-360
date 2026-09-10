from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from app.core.enums import AnnotationPriority, AnnotationStatus
from app.schemas.common import ORMModel


class AnnotationCreate(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=300)]
    description: str | None = None
    yaw_deg: Annotated[float, Field(ge=-360, le=360)] = 0.0
    pitch_deg: Annotated[float, Field(ge=-90, le=90)] = 0.0
    priority: AnnotationPriority = AnnotationPriority.MEDIUM
    assigned_to_id: str | None = None


class AnnotationUpdate(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    description: str | None = None
    yaw_deg: Annotated[float, Field(ge=-360, le=360)] | None = None
    pitch_deg: Annotated[float, Field(ge=-90, le=90)] | None = None
    priority: AnnotationPriority | None = None
    status: AnnotationStatus | None = None
    assigned_to_id: str | None = None


class AnnotationOut(ORMModel):
    id: str
    frame_id: str
    project_id: str
    floor_id: str
    yaw_deg: float
    pitch_deg: float
    title: str
    description: str | None
    priority: AnnotationPriority
    status: AnnotationStatus
    created_by_id: str | None
    assigned_to_id: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AnnotationDetail(AnnotationOut):
    created_by_name: str | None = None
    assigned_to_name: str | None = None
    floor_name: str | None = None
    location_code: str | None = None
    captured_at: datetime | None = None
