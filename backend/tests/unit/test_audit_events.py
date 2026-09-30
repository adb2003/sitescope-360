"""The registry of logged events must match the events actually logged (§45, §46).

`GET /api/system/events` advertises `core.audit.EVENT_NAMES`, because a deployment with no log
aggregator attached still has to be able to answer "what does this thing record". Before this
test, that list was maintained by hand and had drifted to eight names while the code emitted
thirty-five — not a gap in the logging but a false statement about it, served from the endpoint
whose whole job is to describe the system.

So the list is not trusted. This walks the syntax tree of every module in `app/`, collects the
name passed to each `log_event()` call, and requires the two sets to be equal in both
directions: a name emitted and not registered fails, and so does a name registered and never
emitted. The second half matters as much as the first — a registry that over-claims is the same
kind of lie as one that under-claims, and it is the easier one to leave behind after deleting a
feature.

Static analysis rather than runtime capture on purpose. Exercising every branch that can emit an
event would mean provoking every failure path in the platform, and the paths hardest to provoke
are exactly the ones whose logging nobody would otherwise notice was missing.

Only a **literal** first argument counts, deliberately, and that strictness earns its keep: it
caught a call that chose between two names with a conditional expression, which is greppable by
neither of them — and `log_event` exists in the first place so that the observable event names
can be found by grep. A name computed at the call site shows up here as an event the registry
promises and nothing emits, which is the right complaint about it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.core.audit import EVENT_NAMES

APP = Path(__file__).resolve().parents[2] / "app"

#: Destructive or privilege-changing actions that must always be recorded.
#:
#: Named individually rather than inferred from a prefix, because the point is the *specific*
#: list somebody sat down and reasoned about: each of these hands out access, takes it away, or
#: destroys something that cannot be recovered. A future refactor that quietly drops one should
#: fail here and not in an incident.
MUST_BE_LOGGED = frozenset(
    {
        "auth.role_changed",
        "auth.password_reset",
        "auth.user_created",
        "auth.user_deactivated",
        "project.deleted",
        "capture.deleted",
        "capture.file_deleted",
        "floorplan.deleted",
        "location.deleted",
        "annotation.deleted",
    }
)


def emitted_event_names() -> dict[str, list[str]]:
    """Every literal name passed to a `log_event()` call in `app/`, and where from."""
    found: dict[str, list[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "log_event" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value, []).append(
                    f"{path.relative_to(APP.parent)}:{node.lineno}"
                )
    return found


def test_every_logged_event_is_registered() -> None:
    emitted = emitted_event_names()
    unregistered = {name: where for name, where in emitted.items() if name not in EVENT_NAMES}

    assert not unregistered, (
        "These events are emitted but missing from core.audit.EVENT_NAMES, so "
        f"GET /api/system/events under-reports what the platform records: {unregistered}"
    )


def test_every_registered_event_is_actually_emitted() -> None:
    """A registry that over-claims is the same kind of lie as one that under-claims."""
    emitted = emitted_event_names()
    phantom = sorted(name for name in EVENT_NAMES if name not in emitted)

    assert not phantom, (
        "These events are registered in core.audit but nothing emits them, so the API promises "
        f"records that will never exist: {phantom}"
    )


def test_the_destructive_actions_are_all_logged() -> None:
    """The list this whole thing exists for.

    Deleting a capture destroys imagery; deleting a location unplaces every frame that ever
    stood there; changing a role hands somebody the platform. None of these were recorded at all
    until they were looked for.
    """
    emitted = emitted_event_names()
    missing = sorted(MUST_BE_LOGGED - set(emitted))

    assert not missing, f"no audit record is written for: {missing}"


def test_no_event_name_is_emitted_from_nowhere_recognisable() -> None:
    """Names are dotted and lower-case, so a log query can filter on a prefix.

    Cheap, but it is the convention every consumer of these logs assumes, and a single
    `CaptureDeleted` in the middle of it would be invisible to every existing filter.
    """
    odd = sorted(
        name
        for name in emitted_event_names()
        if "." not in name or name != name.lower() or name.strip() != name
    )

    assert not odd, f"event names should be dotted lower-case: {odd}"
