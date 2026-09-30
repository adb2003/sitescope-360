"""Every domain event the platform emits, in one place (§45).

`GET /api/system/events` advertises this list, because a deployment without a log aggregator
attached still has to be able to answer "what does this thing record". That endpoint used to
carry a hard-coded list of eight names while the code emitted thirty-five, which is the worst
version of the problem: not a gap in the logging but a false statement about it, sitting inside
an API response where a reader would take it for a fact.

So the list lives here and the endpoint reads it. What keeps it true is not diligence but
`tests/unit/test_audit_events.py`, which parses every `log_event()` call in `app/` and asserts
the two sets match exactly — a name emitted and not listed fails, and so does a name listed and
never emitted. Adding an event means adding a line here, and the test says so when you forget.

The grouping below is for a reader, not for the code; nothing depends on the order.
"""

from __future__ import annotations

#: Authentication and account administration.
#:
#: These exist because a role change is a privilege change and left no trace at all until it was
#: noticed: "who gave this account admin, and when" had no answer. A password reset and a
#: deactivation are recorded for the same reason — each hands or removes access to somebody.
AUTH_EVENTS: tuple[str, ...] = (
    "auth.login",
    "auth.user_created",
    "auth.user_deactivated",
    "auth.user_activated",
    "auth.role_changed",
    "auth.password_changed",
    "auth.password_reset",
)

#: Irreversible deletions. Each carries what went with it — a count nothing can recover once the
#: row is gone, which is the whole reason to write it down before deleting.
DELETION_EVENTS: tuple[str, ...] = (
    "project.deleted",
    "capture.deleted",
    "capture.file_deleted",
    "floorplan.deleted",
    "location.deleted",
    "annotation.deleted",
)

#: Capture lifecycle: upload, processing, and the stages of it.
CAPTURE_EVENTS: tuple[str, ...] = (
    "capture.created",
    "capture.split",
    "capture.frame_located",
    "file.uploaded",
    "upload.url_issued",
    "upload.multipart_started",
    "upload.multipart_completed",
    "upload.multipart_aborted",
    "processing.started",
    "processing.stage.started",
    "processing.stage.completed",
    "processing.frames_extracted",
    "processing.completed",
    "processing.failed",
    "storage.stale_uploads_purged",
)

#: What the vision pipeline concluded, and how a capture came to sit on a plan.
MAPPING_EVENTS: tuple[str, ...] = (
    "mapping.completed",
    "mapping.anchored",
    "mapping.cross_session",
    "mapping.placement_cleared",
    "grouping.breaks_found",
)

#: Everything else a person did that someone might later ask about.
ACTIVITY_EVENTS: tuple[str, ...] = (
    "api.ready",
    "project.created",
    "project.member_added",
    "project.member_removed",
    "floor.created",
    "floorplan.uploaded",
    "floorplan.sheet_chosen",
    "annotation.created",
    "report.generated",
    "share.created",
    "share.revoked",
    "viewer.opened",
)

#: The whole set, which is what the API reports.
EVENT_NAMES: tuple[str, ...] = tuple(
    sorted(
        {
            *AUTH_EVENTS,
            *DELETION_EVENTS,
            *CAPTURE_EVENTS,
            *MAPPING_EVENTS,
            *ACTIVITY_EVENTS,
        }
    )
)
