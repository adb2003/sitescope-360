"""A role is held on a project, not over a deployment (§27).

Membership answered *which* projects somebody could reach. What they could do once there came
from their account, so the answer was the same everywhere: the only way to let a subcontractor's
lead run their own site was to make them a project manager on every job they could see, and the
only way to stop a manager editing a job they had no business in was to leave them off it
entirely. Construction work is not shaped like that — people are engineers on one site, visitors
on the next, and the same company is both on different floors of the same building.

So `project_members` carries a role, the account role becomes a ceiling over it, and
`membership.effective_role` is the only thing that answers "what is this person here".

Two decisions these tests pin down, because both are the kind that quietly rot:

* **The ceiling is applied on every read, not when a role is assigned.** Demoting an account
  demotes it on every project at once, with nobody having to remember which five they are on.
* **A write route must name the capability it needs at the resolver**, because the capability in
  its signature is now checked against the account — the ceiling — and not against the role that
  decides. The last test here reads the route table and fails on any write route that forgot,
  which is the only way a hole of that shape stays closed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.enums import Role
from tests.conftest import auth_headers, join_project


@pytest.fixture
def two_sites(client: TestClient, admin_headers: dict[str, str]):
    """Two projects, each with a floor, and an engineer who is on both — differently.

    The whole feature in one fixture: `site_engineer` by account, an engineer on the first job
    and a viewer on the second.
    """
    sites = {}
    for name, code in (("Tower", "ROLE-A"), ("Depot", "ROLE-B")):
        project = client.post(
            "/api/projects", json={"name": name, "code": code}, headers=admin_headers
        ).json()
        building = client.post(
            f"/api/projects/{project['id']}/buildings", json={"name": "B"}, headers=admin_headers
        ).json()
        floor = client.post(
            f"/api/buildings/{building['id']}/floors",
            json={"name": "L1", "level": 1},
            headers=admin_headers,
        ).json()
        sites[code] = {"project": project["id"], "floor": floor["id"]}
    return sites


@pytest.fixture
def engineer_on_one(client: TestClient, admin_headers, engineer, two_sites):
    """Their headers, plus the two sites: engineer on ROLE-A, viewer on ROLE-B."""
    join_project(
        client, admin_headers, two_sites["ROLE-A"]["project"], engineer.id, Role.SITE_ENGINEER
    )
    join_project(client, admin_headers, two_sites["ROLE-B"]["project"], engineer.id, Role.VIEWER)
    return auth_headers(client, engineer.email), two_sites


def _start_capture(client: TestClient, headers: dict[str, str], floor_id: str):
    return client.post(
        "/api/captures",
        json={"floor_id": floor_id, "capture_source": "human_operator"},
        headers=headers,
    )


# ---- the thing that was impossible before ------------------------------------
def test_the_same_person_can_work_on_one_job_and_only_watch_another(
    client: TestClient, engineer_on_one
) -> None:
    headers, sites = engineer_on_one

    allowed = _start_capture(client, headers, sites["ROLE-A"]["floor"])
    refused = _start_capture(client, headers, sites["ROLE-B"]["floor"])

    assert allowed.status_code == 201, allowed.text
    assert refused.status_code == 403, refused.text
    # Named, so the person reads why rather than guessing. Their account says engineer; the
    # refusal has to talk about where they are.
    assert refused.json()["details"]["project_role"] == "viewer"
    assert "on this project" in refused.json()["message"]


def test_watching_is_still_reading(client: TestClient, engineer_on_one) -> None:
    """A viewer on a job sees it. The capability check must not cost them the read."""
    headers, sites = engineer_on_one

    listed = client.get("/api/projects", headers=headers).json()
    detail = client.get(f"/api/projects/{sites['ROLE-B']['project']}", headers=headers)

    assert {row["code"] for row in listed["items"]} == {"ROLE-A", "ROLE-B"}
    assert detail.status_code == 200, detail.text


def test_each_project_reports_what_the_caller_is_on_it(client: TestClient, engineer_on_one) -> None:
    """What the interface needs: a button it must not offer is worse than no button.

    The capability list on `/auth/me` describes the account, so it cannot answer this — it says
    `capture:create` for this engineer on both jobs, and on one of them that is wrong.
    """
    headers, sites = engineer_on_one

    rows = {
        row["code"]: row for row in client.get("/api/projects", headers=headers).json()["items"]
    }

    assert rows["ROLE-A"]["your_role"] == "site_engineer"
    assert rows["ROLE-B"]["your_role"] == "viewer"
    assert "capture:create" in rows["ROLE-A"]["your_capabilities"]
    assert "capture:create" not in rows["ROLE-B"]["your_capabilities"]
    # And the detail route agrees with the list, since the two compute it differently.
    detail = client.get(f"/api/projects/{sites['ROLE-B']['project']}", headers=headers).json()
    assert detail["your_role"] == "viewer"


# ---- the ceiling -------------------------------------------------------------
def test_an_over_granted_member_is_not_promised_what_they_cannot_do(
    client: TestClient, admin_headers, viewer, two_sites
) -> None:
    """Where the cap is load-bearing, which is not where it first looks like it is.

    For a capability *check* the cap changes nothing, and that is worth saying plainly: the
    account guard has already refused anything the account cannot do, and the capability sets are
    nested, so taking the minimum can only re-refuse what was refused. Deleting the cap leaves
    every authorization test in this file green — it was written, run against the removal, and
    found to prove nothing.

    What it does change is the answer to "what can I do here", which an interface believes. A
    viewer account stored as this project's manager would be handed `floorplan:edit` and
    `report:generate`, draw the buttons for them, and have every one refused by the account
    guard on click. `capped` is why the list says `viewer`.
    """
    site = two_sites["ROLE-A"]
    join_project(client, admin_headers, site["project"], viewer.id, Role.PROJECT_MANAGER)
    headers = auth_headers(client, viewer.email)

    detail = client.get(f"/api/projects/{site['project']}", headers=headers).json()

    assert detail["your_role"] == "viewer"
    assert "floorplan:edit" not in detail["your_capabilities"]
    assert "capture:read" in detail["your_capabilities"]
    # The list computes it with a different query from the detail, so both are asserted.
    row = next(
        item
        for item in client.get("/api/projects", headers=headers).json()["items"]
        if item["id"] == site["project"]
    )
    assert row["your_role"] == "viewer"
    assert "report:generate" not in row["your_capabilities"]


def test_a_project_role_cannot_exceed_the_account(
    client: TestClient, admin_headers, viewer, two_sites
) -> None:
    """Being made a manager here does not make a viewer account into a manager.

    The project role is stored as asked — an administrator is allowed to write it, and may be
    staffing ahead of a promotion — but every check takes the lower of the two. Were it otherwise,
    `settings:project` would be a way to hand out any role in the deployment.

    The refusal here comes from the route's own signature rather than from `assert_can`, and that
    is the ceiling doing its job: a viewer account never holds `capture:create`, so the request
    is turned away before any project is resolved. Asserted as it actually behaves, because a
    test that demanded the project-level message would be describing a worse design — one where
    the account role had stopped meaning anything.
    """
    site = two_sites["ROLE-A"]
    join_project(client, admin_headers, site["project"], viewer.id, Role.PROJECT_MANAGER)
    headers = auth_headers(client, viewer.email)

    refused = _start_capture(client, headers, site["floor"])

    assert refused.status_code == 403, refused.text
    assert refused.json()["details"]["required_capability"] == "capture:create"


def test_the_members_list_shows_the_stored_role_and_the_one_in_force(
    client: TestClient, admin_headers, viewer, two_sites
) -> None:
    """Both, because they differ here, and a list showing one of them cannot explain the other."""
    site = two_sites["ROLE-A"]
    join_project(client, admin_headers, site["project"], viewer.id, Role.PROJECT_MANAGER)

    members = client.get(f"/api/projects/{site['project']}/members", headers=admin_headers).json()

    entry = next(row for row in members if row["user_id"] == viewer.id)
    assert entry["role"] == "project_manager"
    assert entry["account_role"] == "viewer"
    assert entry["effective_role"] == "viewer"


def test_lowering_an_account_lowers_every_project_at_once(
    client: TestClient, admin_headers, manager, two_sites
) -> None:
    """The reason the minimum is taken on read rather than written down once.

    An administrator demoting somebody should not have to find the jobs they are on. If the
    stored project role were the answer, this test would need a sweep to pass — and a sweep is
    the thing that is correct the day it is written and half-applied a year later.
    """
    site = two_sites["ROLE-A"]
    join_project(client, admin_headers, site["project"], manager.id, Role.PROJECT_MANAGER)
    headers = auth_headers(client, manager.email)
    assert (
        client.patch(
            f"/api/projects/{site['project']}",
            json={"client": "Before"},
            headers=headers,
        ).status_code
        == 200
    )

    demoted = client.patch(
        f"/api/users/{manager.id}", json={"role": "viewer"}, headers=admin_headers
    )
    assert demoted.status_code == 200, demoted.text

    # A new token, because a role change invalidates the old one on purpose (`deps` refuses a
    # token whose role no longer matches the account).
    refused = client.patch(
        f"/api/projects/{site['project']}",
        json={"client": "After"},
        headers=auth_headers(client, manager.email),
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["details"]["required_capability"] == "project:write"
    # The membership still says `project_manager`. Nothing swept it, and nothing needed to.
    members = client.get(f"/api/projects/{site['project']}/members", headers=admin_headers).json()
    entry = next(row for row in members if row["user_id"] == manager.id)
    assert entry["role"] == "project_manager"
    assert entry["effective_role"] == "viewer"


# ---- who may hand out what ---------------------------------------------------
def test_nobody_grants_a_role_above_their_own(
    client: TestClient, admin_headers, manager, viewer, two_sites
) -> None:
    """`settings:project` must not be a path to `settings:system`.

    A project manager staffing their own job could otherwise make somebody an administrator and
    ask them for whatever they wanted next.
    """
    site = two_sites["ROLE-A"]
    join_project(client, admin_headers, site["project"], manager.id, Role.PROJECT_MANAGER)
    join_project(client, admin_headers, site["project"], viewer.id, Role.VIEWER)
    headers = auth_headers(client, manager.email)

    refused = client.patch(
        f"/api/projects/{site['project']}/members/{viewer.id}",
        json={"role": "admin"},
        headers=headers,
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["details"]["requested_role"] == "admin"
    assert refused.json()["details"]["your_role"] == "project_manager"


def test_a_manager_can_promote_within_their_own_ceiling(
    client: TestClient, admin_headers, manager, engineer, two_sites
) -> None:
    """The point of the route: staffing a job is the manager's own work, not a support request."""
    site = two_sites["ROLE-A"]
    join_project(client, admin_headers, site["project"], manager.id, Role.PROJECT_MANAGER)
    join_project(client, admin_headers, site["project"], engineer.id, Role.VIEWER)
    headers = auth_headers(client, manager.email)

    promoted = client.patch(
        f"/api/projects/{site['project']}/members/{engineer.id}",
        json={"role": "site_engineer"},
        headers=headers,
    )

    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "site_engineer"
    # In force immediately, with the token they already hold: the project role is not in the
    # access token, which is the reason it can change without signing anybody out.
    allowed = _start_capture(client, auth_headers(client, engineer.email), site["floor"])
    assert allowed.status_code == 201, allowed.text


def test_a_role_change_is_recorded(client: TestClient, admin_headers, viewer, two_sites) -> None:
    """A privilege change with no trace is the thing the audit registry exists for."""
    site = two_sites["ROLE-A"]
    join_project(client, admin_headers, site["project"], viewer.id, Role.VIEWER)

    client.patch(
        f"/api/projects/{site['project']}/members/{viewer.id}",
        json={"role": "site_engineer"},
        headers=admin_headers,
    )

    body = client.get("/api/system/events", headers=admin_headers).json()
    assert "project.member_role_changed" in set(body["logged_event_names"])


def test_changing_the_role_of_somebody_not_on_the_project_is_a_404(
    client: TestClient, admin_headers, viewer, two_sites
) -> None:
    site = two_sites["ROLE-A"]

    refused = client.patch(
        f"/api/projects/{site['project']}/members/{viewer.id}",
        json={"role": "site_engineer"},
        headers=admin_headers,
    )

    assert refused.status_code == 404, refused.text


# ---- the two refusals mean different things ----------------------------------
def test_a_non_member_is_not_told_the_project_exists(
    client: TestClient, admin_headers, engineer, two_sites
) -> None:
    """404 for a stranger, 403 for a member — the distinction `assert_can` keeps.

    A 403 on a project somebody was never told about confirms that it exists, and project codes
    are short and guessable. A member looking at a button that did not work needs the opposite:
    "not found" is a lie they cannot act on.
    """
    site = two_sites["ROLE-A"]
    headers = auth_headers(client, engineer.email)

    stranger = _start_capture(client, headers, site["floor"])
    join_project(client, admin_headers, site["project"], engineer.id, Role.VIEWER)
    member = _start_capture(client, auth_headers(client, engineer.email), site["floor"])

    assert stranger.status_code == 404, stranger.text
    assert member.status_code == 403, member.text


def test_an_administrator_needs_no_membership(client: TestClient, admin_headers, two_sites) -> None:
    """Unchanged, and worth a test here: the exemption is now the only path with no role row."""
    site = two_sites["ROLE-B"]

    created = _start_capture(client, admin_headers, site["floor"])

    assert created.status_code == 201, created.text
    assert (
        client.get(f"/api/projects/{site['project']}", headers=admin_headers).json()["your_role"]
        == "admin"
    )


# ---- the guard ---------------------------------------------------------------
def test_every_write_route_names_the_capability_it_needs() -> None:
    """The hole this feature would otherwise leave open, closed by reading the route table.

    A route's signature declares a capability, and that declaration is checked against the
    **account** role — which is now only a ceiling. The role that decides is the one held on the
    project, and the only place that is known is the resolver. So a write route that resolves its
    resource without saying what it is doing is checked against the ceiling alone: a viewer on
    this job with an engineer's account would pass.

    Source rather than responses, for the reason `test_project_scoping.py` gives: the writes are
    exactly the routes a sweep over live endpoints cannot reach, because they need a body.
    """
    import ast
    from pathlib import Path

    # Guard alias in the signature → the capability a project-scoped check must name.
    WRITE_GUARDS = {
        "RequireProjectWrite": "PROJECT_WRITE",
        "RequireProjectDelete": "PROJECT_DELETE",
        "RequireCaptureCreate": "CAPTURE_CREATE",
        "RequireCaptureDelete": "CAPTURE_DELETE",
        "RequireUpload": "UPLOAD",
        "RequireAnnotate": "ANNOTATE",
        "RequireFloorPlanEdit": "FLOORPLAN_EDIT",
        "RequireLocationEdit": "LOCATION_EDIT",
        "RequireReportGenerate": "REPORT_GENERATE",
        "RequireProjectSettings": "SETTINGS_PROJECT",
    }
    # The ids that belong to a project. A route addressed by something else — a camera device is
    # registered for the deployment, not for a job — has no project to scope to, and its account
    # capability is the only check there can be.
    SCOPED = (
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

    silent: list[str] = []
    disagreeing: list[str] = []
    routes_dir = Path(__file__).resolve().parents[2] / "app" / "api" / "routes"
    for module in sorted(routes_dir.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
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
            if not any(token in path for path in paths for token in SCOPED):
                continue
            declared = {
                arg.annotation.id
                for arg in [*node.args.args, *node.args.kwonlyargs]
                if isinstance(arg.annotation, ast.Name)
            } & set(WRITE_GUARDS)
            if not declared:
                continue
            expected = {WRITE_GUARDS[guard] for guard in declared}

            named: set[str] = set()
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                for keyword in call.keywords:
                    if keyword.arg != "needs":
                        continue
                    value = keyword.value
                    if isinstance(value, ast.Attribute):
                        named.add(value.attr)

            where = f"{module.name}:{node.lineno} {node.name}"
            if not named:
                silent.append(f"{where} declares {sorted(expected)} and names nothing")
            elif not named & expected:
                disagreeing.append(f"{where} declares {sorted(expected)}, asks for {sorted(named)}")

    assert not silent, (
        "These routes write to a project and never say what capability that needs, so they are "
        f"judged on the account role — the ceiling — and not on the role held here: {silent}"
    )
    assert not disagreeing, (
        "These routes ask their resolver for a different capability than their signature "
        f"declares, so one of the two is wrong: {disagreeing}"
    )
