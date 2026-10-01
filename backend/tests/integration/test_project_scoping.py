"""Which projects a user can reach (§27).

Roles said what a user could do. Nothing said which projects they could do it to, so every
authenticated account could read every project in the deployment: a subcontractor's viewer could
open the main contractor's other jobs, and no role change could prevent it because "viewer" was
never meant to mean "viewer of everything".

These tests are the enforcement, and they are deliberately about the *leaks* rather than the
happy path. A scoping check is easy to add at the obvious door and forget at the five others, so
each test below picks one route family and asks the same question: given an id from a project I
am not on, what do I get?

The answer is **404 and not 403** throughout. A 403 on a project a stranger was never told about
confirms that it exists, and project codes are short and guessable. A member who has genuinely
lost access sees the same 404 — a worse message for them, and the right trade, because they can
ask a person and an enumerator cannot.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers, join_project


@pytest.fixture
def two_projects(client: TestClient, admin_headers: dict[str, str]):
    """Two projects, and a viewer who is a member of the first only."""
    mine = client.post(
        "/api/projects", json={"name": "Mine", "code": "SCOPE-MINE"}, headers=admin_headers
    ).json()
    theirs = client.post(
        "/api/projects", json={"name": "Theirs", "code": "SCOPE-THEIRS"}, headers=admin_headers
    ).json()
    return mine, theirs


@pytest.fixture
def outsider(client: TestClient, admin_headers: dict[str, str], viewer, two_projects):
    """A viewer on the first project and not the second, with their headers."""
    mine, theirs = two_projects
    join_project(client, admin_headers, mine["id"], viewer.id)
    return auth_headers(client, viewer.email), mine, theirs


def _floor_in(client: TestClient, headers: dict[str, str], project_id: str) -> tuple[str, str]:
    building = client.post(
        f"/api/projects/{project_id}/buildings", json={"name": "B"}, headers=headers
    ).json()
    floor = client.post(
        f"/api/buildings/{building['id']}/floors", json={"name": "L1", "level": 1}, headers=headers
    ).json()
    return str(building["id"]), str(floor["id"])


# ---- the lists ---------------------------------------------------------------
def test_the_project_list_shows_only_your_projects(outsider, client) -> None:
    """The front door. Everything else is reached from here, or from a guessed id."""
    headers, _mine, _theirs = outsider

    listed = client.get("/api/projects", headers=headers).json()

    codes = {row["code"] for row in listed["items"]}
    assert "SCOPE-MINE" in codes
    assert "SCOPE-THEIRS" not in codes
    # The total must narrow too. A count that includes projects the rows omit tells the caller
    # how many other jobs exist, which is most of what the filter is protecting.
    assert listed["total"] == len(listed["items"])


def test_the_portfolio_totals_count_only_your_projects(outsider, client) -> None:
    """A portfolio is a total. Summing every job would report the whole deployment's workload."""
    headers, _mine, _theirs = outsider

    portfolio = client.get("/api/dashboard", headers=headers).json()

    assert portfolio["total_projects"] == 1
    assert len(portfolio["projects"]) == 1


def test_the_capture_list_shows_only_your_captures(
    outsider, client, admin_headers, panorama_factory
) -> None:
    """Captures carry the imagery, so an unfiltered list is the widest leak of the lot."""
    headers, mine, theirs = outsider
    _b, my_floor = _floor_in(client, admin_headers, mine["id"])
    _b2, their_floor = _floor_in(client, admin_headers, theirs["id"])
    client.post("/api/captures", json={"floor_id": my_floor}, headers=admin_headers)
    client.post("/api/captures", json={"floor_id": their_floor}, headers=admin_headers)

    listed = client.get("/api/captures", headers=headers).json()

    floors = {row["floor_id"] for row in listed["items"]}
    assert floors == {my_floor}


def test_the_offline_sync_ships_only_your_reference_data(outsider, client, admin_headers) -> None:
    """The widest read in the API, and the one a phone keeps after it disconnects."""
    headers, mine, theirs = outsider
    _b, my_floor = _floor_in(client, admin_headers, mine["id"])
    _b2, their_floor = _floor_in(client, admin_headers, theirs["id"])

    synced = client.get("/api/sync", headers=headers).json()

    project_ids = {row["id"] for row in synced["projects"]["changed"]}
    floor_ids = {row["id"] for row in synced["floors"]["changed"]}
    assert project_ids == {mine["id"]}
    assert my_floor in floor_ids
    assert their_floor not in floor_ids
    # The manifest is how a client learns what to delete, so a leak there is the same leak.
    assert {entry["id"] for entry in synced["projects"]["manifest"]} == {mine["id"]}


# ---- reaching a single resource by id ---------------------------------------
def test_another_projects_id_is_not_found_rather_than_forbidden(outsider, client) -> None:
    headers, _mine, theirs = outsider

    response = client.get(f"/api/projects/{theirs['id']}", headers=headers)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


def test_a_floor_in_another_project_is_out_of_reach(outsider, client, admin_headers) -> None:
    """Floors are addressed directly, so the check has to walk up to the project."""
    headers, _mine, theirs = outsider
    building_id, floor_id = _floor_in(client, admin_headers, theirs["id"])

    assert client.get(f"/api/floors/{floor_id}", headers=headers).status_code == 404
    assert client.get(f"/api/buildings/{building_id}/floors", headers=headers).status_code == 404
    assert client.get(f"/api/floors/{floor_id}/locations", headers=headers).status_code == 404


def test_a_capture_in_another_project_is_out_of_reach(outsider, client, admin_headers) -> None:
    headers, _mine, theirs = outsider
    _b, their_floor = _floor_in(client, admin_headers, theirs["id"])
    session_id = client.post(
        "/api/captures", json={"floor_id": their_floor}, headers=admin_headers
    ).json()["id"]

    assert client.get(f"/api/captures/{session_id}", headers=headers).status_code == 404
    assert client.get(f"/api/captures/{session_id}/frames", headers=headers).status_code == 404
    assert client.get(f"/api/captures/{session_id}/status", headers=headers).status_code == 404


def test_a_report_on_another_project_is_out_of_reach(outsider, client, admin_headers) -> None:
    """A report is not a route to the figures — it *is* the figures."""
    headers, _mine, theirs = outsider
    report = client.post(
        f"/api/projects/{theirs['id']}/reports",
        json={"report_type": "project_summary", "report_format": "json"},
        headers=admin_headers,
    ).json()

    assert client.get(f"/api/reports/{report['id']}", headers=headers).status_code == 404
    assert client.get(f"/api/reports/{report['id']}/download", headers=headers).status_code == 404


def test_writes_to_another_project_are_refused_too(
    outsider, client, admin_headers, engineer
) -> None:
    """Reads are the obvious half. A capture created on someone else's floor is worse.

    The engineer holds `capture:create`, so the capability check passes and the only thing
    standing between them and writing into another company's job is membership.
    """
    _headers, _mine, theirs = outsider
    _b, their_floor = _floor_in(client, admin_headers, theirs["id"])
    engineer_headers = auth_headers(client, engineer.email)

    created = client.post("/api/captures", json={"floor_id": their_floor}, headers=engineer_headers)

    assert created.status_code == 404, created.text


# ---- the admin exemption ----------------------------------------------------
def test_an_administrator_reaches_every_project(client, admin_headers, two_projects) -> None:
    """Not a loophole: an admin holds `settings:system` and `user:manage`.

    A deployment where the administrator could be locked out of a project would have nobody able
    to fix it except the person who is locked out.
    """
    _mine, theirs = two_projects

    assert client.get(f"/api/projects/{theirs['id']}", headers=admin_headers).status_code == 200
    listed = client.get("/api/projects", headers=admin_headers).json()
    assert {"SCOPE-MINE", "SCOPE-THEIRS"} <= {row["code"] for row in listed["items"]}


# ---- managing membership ----------------------------------------------------
def test_whoever_creates_a_project_can_still_see_it(client, manager) -> None:
    """The bug this would obviously have had: create a job, lose it immediately."""
    headers = auth_headers(client, manager.email)

    created = client.post(
        "/api/projects", json={"name": "Mine to keep", "code": "SCOPE-OWN"}, headers=headers
    )

    assert created.status_code == 201, created.text
    assert client.get(f"/api/projects/{created.json()['id']}", headers=headers).status_code == 200


def test_granting_access_is_what_makes_a_project_visible(
    client, admin_headers, viewer, two_projects
) -> None:
    """The whole mechanism, in one test: before and after."""
    _mine, theirs = two_projects
    headers = auth_headers(client, viewer.email)
    assert client.get(f"/api/projects/{theirs['id']}", headers=headers).status_code == 404

    join_project(client, admin_headers, theirs["id"], viewer.id)

    assert client.get(f"/api/projects/{theirs['id']}", headers=headers).status_code == 200


def test_access_can_be_taken_away_again(client, admin_headers, viewer, two_projects) -> None:
    _mine, theirs = two_projects
    headers = auth_headers(client, viewer.email)
    join_project(client, admin_headers, theirs["id"], viewer.id)

    removed = client.delete(
        f"/api/projects/{theirs['id']}/members/{viewer.id}", headers=admin_headers
    )

    assert removed.status_code == 200, removed.text
    assert client.get(f"/api/projects/{theirs['id']}", headers=headers).status_code == 404


def test_granting_the_same_access_twice_is_not_an_error(
    client, admin_headers, viewer, two_projects
) -> None:
    """A retried request, or two administrators doing the same obvious thing."""
    _mine, theirs = two_projects

    first = join_project(client, admin_headers, theirs["id"], viewer.id)
    second = join_project(client, admin_headers, theirs["id"], viewer.id)

    assert first["user_id"] == second["user_id"]
    members = client.get(f"/api/projects/{theirs['id']}/members", headers=admin_headers).json()
    assert sum(1 for m in members if m["user_id"] == viewer.id) == 1


def test_you_cannot_staff_a_project_you_are_not_on(
    client, admin_headers, manager, viewer, two_projects
) -> None:
    """`settings:project` says a manager may staff projects; membership says which ones."""
    _mine, theirs = two_projects
    headers = auth_headers(client, manager.email)

    refused = client.post(
        f"/api/projects/{theirs['id']}/members", json={"user_id": viewer.id}, headers=headers
    )

    assert refused.status_code == 404, refused.text


def test_the_member_list_states_each_persons_role(
    client, admin_headers, viewer, two_projects
) -> None:
    """Membership is not a role. The list has to show both, or it implies it is one."""
    _mine, theirs = two_projects
    join_project(client, admin_headers, theirs["id"], viewer.id)

    members = client.get(f"/api/projects/{theirs['id']}/members", headers=admin_headers).json()

    entry = next(m for m in members if m["user_id"] == viewer.id)
    assert entry["role"] == "viewer"
    assert entry["email"] == viewer.email


def test_a_member_can_be_named_by_email(client, admin_headers, viewer, two_projects) -> None:
    """The only option a project manager has: listing users needs `user:manage`.

    A manager staffing their own job cannot look up an id, and email is not a leak of the user
    list — you have to know the address to use it.
    """
    _mine, theirs = two_projects

    added = client.post(
        f"/api/projects/{theirs['id']}/members",
        json={"email": viewer.email},
        headers=admin_headers,
    )

    assert added.status_code == 201, added.text
    assert added.json()["user_id"] == viewer.id


def test_naming_a_member_both_ways_at_once_is_refused(
    client, admin_headers, viewer, two_projects
) -> None:
    """Two identifiers is a client bug, and silently preferring one would hide it."""
    _mine, theirs = two_projects

    refused = client.post(
        f"/api/projects/{theirs['id']}/members",
        json={"user_id": viewer.id, "email": viewer.email},
        headers=admin_headers,
    )

    assert refused.status_code == 422, refused.text


# ---- the five other doors ----------------------------------------------------
def test_the_reporting_views_of_another_project_are_out_of_reach(
    outsider, client, admin_headers
) -> None:
    """Six routes answered these questions for anyone with an account.

    Each of them took the project id, filtered on it, and never resolved it — the signature was
    `_: RequireProjectRead`, which checks the *capability* and discards the user, so there was
    no user to check membership with and nothing for a type checker to notice. `project_dashboard`
    sits directly above `project_timeline` in the same module doing it correctly, which is how
    the pattern survived.

    Between them they are a readable account of a job's progress: what has been inspected, how
    often, what is overdue, and every defect raised with its title and description.
    """
    headers, _mine, theirs = outsider

    for path in (
        f"/api/projects/{theirs['id']}/timeline",
        f"/api/projects/{theirs['id']}/coverage",
        f"/api/projects/{theirs['id']}/frequency",
        f"/api/projects/{theirs['id']}/stale-areas",
        f"/api/projects/{theirs['id']}/annotations",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 404, f"{path} answered: {response.text[:200]}"
        assert response.json()["code"] == "not_found"


def test_the_walked_routes_on_another_projects_floor_are_out_of_reach(
    outsider, client, admin_headers
) -> None:
    """A capture path is the shape of somebody's walk through somebody else's building."""
    headers, _mine, theirs = outsider
    _building, floor_id = _floor_in(client, admin_headers, theirs["id"])

    response = client.get(f"/api/floors/{floor_id}/paths", headers=headers)

    assert response.status_code == 404, response.text


def test_every_route_taking_a_scoped_id_enforces_membership() -> None:
    """The guard, so this class of hole cannot come back quietly.

    Reads the source rather than the responses, because the alternative — calling every route as
    a non-member — cannot cover the ones needing a request body, and those are the writes. A
    route that takes somebody else's id must *resolve* it through a helper that checks
    membership; filtering a query by an id the caller supplied is not a check, it is the
    question.

    The resolver list is maintained by hand and that is deliberate: adding a new way to reach a
    project should be a decision somebody makes, and this test failing is how it gets made.
    """
    import ast
    from pathlib import Path

    resolvers = {
        "get_project_or_404",
        "get_building_or_404",
        "get_floor_or_404",
        "get_session_or_404",
        "get_frame_or_404",
        "get_zone_or_404",
        "get_plan_or_404",
        "get_location_or_404",
        "get_report_or_404",
        "get_capture_file_or_404",
        "assert_member",
        "restrict_to_member_projects",
        "visible_project_ids",
        "member_floor_ids",
        "is_member",
        # Three modules resolve an id addressed directly — an annotation, a report — through a
        # local helper that walks up to the project. Named here rather than inlined, because the
        # walk is the same three lines in each and hiding it in the route would be worse.
        "_get_or_404",
    }
    scoped = (
        "{project_id}",
        "{building_id}",
        "{floor_id}",
        "{session_id}",
        "{frame_id}",
        "{zone_id}",
        "{plan_id}",
        "{location_id}",
        "{report_id}",
        "{file_id}",
        "{annotation_id}",
    )

    unguarded: list[str] = []
    routes_dir = Path(__file__).resolve().parents[2] / "app" / "api" / "routes"
    for module in sorted(routes_dir.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            paths = [
                decorator.args[0].value
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ]
            touched = [path for path in paths if any(token in path for token in scoped)]
            if not touched:
                continue
            called = {
                call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            }
            if not called & resolvers:
                unguarded.extend(f"{module.name}:{node.lineno} {path}" for path in touched)

    assert not unguarded, (
        "These routes take an id belonging to a project and never resolve it through a "
        f"membership check, so any account can read or write it: {unguarded}"
    )
