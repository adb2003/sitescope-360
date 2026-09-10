"""Annotations pinned inside 360 frames (§28)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import func, select

from app.api.deps import (
    DbSession,
    Pagination,
    RequireAnnotate,
    RequireCaptureRead,
    get_frame_or_404,
    get_session_or_404,
)
from app.auth.rbac import Capability, has_capability
from app.core.enums import AnnotationPriority, AnnotationStatus
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.logging import log_event
from app.models import Annotation, CaptureFrame, CaptureSession, Floor, User
from app.schemas.annotation import (
    AnnotationCreate,
    AnnotationDetail,
    AnnotationOut,
    AnnotationUpdate,
)
from app.schemas.common import Message, Page

router = APIRouter(tags=["annotations"])


@router.post(
    "/frames/{frame_id}/annotations",
    response_model=AnnotationDetail,
    status_code=status.HTTP_201_CREATED,
)
def create_annotation(
    frame_id: str, payload: AnnotationCreate, db: DbSession, user: RequireAnnotate
) -> AnnotationDetail:
    frame = get_frame_or_404(db, frame_id)
    session = get_session_or_404(db, frame.session_id)

    annotation = Annotation(
        frame_id=frame.id,
        project_id=session.project_id,
        floor_id=session.floor_id,
        yaw_deg=payload.yaw_deg,
        pitch_deg=payload.pitch_deg,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        status=AnnotationStatus.OPEN,
        created_by_id=user.id,
        assigned_to_id=payload.assigned_to_id,
    )
    db.add(annotation)
    db.commit()
    log_event(
        "annotation.created",
        annotation_id=annotation.id,
        frame_id=frame.id,
        project_id=session.project_id,
        priority=str(annotation.priority),
    )
    return _detail(db, annotation)


@router.get("/frames/{frame_id}/annotations", response_model=list[AnnotationDetail])
def list_frame_annotations(
    frame_id: str, db: DbSession, _: RequireCaptureRead
) -> list[AnnotationDetail]:
    get_frame_or_404(db, frame_id)
    rows = db.scalars(
        select(Annotation).where(Annotation.frame_id == frame_id).order_by(Annotation.created_at)
    )
    return [_detail(db, row) for row in rows]


@router.get("/projects/{project_id}/annotations", response_model=Page[AnnotationDetail])
def list_project_annotations(
    project_id: str,
    db: DbSession,
    page: Pagination,
    _: RequireCaptureRead,
    annotation_status: Annotated[AnnotationStatus | None, Query(alias="status")] = None,
    priority: Annotated[AnnotationPriority | None, Query()] = None,
    floor_id: Annotated[str | None, Query()] = None,
) -> Page[AnnotationDetail]:
    query = select(Annotation).where(Annotation.project_id == project_id)
    if annotation_status:
        query = query.where(Annotation.status == annotation_status)
    if priority:
        query = query.where(Annotation.priority == priority)
    if floor_id:
        query = query.where(Annotation.floor_id == floor_id)

    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = list(
        db.scalars(
            query.order_by(Annotation.created_at.desc()).limit(page.limit).offset(page.offset)
        )
    )
    return Page(
        items=[_detail(db, row) for row in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/annotations/{annotation_id}", response_model=AnnotationDetail)
def get_annotation(annotation_id: str, db: DbSession, _: RequireCaptureRead) -> AnnotationDetail:
    return _detail(db, _get_or_404(db, annotation_id))


@router.patch("/annotations/{annotation_id}", response_model=AnnotationDetail)
def update_annotation(
    annotation_id: str, payload: AnnotationUpdate, db: DbSession, user: RequireAnnotate
) -> AnnotationDetail:
    annotation = _get_or_404(db, annotation_id)
    updates = payload.model_dump(exclude_unset=True)

    # Anyone who can annotate may edit their own note; resolving someone else's finding is a
    # project-management decision, so it needs the resolve capability.
    if annotation.created_by_id != user.id and not has_capability(
        user.role, Capability.ANNOTATION_RESOLVE
    ):
        raise PermissionDeniedError(
            "You can only edit annotations you created. Resolving others' annotations requires "
            "the Project Manager role."
        )
    new_status = updates.get("status")
    if new_status == AnnotationStatus.RESOLVED and not has_capability(
        user.role, Capability.ANNOTATION_RESOLVE
    ):
        raise PermissionDeniedError("Resolving an annotation requires the Project Manager role")

    for field, value in updates.items():
        setattr(annotation, field, value)

    if new_status == AnnotationStatus.RESOLVED and annotation.resolved_at is None:
        annotation.resolved_at = datetime.now(UTC)
    elif new_status is not None and new_status != AnnotationStatus.RESOLVED:
        annotation.resolved_at = None

    db.commit()
    return _detail(db, annotation)


@router.delete("/annotations/{annotation_id}", response_model=Message)
def delete_annotation(annotation_id: str, db: DbSession, user: RequireAnnotate) -> Message:
    annotation = _get_or_404(db, annotation_id)
    if annotation.created_by_id != user.id and not has_capability(
        user.role, Capability.ANNOTATION_RESOLVE
    ):
        raise PermissionDeniedError("You can only delete annotations you created")
    db.delete(annotation)
    db.commit()
    return Message(message="Annotation deleted")


def _get_or_404(db: DbSession, annotation_id: str) -> Annotation:
    annotation = db.get(Annotation, annotation_id)
    if annotation is None:
        raise NotFoundError(f"Annotation {annotation_id} not found")
    return annotation


def _detail(db: DbSession, annotation: Annotation) -> AnnotationDetail:
    row = db.execute(
        select(Floor.name, CaptureFrame.captured_at)
        .select_from(Annotation)
        .join(CaptureFrame, CaptureFrame.id == Annotation.frame_id)
        .join(CaptureSession, CaptureSession.id == CaptureFrame.session_id)
        .join(Floor, Floor.id == CaptureSession.floor_id)
        .where(Annotation.id == annotation.id)
    ).first()
    floor_name, captured_at = row or (None, None)

    creator = db.get(User, annotation.created_by_id) if annotation.created_by_id else None
    assignee = db.get(User, annotation.assigned_to_id) if annotation.assigned_to_id else None
    frame = db.get(CaptureFrame, annotation.frame_id)

    return AnnotationDetail(
        **AnnotationOut.model_validate(annotation).model_dump(),
        created_by_name=creator.full_name if creator else None,
        assigned_to_name=assignee.full_name if assignee else None,
        floor_name=floor_name,
        location_code=frame.location.code if frame and frame.location else None,
        captured_at=captured_at,
    )
