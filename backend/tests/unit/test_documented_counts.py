"""The API reference has to describe this API, not an earlier one.

`docs/API.md` opens by stating how many endpoints exist. That number was written once and then
quietly became wrong — it said 99 across 71 paths while the application served 120 across 89,
because five features had shipped since anybody read the first line of the file.

It matters more than a stale number usually would. The whole document is a claim about what this
service does, and the first sentence being demonstrably false is the cheapest possible reason for
a reader to stop trusting the rest of it.

So the count is checked rather than maintained. This test fails when an endpoint is added or
removed without the reference being updated, which is exactly the moment somebody is in a
position to update it — and it costs one line of a diff rather than a periodic audit nobody
schedules.

Only this one number is guarded. The test counts in `README.md` are deliberately left alone: a
test asserting how many tests exist changes its own answer, and a guard that must be edited every
time anything is added is noise rather than a signal.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.main import app

DOCS = Path(__file__).resolve().parents[2].parent / "docs" / "API.md"

#: "120 endpoints across 89 paths." — the opening claim of the reference.
CLAIM = re.compile(r"(\d+) endpoints across (\d+) paths")

#: What counts as an endpoint: an HTTP operation somebody can call.
METHODS = {"get", "post", "put", "patch", "delete"}


def _actual() -> tuple[int, int]:
    paths = app.openapi()["paths"]
    endpoints = sum(len(METHODS & set(operations)) for operations in paths.values())
    return endpoints, len(paths)


def test_the_api_reference_counts_the_api_it_documents() -> None:
    assert DOCS.exists(), f"expected the API reference at {DOCS}"
    match = CLAIM.search(DOCS.read_text(encoding="utf-8"))
    assert match, "docs/API.md no longer opens with an endpoint count; update this test with it"

    documented = (int(match.group(1)), int(match.group(2)))
    actual = _actual()

    assert documented == actual, (
        f"docs/API.md says {documented[0]} endpoints across {documented[1]} paths; "
        f"the application serves {actual[0]} across {actual[1]}. Update the first line of "
        "docs/API.md — and the endpoint table below it, which is the part a reader actually uses."
    )
