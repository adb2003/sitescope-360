"""Read-only links to a single view (§27).

Two audiences, and the split matters. Creating, listing and revoking a link is ordinary
authenticated, membership-scoped work. *Opening* one is the only route in this API that reads
project data with no account at all, and it is deliberately the smallest surface that can be
useful: one frame, no navigation, no ids that work elsewhere.

`GET /api/shared/{token}` is rate limited by address, **in its own bucket**. The token is 256
bits, so this is not about guessing — it is about an unauthenticated endpoint that signs URLs and
writes a row on every request, which is worth a ceiling whether or not anyone is attacking it.
The bucket is separate from the sign-in limiter's on purpose: a link passed around one office
would otherwise spend everybody's login allowance.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request, status
from sqlalchemy import func, select

from app.api.deps import DbSession, Pagination, RequireCaptureRead, get_frame_or_404
from app.auth.membership import assert_member
from app.auth.ratelimit import check_share_address
from app.core.errors import NotFoundError
from app.models import (
    Building,
    CaptureFrame,
    CaptureLocation,
    CaptureSession,
    Floor,
    Project,
    ShareLink,
)
from app.schemas.capture import (
    SharedFrameView,
    ShareLinkCreate,
    ShareLinkCreated,
    ShareLinkOut,
)
from app.schemas.common import Message, Page
from app.services import sharing
from app.services.assets import sign

router = APIRouter(tags=["shares"])

#: What a shared view says about itself, in the payload rather than only in the interface.
#:
#: A client rendering this must not be able to present it as a live view of the project or as a
#: measurement, and the honest caveats travel with the data rather than being the responsibility
#: of whoever writes the page.
NOTICE = (
    "A read-only view of one 360 capture, shared by link. It shows this location on this date "
    "only; positions are estimated, not surveyed. The link expires and can be revoked."
)


def _out(share: ShareLink) -> ShareLinkOut:
    return ShareLinkOut(
        id=share.id,
        frame_id=share.frame_id,
        label=share.label,
        created_by_id=share.created_by_id,
        expires_at=share.expires_at,
        revoked_at=share.revoked_at,
        is_live=sharing.is_live(share),
        view_count=int(share.view_count or 0),
        last_viewed_at=share.last_viewed_at,
        created_at=share.created_at,
    )


@router.post(
    "/frames/{frame_id}/share",
    response_model=ShareLinkCreated,
    status_code=status.HTTP_201_CREATED,
)
def create_share_link(
    frame_id: str,
    payload: ShareLinkCreate,
    db: DbSession,
    user: RequireCaptureRead,
) -> ShareLinkCreated:
    """Create a link that shows this frame to someone without an account.

    Gated on `capture:read` and on membership of the frame's project — you can share what you can
    see, and nothing else. **The token is in this response and in no other.** Only its hash is
    stored, so it cannot be shown again; the answer to a lost link is a new link and a revocation
    of the old one.
    """
    frame = get_frame_or_404(db, frame_id, user)
    created = sharing.create_share(
        db,
        frame,
        user,
        expires_in_days=payload.expires_in_days,
        label=payload.label,
    )
    db.commit()

    base = _out(created.share).model_dump()
    return ShareLinkCreated(
        **base,
        token=created.token,
        # A path, not a URL. The API does not know the web app's origin, and deriving one from a
        # `Host` header is how a shared link ends up naming whatever hostname the last proxy used.
        path=f"/s/{created.token}",
    )


@router.get("/frames/{frame_id}/shares", response_model=list[ShareLinkOut])
def list_frame_shares(frame_id: str, db: DbSession, user: RequireCaptureRead) -> list[ShareLinkOut]:
    """Links to this frame, live or not.

    Expired and revoked ones are listed too: "was this ever shared, and with how many links" is
    the question being asked, and hiding the dead ones answers it wrongly.
    """
    get_frame_or_404(db, frame_id, user)
    rows = db.scalars(
        select(ShareLink)
        .where(ShareLink.frame_id == frame_id)
        .order_by(ShareLink.created_at.desc())
    )
    return [_out(row) for row in rows]


@router.get("/projects/{project_id}/shares", response_model=Page[ShareLinkOut])
def list_project_shares(
    project_id: str, db: DbSession, page: Pagination, user: RequireCaptureRead
) -> Page[ShareLinkOut]:
    """Every link into this project — the page someone opens to audit what is out there.

    Paginated, and newest first. Dead links are listed deliberately, because "was this ever
    shared, and how widely" is half the question the page exists to answer — which also means
    the list only ever grows, for as long as the project runs.
    """
    assert_member(db, user, project_id, what=f"Project {project_id}")
    query = select(ShareLink).where(ShareLink.project_id == project_id)
    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = db.scalars(
        query.order_by(ShareLink.created_at.desc()).limit(page.limit).offset(page.offset)
    )
    return Page(
        items=[_out(row) for row in rows], total=total, limit=page.limit, offset=page.offset
    )


@router.delete("/shares/{share_id}", response_model=Message)
def revoke_share_link(share_id: str, db: DbSession, user: RequireCaptureRead) -> Message:
    """Stop a link working now, whatever its expiry says.

    `capture:read` and membership, the same bar as creating one. Anyone who could have shared the
    frame can un-share it — a link that only its author can revoke is a link that outlives them
    leaving the project.
    """
    share = db.get(ShareLink, share_id)
    if share is None:
        raise NotFoundError("That share link does not exist")
    assert_member(db, user, share.project_id, what="That share link")

    sharing.revoke(db, share, user)
    db.commit()
    return Message(message="Link revoked")


# ---- the public route --------------------------------------------------------
@router.get("/shared/{token}", response_model=SharedFrameView, tags=["shares"])
def open_shared_view(
    request: Request,
    db: DbSession,
    token: Annotated[str, Path(min_length=20, max_length=200)],
) -> SharedFrameView:
    """Open a shared view. **No authentication** — the token is the credential.

    Every failure is the same 404: wrong token, expired, revoked, or a frame since deleted.
    Telling a visitor that a link *has expired* confirms it was once real, which is the one fact
    a guesser is looking for and no help at all to the person it was sent to, who has to ask for
    a new one either way.
    """
    check_share_address(request)
    share = sharing.resolve(db, token)

    # One query for the frame and the names around it. The join is the reason `share_links` keeps
    # a `project_id` of its own: this read happens with no user and no membership to resolve, so
    # it must not depend on any of the scoped helpers.
    row = db.execute(
        select(CaptureFrame, Project.name, Building.name, Floor.name, CaptureLocation.code)
        .select_from(CaptureFrame)
        .join(CaptureSession, CaptureSession.id == CaptureFrame.session_id)
        .join(Floor, Floor.id == CaptureSession.floor_id)
        .join(Building, Building.id == Floor.building_id)
        .join(Project, Project.id == Building.project_id)
        .outerjoin(CaptureLocation, CaptureLocation.id == CaptureFrame.location_id)
        .where(CaptureFrame.id == share.frame_id)
    ).first()
    if row is None:
        # The frame is gone. Same answer as an invalid token, for the same reason.
        raise NotFoundError("This link is not valid")
    frame, project_name, building_name, floor_name, location_code = row

    sharing.record_view(db, share)
    db.commit()

    return SharedFrameView(
        captured_at=frame.captured_at,
        project_name=project_name,
        building_name=building_name,
        floor_name=floor_name,
        location_code=location_code,
        width_px=frame.width_px,
        height_px=frame.height_px,
        projection=frame.projection,
        initial_yaw_deg=frame.initial_yaw_deg,
        full=sign(frame.storage_key),
        preview=sign(frame.preview_key),
        expires_at=share.expires_at,
        notice=NOTICE,
    )
