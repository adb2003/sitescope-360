"""Stored objects that no database row points at (§44).

Every deletion path in this platform tries to take its bytes with it, and each one is tested. What
did not exist was any way to ask whether they *had* — so a gap in one of them was silent, permanent
and invisible until somebody happened to look.

One did exist. Splitting an upload re-points frame rows instead of copying hundreds of megabytes,
so a walkthrough carved out of a capture owns frames stored under the *original's* prefix. Deleting
that walkthrough swept its own prefix, which is empty, and left every byte behind: 263 files and
23 MB from a single deletion on a development store. It was found by writing a throwaway script to
compare the filesystem against the database, which is not a thing anybody does on a schedule.

So this is that script, kept. It **reports and never deletes**, which is the whole design:

* an orphan costs storage, and deleting the wrong thing costs somebody their imagery — those are
  not symmetrical, so nothing here is automatic;
* a listing that is merely *stale* looks exactly like a leak. A capture being processed right now
  has frames on disk whose rows arrive moments later, so a sweeper acting on this would race the
  pipeline and win sometimes;
* the number is the useful part. "47 orphans, 12 MB" tells an operator whether to care, and a
  deployment that has never leaked should say zero for ever.

Scoped to one project at a time and capped, because a bucket can hold millions of objects and a
report worth having is not worth an unbounded scan. The caller is told when the listing was cut
short rather than handed a quiet half-answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaptureFile, CaptureFrame, FloorPlan, Project, Report
from app.storage import build_key, get_storage

#: Objects examined in one report, before it says it stopped early.
#:
#: Ten thousand is roughly a year of weekly capture on a large floor, so an ordinary project
#: reports in full; anything past it is a deployment where the answer "there are at least this
#: many" is already enough to act on.
MAX_OBJECTS = 10_000

#: Sample paths returned with the count. Enough to recognise *what* leaked — frames, previews,
#: a whole session — without turning a summary into a file listing.
SAMPLE = 12


@dataclass(slots=True)
class OrphanReport:
    project_id: str
    scanned: int = 0
    referenced: int = 0
    orphaned: int = 0
    orphaned_bytes: int = 0
    truncated: bool = False
    sample: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "scanned": self.scanned,
            "referenced": self.referenced,
            "orphaned": self.orphaned,
            "orphaned_bytes": self.orphaned_bytes,
            "truncated": self.truncated,
            "sample": list(self.sample),
            "note": (
                "Reported, never deleted. An object listed here is one no row points at *now* — "
                "a capture still being processed has frames on disk whose rows arrive moments "
                "later, so treat a fresh upload's objects as expected rather than leaked."
            ),
        }


def find_orphans(db: Session, project: Project, *, limit: int = MAX_OBJECTS) -> OrphanReport:
    """Compare what is stored under one project against what its rows reference."""
    storage = get_storage()
    prefix = build_key("projects", project.id) + "/"
    keys, truncated = storage.list_keys(prefix, limit)

    referenced = _referenced_keys(db, project.id)
    report = OrphanReport(
        project_id=project.id,
        scanned=len(keys),
        truncated=truncated,
    )
    orphans = [key for key in keys if key not in referenced]
    report.referenced = len(keys) - len(orphans)
    report.orphaned = len(orphans)
    report.sample = orphans[:SAMPLE]
    for key in orphans:
        try:
            report.orphaned_bytes += storage.size(key)
        except Exception:  # pragma: no cover - a key listed then removed mid-report
            continue
    return report


def _referenced_keys(db: Session, project_id: str) -> set[str]:
    """Every stored object this project's rows name, through any column that can hold one."""
    keys: set[str] = set()

    frames = db.execute(
        select(
            CaptureFrame.storage_key,
            CaptureFrame.thumbnail_key,
            CaptureFrame.preview_key,
            CaptureFrame.feature_blob_key,
        )
        .join(CaptureFile, CaptureFile.id == CaptureFrame.file_id)
        .where(CaptureFrame.storage_key.startswith(f"projects/{project_id}/"))
    )
    for row in frames:
        keys.update(value for value in row if value)

    # Frames whose *derivatives* live under this project but whose own key does not — a
    # walkthrough split out of an upload is exactly that shape, so matching on one column would
    # report its imagery as orphaned.
    for row in db.execute(
        select(
            CaptureFrame.storage_key,
            CaptureFrame.thumbnail_key,
            CaptureFrame.preview_key,
            CaptureFrame.feature_blob_key,
        ).where(CaptureFrame.thumbnail_key.startswith(f"projects/{project_id}/"))
    ):
        keys.update(value for value in row if value)

    keys.update(
        value
        for value in db.scalars(
            select(CaptureFile.storage_key).where(
                CaptureFile.storage_key.startswith(f"projects/{project_id}/")
            )
        )
        if value
    )
    for plan in db.scalars(
        select(FloorPlan).where(FloorPlan.storage_key.startswith(f"projects/{project_id}/"))
    ):
        if plan.storage_key:
            keys.add(plan.storage_key)
        source = (plan.render or {}).get("source_storage_key")
        if isinstance(source, str):
            keys.add(source)
    keys.update(
        value
        for value in db.scalars(select(Report.storage_key).where(Report.project_id == project_id))
        if value
    )
    return keys
