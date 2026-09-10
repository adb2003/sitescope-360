"""Report renderers: JSON, CSV and PDF (§29).

PDF uses ReportLab's platypus flowables rather than absolute drawing, so a long table paginates
by itself instead of running off the page — which matters because a capture-history report for a
busy site is dozens of pages.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.core.enums import ReportFormat

ACCENT = colors.HexColor("#0F5A4E")
MUTED = colors.HexColor("#5B6B72")
LINE = colors.HexColor("#D8DEE1")
ZEBRA = colors.HexColor("#F4F7F7")


def render(payload: dict[str, Any], report_format: ReportFormat) -> tuple[bytes, str]:
    """Return ``(bytes, content_type)``."""
    if report_format is ReportFormat.JSON:
        return json.dumps(payload, indent=2, default=str).encode(), "application/json"
    if report_format is ReportFormat.CSV:
        return _render_csv(payload), "text/csv"
    return _render_pdf(payload), "application/pdf"


# ---- CSV ---------------------------------------------------------------------
def _render_csv(payload: dict[str, Any]) -> bytes:
    """Flatten the report's tabular section, with the header block as leading comments.

    Reports have one obvious table each (``rows``, or ``floors``/``timeline`` for the summary).
    Emitting the metadata as ``#`` comment lines keeps the file openable in Excel while
    preserving the provenance statement — dropping it would strip exactly the caveat that makes
    the numbers safe to quote.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")

    project = payload.get("project", {})
    writer.writerow([f"# {payload.get('title', 'Report')}"])
    writer.writerow([f"# Project: {project.get('name')} ({project.get('code')})"])
    writer.writerow([f"# Generated: {payload.get('generated_at')}"])
    period = payload.get("period") or {}
    if period.get("start") or period.get("end"):
        writer.writerow(
            [f"# Period: {period.get('start') or 'start'} to {period.get('end') or 'now'}"]
        )
    for key, note in (payload.get("provenance") or {}).items():
        writer.writerow([f"# {key}: {note}"])
    writer.writerow([])

    rows = _table_rows(payload)
    if not rows:
        writer.writerow(["No data for the selected period"])
        return buffer.getvalue().encode("utf-8-sig")

    columns = list(rows[0].keys())
    writer.writerow([_humanize(column) for column in columns])
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])

    # utf-8-sig so Excel on Windows reads the accented characters correctly.
    return buffer.getvalue().encode("utf-8-sig")


def _table_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("rows", "floors", "timeline"):
        value = payload.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def _csv_cell(value: Any) -> Any:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    if isinstance(value, float):
        return round(value, 4)
    return "" if value is None else value


# ---- PDF ---------------------------------------------------------------------
def _render_pdf(payload: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    rows = _table_rows(payload)
    # Wide tables get landscape; a nine-column capture history in portrait is unreadable.
    pagesize = landscape(A4) if rows and len(rows[0]) > 6 else A4

    document = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title=payload.get("title", "SiteScope 360 report"),
        author="SiteScope 360",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "SSTitle",
        parent=styles["Title"],
        fontSize=17,
        leading=21,
        textColor=ACCENT,
        alignment=TA_LEFT,
        spaceAfter=2,
    )
    meta_style = ParagraphStyle(
        "SSMeta", parent=styles["Normal"], fontSize=8.5, leading=11.5, textColor=MUTED
    )
    heading_style = ParagraphStyle(
        "SSHeading",
        parent=styles["Heading2"],
        fontSize=11.5,
        leading=14,
        textColor=ACCENT,
        spaceBefore=10,
        spaceAfter=4,
    )
    body_style = ParagraphStyle("SSBody", parent=styles["Normal"], fontSize=8.5, leading=11.5)

    story: list[Any] = []
    project = payload.get("project", {})
    story.append(Paragraph(payload.get("title", "Report"), title_style))
    story.append(
        Paragraph(
            f"{project.get('name', '')} · {project.get('code', '')}"
            + (f" · {project.get('client')}" if project.get("client") else ""),
            meta_style,
        )
    )
    period = payload.get("period") or {}
    period_text = ""
    if period.get("start") or period.get("end"):
        period_text = f" · Period {period.get('start') or '—'} to {period.get('end') or 'now'}"
    story.append(Paragraph(f"Generated {payload.get('generated_at', '')}{period_text}", meta_style))
    story.append(Spacer(1, 6))

    for section, builder in (
        ("totals", _kv_table),
        ("coverage", _kv_table),
        ("capture_health", _kv_table),
        ("quality", _kv_table),
        ("frequency", _kv_table),
        ("summary", _kv_table),
    ):
        block = payload.get(section)
        if isinstance(block, dict) and block:
            story.append(Paragraph(_humanize(section), heading_style))
            story.append(builder(block, pagesize))
            story.append(Spacer(1, 4))

    if rows:
        story.append(Paragraph(_humanize(_table_key(payload)), heading_style))
        story.extend(_wide_table(rows, pagesize))

    attention = payload.get("attention_required")
    if isinstance(attention, dict) and attention.get("items"):
        story.append(Paragraph("Attention required", heading_style))
        story.extend(_wide_table(attention["items"], pagesize))

    progress = payload.get("progress_analysis")
    if isinstance(progress, dict) and not progress.get("available", True):
        story.append(Spacer(1, 6))
        story.append(
            KeepTogether(
                [
                    Paragraph("Progress analysis", heading_style),
                    Paragraph(str(progress.get("reason", "")), body_style),
                ]
            )
        )

    provenance = payload.get("provenance") or {}
    if provenance:
        story.append(PageBreak())
        story.append(Paragraph("How these numbers were produced", heading_style))
        for key, note in provenance.items():
            story.append(Paragraph(f"<b>{_humanize(key)}</b> — {note}", body_style))
            story.append(Spacer(1, 2))
    if payload.get("independence_note"):
        story.append(Spacer(1, 6))
        story.append(Paragraph(str(payload["independence_note"]), meta_style))

    document.build(story, onLaterPages=_footer, onFirstPage=_footer)
    return buffer.getvalue()


def _table_key(payload: dict[str, Any]) -> str:
    for key in ("rows", "floors", "timeline"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return "detail" if key == "rows" else key
    return "detail"


def _kv_table(block: dict[str, Any], pagesize: tuple[float, float]) -> Table:
    data = [
        [Paragraph(f"<b>{_humanize(key)}</b>", _small()), Paragraph(_fmt(value), _small())]
        for key, value in block.items()
        if not isinstance(value, dict | list)
    ]
    if not data:
        data = [[Paragraph("—", _small()), Paragraph("", _small())]]
    available = pagesize[0] - 28 * mm
    table = Table(data, colWidths=[available * 0.35, available * 0.65], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, ZEBRA]),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _wide_table(rows: list[dict[str, Any]], pagesize: tuple[float, float]) -> list[Any]:
    # Drop URL columns from the PDF: a signed URL is 200+ characters and expires, so printing it
    # wastes the width that the actual data needs.
    columns = [
        key for key in rows[0] if not key.endswith("_url") and key not in {"session_id", "id"}
    ] or list(rows[0].keys())

    header = [Paragraph(f"<b>{_humanize(column)}</b>", _small(white=True)) for column in columns]
    body = [
        [Paragraph(_fmt(row.get(column)), _small()) for column in columns] for row in rows[:400]
    ]

    available = pagesize[0] - 28 * mm
    width = available / max(len(columns), 1)
    table = Table([header, *body], colWidths=[width] * len(columns), repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("GRID", (0, 0), (-1, -1), 0.35, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    story: list[Any] = [table]
    if len(rows) > 400:
        story.append(Spacer(1, 4))
        story.append(
            Paragraph(
                f"Showing the first 400 of {len(rows)} rows. Export as CSV for the full set.",
                ParagraphStyle("SSNote", fontSize=8, textColor=MUTED),
            )
        )
    return story


def _footer(canvas: Any, document: Any) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(14 * mm, 9 * mm, "SiteScope 360 — construction 360° site monitoring")
    canvas.drawRightString(document.pagesize[0] - 14 * mm, 9 * mm, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


_small_cache: dict[bool, ParagraphStyle] = {}


def _small(white: bool = False) -> ParagraphStyle:
    if white not in _small_cache:
        _small_cache[white] = ParagraphStyle(
            f"SSCell{white}",
            fontName="Helvetica",
            fontSize=7.4,
            leading=9.4,
            textColor=colors.white if white else colors.black,
        )
    return _small_cache[white]


def _humanize(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        # Anything in 0..1 named like a ratio reads better as a percentage.
        return f"{value:.1%}" if 0.0 <= value <= 1.0 else f"{value:,.2f}"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "—"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items()) or "—"
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
