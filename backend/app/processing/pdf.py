"""Turning a PDF sheet into a floor-plan raster (§12).

Contractors and architects hand over PDF sheet sets. Until now this platform stored one and then
told the operator to go and export a PNG by hand:

    "PDF floor plans are stored but cannot be used for pixel-coordinate placement until they are
     rasterised. Upload a PNG or JPG export of the sheet to place capture points."

Everything positional in the application is expressed in *plan pixel coordinates* — capture
points, the viewer's minimap, the spatial time-travel query, the two-anchor track alignment — so
a plan with no raster is a plan none of those features work on. That made the most common real
input the one format the product could not use.

**The pixel grid is the coordinate system, so it has to be reproducible.** A capture location
stores ``x``/``y`` against a plan's pixels. If the same PDF rendered to a different size on a
different machine, or after an upgrade, every stored coordinate on it would silently point
somewhere else. So the size is derived only from the page's own dimensions and an explicit DPI,
never from a viewport, a window or a guess — and the DPI actually used is recorded alongside the
raster rather than assumed to be the default.

**What this deliberately does not do is infer scale.** A PDF knows its *sheet* size — that this
is an A1 page — and nothing whatsoever about the building: the drawing scale is a convention
printed on the paper, not metadata. Deriving ``px_per_meter`` from the page geometry would
produce a confident, precise and completely wrong number. Real-world scale stays where it was:
two points a human identifies against a printed dimension, stored as ``manual``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from app.core.errors import UnsupportedMediaError, ValidationError

#: Every PDF starts with this. Used to catch a PDF wearing a .png extension, and vice versa.
PDF_MAGIC = b"%PDF"

#: PDF user space is 72 units to the inch, by definition.
POINTS_PER_INCH = 72.0

#: Rendering resolution, in dots per inch of *paper*.
#:
#: 150 puts an A1 sheet at roughly 3500x5000 px — about 2 px per millimetre of paper, so on a
#: 1:100 drawing one pixel is around 50 mm of building. Far finer than an operator can place a
#: capture point by eye, and it keeps a full sheet set within a few megabytes each. Raising it
#: costs memory quadratically for detail nobody can click on.
DEFAULT_DPI = 150

#: Hard ceiling on the longest edge, whatever DPI was asked for.
#:
#: A browser decodes a raster to uncompressed RGBA, so a 10,000px edge is a few hundred megabytes
#: in the tab that has to draw the minimap. When a sheet would exceed this the DPI is reduced to
#: fit and the *effective* value is recorded — the alternative, refusing the upload, would leave
#: the operator with no plan at all for an unusually large sheet.
MAX_EDGE_PX = 5000

#: Sanity bound. A "PDF" claiming ten thousand pages is not a floor plan.
MAX_PAGES = 2000


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """One rasterised sheet, and everything needed to reproduce or explain it."""

    png: bytes
    width_px: int
    height_px: int
    #: 1-based, because that is how a person refers to a sheet in a set.
    page: int
    page_count: int
    #: What was actually used, which may be lower than requested — see :data:`MAX_EDGE_PX`.
    dpi: float
    page_width_pt: float
    page_height_pt: float

    def to_metadata(self) -> dict[str, object]:
        """The record stored with the plan.

        Persisted because the page number is load-bearing: capture coordinates on a sheet set are
        meaningless without knowing *which* sheet they were placed on, and a raster on its own
        cannot say. The rest is here so the render can be reproduced or explained rather than
        re-derived from assumptions about the defaults at the time.
        """
        return {
            "source": "pdf",
            "page": self.page,
            "page_count": self.page_count,
            "dpi": round(self.dpi, 2),
            "page_width_pt": round(self.page_width_pt, 2),
            "page_height_pt": round(self.page_height_pt, 2),
            # Stated rather than left to be inferred: the sheet size says nothing about the
            # building, so the pixel-to-metre scale is still a human's to enter.
            "scale_note": (
                "Rasterised from the PDF page geometry. The drawing's real-world scale is not "
                "recorded in the file and must still be calibrated from two known points."
            ),
        }


def looks_like_pdf(data: bytes) -> bool:
    return data[: len(PDF_MAGIC)] == PDF_MAGIC


def _open(data: bytes) -> object:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - a declared dependency
        raise UnsupportedMediaError(
            "PDF floor plans need pypdfium2. Install it with: uv sync"
        ) from exc

    try:
        return pdfium.PdfDocument(data)
    except Exception as exc:
        # Encrypted, truncated, or not really a PDF. The extension and magic bytes already
        # passed, so this is about the contents.
        raise UnsupportedMediaError(
            "This PDF could not be opened. It may be password-protected or damaged."
        ) from exc


def count_pages(data: bytes) -> int:
    document = _open(data)
    try:
        return len(document)  # type: ignore[arg-type]
    finally:
        close = getattr(document, "close", None)
        if close is not None:
            close()


def effective_dpi(page_width_pt: float, page_height_pt: float, requested_dpi: float) -> float:
    """The DPI that keeps the longest edge within :data:`MAX_EDGE_PX`.

    Separated out so the arithmetic can be tested directly. Getting it wrong in the generous
    direction produces a raster large enough to stall the browser that has to draw it, which
    looks like the viewer being broken rather than the plan being too big.
    """
    longest_pt = max(page_width_pt, page_height_pt)
    if longest_pt <= 0:
        raise ValidationError("This PDF page has no dimensions")
    ceiling = MAX_EDGE_PX * POINTS_PER_INCH / longest_pt
    return min(requested_dpi, ceiling)


def render_page(data: bytes, *, page: int = 1, dpi: float = DEFAULT_DPI) -> RenderedPage:
    """Rasterise one page of a PDF to a PNG.

    ``page`` is 1-based. A sheet set is uploaded once per sheet the site actually needs, rather
    than every page being turned into a floor plan automatically: a 40-page set contains sections,
    elevations and details, and importing them all as plans to place capture points on would be
    worse than importing none.
    """
    if dpi <= 0:
        raise ValidationError("Render DPI must be positive")

    document = _open(data)
    try:
        total = len(document)  # type: ignore[arg-type]
        if total < 1:
            raise UnsupportedMediaError("This PDF has no pages")
        if total > MAX_PAGES:
            raise UnsupportedMediaError(f"This PDF has {total} pages, more than {MAX_PAGES}")
        if not 1 <= page <= total:
            raise ValidationError(
                f"This PDF has {total} page{'s' if total != 1 else ''}; page {page} does not exist"
            )

        sheet = document[page - 1]  # type: ignore[index]
        page_width_pt, page_height_pt = (float(value) for value in sheet.get_size())
        used_dpi = effective_dpi(page_width_pt, page_height_pt, dpi)

        bitmap = sheet.render(scale=used_dpi / POINTS_PER_INCH)
        image = bitmap.to_pil()
        # Flattened to RGB: a PDF page renders with an alpha channel that is fully opaque for a
        # drawing, and carrying it would grow every stored plan by a quarter for nothing.
        if image.mode != "RGB":
            image = image.convert("RGB")

        buffer = io.BytesIO()
        # `optimize` costs a little CPU once, at upload, and is paid back on every viewer load.
        image.save(buffer, format="PNG", optimize=True)

        return RenderedPage(
            png=buffer.getvalue(),
            width_px=image.width,
            height_px=image.height,
            page=page,
            page_count=total,
            dpi=used_dpi,
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
        )
    finally:
        close = getattr(document, "close", None)
        if close is not None:
            close()
