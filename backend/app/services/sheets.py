"""Choosing which sheet of a drawing set is the floor plan (§22).

A PDF sheet set is one file containing forty drawings, of which one or two are the plans anybody
wants to place capture points on; the rest are sections, elevations, details and a title page.
The upload route has always taken a page number, and the operator has always had to *know* it —
on a set received by email six months ago, from a consultant, with no consistent ordering, that
is a guess followed by a re-upload.

What was missing is not a way to render a different page; `processing/pdf.py` has done that from
the start, and the original PDF is kept beside every plan it produced precisely so a second sheet
could be pulled out later. What was missing is a way to **look** before choosing.

So: cheap per-page previews, and a way to say "that one instead".

**Switching sheets creates a new revision rather than re-rendering in place.** This is the part
worth being careful about. Every capture location on a floor plan is stored in *that plan's
pixels*, and a different sheet is a different drawing at a different scale — so overwriting the
raster would silently relocate every point somebody had placed, to coordinates that mean nothing.
Revisions already exist for exactly this reason (a new upload deactivates the old one and keeps
it, so an older capture's coordinates stay interpretable against the plan they were placed on),
and picking a sheet is the same event arriving by a different route.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.ids import new_id
from app.core.logging import log_event
from app.models import FloorPlan, User
from app.processing.pdf import count_pages, render_page
from app.storage import floorplan_key, get_storage

#: DPI for a preview thumbnail.
#:
#: Low on purpose. A preview answers one question — "is this the plan or is it a section" — which
#: survives being small, and an A3 sheet at this density renders in a few milliseconds and weighs
#: a couple of kilobytes. The plan itself is rendered at `pdf.DEFAULT_DPI`; this is never what
#: gets placed on.
PREVIEW_DPI = 24


@dataclass(frozen=True, slots=True)
class SheetInfo:
    """One page of a set, described without rendering it."""

    page: int
    width_pt: float
    height_pt: float

    @property
    def orientation(self) -> str:
        return "landscape" if self.width_pt >= self.height_pt else "portrait"


def source_of(plan: FloorPlan) -> str:
    """The stored PDF a plan was rendered from, or refuse if it was not.

    An uploaded PNG has no sheets to choose between, and saying so plainly beats returning an
    empty list that reads like a set with nothing in it.
    """
    render = plan.render or {}
    key = render.get("source_storage_key")
    if not key:
        raise ValidationError(
            "This floor plan was uploaded as an image, so it has no sheets to choose from",
            floor_plan_id=plan.id,
        )
    return str(key)


def _source_bytes(plan: FloorPlan) -> bytes:
    key = source_of(plan)
    try:
        return get_storage().read_bytes(key)
    except Exception as exc:
        # The row says there is a source and storage disagrees. Worth its own message: it is an
        # operational fault, not the operator having uploaded the wrong sort of file.
        raise NotFoundError(
            "The original PDF for this floor plan is no longer in storage; re-upload the set"
        ) from exc


def list_sheets(plan: FloorPlan) -> list[SheetInfo]:
    """Every page of the set, with its paper size and nothing rendered.

    Sizes come from the page objects, so this stays cheap on a set with hundreds of pages — the
    picker renders previews one at a time as they are actually looked at.
    """
    from app.processing.pdf import _open

    data = _source_bytes(plan)
    document = _open(data)
    try:
        sheets = []
        for index in range(len(document)):  # type: ignore[arg-type]
            page = document[index]  # type: ignore[index]
            width_pt, height_pt = (float(value) for value in page.get_size())
            sheets.append(SheetInfo(page=index + 1, width_pt=width_pt, height_pt=height_pt))
        return sheets
    finally:
        close = getattr(document, "close", None)
        if close is not None:
            close()


def preview(plan: FloorPlan, page: int) -> bytes:
    """A small PNG of one page, for the picker."""
    data = _source_bytes(plan)
    return render_page(data, page=page, dpi=PREVIEW_DPI).png


def page_count(plan: FloorPlan) -> int:
    return count_pages(_source_bytes(plan))


def choose_sheet(db: Session, plan: FloorPlan, page: int, user: User) -> FloorPlan:
    """Make a different sheet of the same set the floor's active plan.

    A new revision, not an overwrite. Capture locations are stored in the pixels of the plan they
    were placed on, and another sheet is a different drawing at a different scale — rewriting the
    raster underneath them would move every point somebody placed to a coordinate that means
    nothing, without anything in the data recording that it had happened.

    The source PDF is shared with the revision rather than copied: it is the same file, it can be
    tens of megabytes, and a set with four plans in it should not be stored four times.

    A sheet already rendered for this floor is **re-activated rather than rendered again**, and
    that is not merely an optimisation. Capture points reference the revision they were placed
    on, so an operator who switches to the section and back has to arrive at the *same* revision
    or their points are orphaned for good — a second revision of sheet 1 renders the identical
    drawing and is still, to every point on the first, a different plan. Rendering afresh each
    time also grew the revision list and the stored rasters without bound: toggling between two
    sheets four times left eight revisions and eight PNGs describing two drawings.
    """
    source_key = source_of(plan)
    data = _source_bytes(plan)
    total = count_pages(data)
    if not 1 <= page <= total:
        raise ValidationError(
            f"This set has {total} sheet{'s' if total != 1 else ''}; sheet {page} does not exist",
            floor_plan_id=plan.id,
        )

    existing = _revision_for_page(db, plan, source_key, page)
    if existing is not None:
        _deactivate_others(db, plan.floor_id)
        existing.is_active = True
        db.flush()
        log_event(
            "floorplan.sheet_chosen",
            floor_id=plan.floor_id,
            floor_plan_id=existing.id,
            replaced_plan_id=plan.id,
            page=page,
            of_pages=total,
            reused_existing_revision=True,
            chosen_by=user.id,
        )
        return existing

    sheet = render_page(data, page=page)
    render = sheet.to_metadata()
    render["source_storage_key"] = source_key

    plan_id = new_id()
    key = floorplan_key(_project_of(db, plan), plan.floor_id, f"{plan_id}.png")
    get_storage().put_bytes(key, sheet.png, "image/png")

    _deactivate_others(db, plan.floor_id)

    revision = FloorPlan(
        id=plan_id,
        floor_id=plan.floor_id,
        name=f"{_base_name(plan.name)} — sheet {page}",
        storage_key=key,
        content_type="image/png",
        width_px=sheet.width_px,
        height_px=sheet.height_px,
        file_size_bytes=len(sheet.png),
        is_active=True,
        uploaded_by_id=user.id,
        # Not carried over. A calibration is a pixel-to-metre scale measured on one drawing, and
        # another sheet is drawn at another scale — copying it would produce a plan that looks
        # calibrated and reports wrong distances, which is worse than one that admits it is not.
        calibration=None,
        render=render,
    )
    db.add(revision)
    db.flush()

    log_event(
        "floorplan.sheet_chosen",
        floor_id=plan.floor_id,
        floor_plan_id=revision.id,
        replaced_plan_id=plan.id,
        page=page,
        of_pages=total,
        reused_existing_revision=False,
        chosen_by=user.id,
    )
    return revision


def _revision_for_page(
    db: Session, plan: FloorPlan, source_key: str, page: int
) -> FloorPlan | None:
    """A revision of this floor already rendered from this set's given sheet, if there is one.

    Matched on the source file *and* the page, because a floor can legitimately hold plans from
    two different sets and "sheet 2" of one is unrelated to "sheet 2" of the other. Reads the
    handful of rows rather than asking two dialects to index into a JSON column — and a wrong
    match here would re-activate a plan showing the wrong drawing, so it must not be clever.
    """
    for candidate in db.scalars(select(FloorPlan).where(FloorPlan.floor_id == plan.floor_id)):
        render = candidate.render or {}
        if render.get("source_storage_key") == source_key and render.get("page") == page:
            return candidate
    return None


def _deactivate_others(db: Session, floor_id: str) -> None:
    for previous in db.scalars(
        select(FloorPlan).where(FloorPlan.floor_id == floor_id, FloorPlan.is_active.is_(True))
    ):
        previous.is_active = False


def _base_name(name: str) -> str:
    """The set's name without a sheet suffix, so choosing twice does not stack them."""
    marker = " — sheet "
    return name.split(marker)[0] if marker in name else name


def _project_of(db: Session, plan: FloorPlan) -> str:
    from app.models import Building, Floor

    row = db.execute(
        select(Building.project_id)
        .join(Floor, Floor.building_id == Building.id)
        .where(Floor.id == plan.floor_id)
    ).first()
    if row is None:
        raise NotFoundError("The plan's floor no longer exists")
    return str(row[0])
