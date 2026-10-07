# API reference

126 endpoints across 95 paths. Base prefix `/api`.

Interactive documentation is generated from the code and is the authoritative source:

| | |
| --- | --- |
| Swagger UI | <http://localhost:8000/docs> |
| ReDoc | <http://localhost:8000/redoc> |
| OpenAPI schema | <http://localhost:8000/openapi.json> |

This document covers the cross-cutting conventions and lists every endpoint with its purpose.

---

## Conventions

### Authentication

Local email + password, exchanged for a short-lived JWT access token and a rotating refresh
token. Send the access token as a bearer header:

```http
Authorization: Bearer <access_token>
```

```bash
TOKEN=$(curl -s localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@demo.example.com","password":"SiteScope360!demo"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -s localhost:8000/api/dashboard -H "Authorization: Bearer $TOKEN"
```

Refresh tokens **rotate**: presenting one revokes it and issues a new pair. Replaying a consumed
refresh token returns 401 — that is deliberate reuse detection, and it is why the web client
shares a single in-flight refresh across concurrent 401s.

Two things happen immediately rather than at token expiry:

- Changing a user's role invalidates their existing access tokens
  (`"Your permissions changed — please sign in again"`).
- Changing a password revokes every refresh token for that user.

Only `GET /health`, `GET /api/system/health`, `POST /api/auth/login`, `POST /api/auth/refresh`,
`GET /api/files/{key}` and `GET /api/shared/{token}` are reachable without a token. Authentication is checked **before** body
validation, so an unauthenticated caller learns nothing about whether their payload was
acceptable.

#### Sign-in is rate limited

Both auth routes are limited, and a refusal is **429** with `Retry-After` (also in the body, since
a browser fetch cannot read the header cross-origin unless it is exposed — it is).

| Limit | Default | Counts | Why |
| --- | --- | --- | --- |
| Per client address | 60 / 5 min | *every* attempt | CPU exhaustion |
| Per account | 10 failures / 15 min | failures only | brute force |

The address limit exists because of cost asymmetry rather than guessing: bcrypt is deliberately
slow, so every request costs this server a fraction of a CPU-second and costs the caller nothing.
A few hundred concurrent requests saturate the API without a single correct password. It is
therefore checked **before** the password is verified — a limiter that ran afterwards would have
already paid the cost it exists to avoid — and it counts successes too, because a successful
login costs exactly the same bcrypt time as a failed one.

The account limit counts only failures, and a successful sign-in clears the count: someone
signing in correctly all day is not attacking anything, and leaving a user one mistake from
lockout after they have demonstrated the right password buys nothing.

**Two honest caveats.** The account limit can be spent *against* a known address by someone who
wants that person locked out; that is the accepted trade against unbounded targeted brute force,
mitigated by keeping the window short and the lockout self-expiring. And the limiter **fails
open** — if its Redis is unreachable, requests are allowed and a warning is logged, because a
limiter that failed closed would turn a broker blip into an outage of the one endpoint everything
depends on.

Defaults are tunable: `AUTH_ADDRESS_MAX_ATTEMPTS`, `AUTH_ADDRESS_WINDOW_SECONDS`,
`AUTH_ACCOUNT_MAX_FAILURES`, `AUTH_ACCOUNT_WINDOW_SECONDS`. Zero disables either limit, for a
deployment that limits at its edge instead.

**`TRUST_PROXY_HEADERS` matters for this to work.** Behind a proxy every request arrives from the
proxy's address, so the per-address limit becomes one global bucket for the whole site. Turning
it on makes the API key on `X-Real-IP` instead — which is safe **only** when every route to the
service passes through a proxy that overwrites that header, as `infrastructure/nginx/nginx.conf`
does. It is off by default, and `docker-compose.yml` turns it on for the containerised stack.
Note this uses `X-Real-IP` rather than the first entry of `X-Forwarded-For`: nginx *appends* to
`X-Forwarded-For`, so its leftmost value is whatever the caller sent, and limiting on it would
let anyone bypass the limit by rotating a fake header.

nginx applies its own `limit_req` to the two sign-in routes as a second layer. The API's limit is
better informed; nginx's is the only one that can decline before a worker is occupied.

#### Request bodies are capped per route

**1 MiB by default**, raised only where a large body is the point:

| Route | Ceiling |
| --- | --- |
| `PUT /api/files/…` | 2 GiB — direct uploads and multipart parts |
| `POST /api/captures/{id}/upload` | 2 GiB — the proxied upload |
| `POST /api/floors/{id}/floorplan` | 128 MiB — the route itself caps at 64 MiB |
| everything else | 1 MiB |

Over the ceiling is **413** with the usual `{code, message}` envelope and the limit in `details`.

This was previously 2 GB everywhere, set once at nginx's http level because one route uploads
video. The route that made it matter is `/auth/login`: it needs no credentials, and FastAPI
parses a request body to build the endpoint's arguments **before** the function runs — so the
sign-in rate limiter, which lives inside the function, never saw a huge body. The limit is
therefore enforced by middleware that answers before routing and body parsing, and by nginx.

Enforced in **both** places on purpose. Neither Starlette nor uvicorn imposes a default body
limit, so an API exposed without the gateway had no ceiling at all; and nginx can refuse before a
worker is occupied, which the application cannot.

**One stated limitation:** the application checks `Content-Length` only. A request that declares
no length (chunked transfer) is bounded by nginx, which enforces its limit across the accumulated
body, but not by the middleware — counting bytes as they stream would mean wrapping `receive` in
front of a 2 GiB upload, and a bug there breaks the uploads this project works hardest to keep
reliable. Tunable with `MAX_JSON_BODY_BYTES`.

### Authorization

Capability-based, derived from the §27 role matrix and enforced as a route dependency. A refusal
names the missing capability:

```json
{
  "code": "permission_denied",
  "message": "Your role (viewer) cannot perform this action",
  "details": { "required_capability": "project:write" }
}
```

`GET /api/auth/me` returns the caller's capability list; the web app gates its UI on that rather
than re-deriving the role table.

| Role | Capabilities |
| --- | --- |
| `viewer` | `project:read`, `capture:read` |
| `site_engineer` | + `capture:create`, `upload`, `annotate`, `location:edit` |
| `project_manager` | + `project:write`, `capture:delete`, `annotation:resolve`, `floorplan:edit`, `report:generate`, `settings:project` |
| `admin` | everything, including `settings:system`, `user:manage`, `job:admin`, `project:delete` |

A test asserts capabilities grow monotonically with role — a more privileged role can never lose
one a lesser role has.

#### Projects are scoped to their members

The role matrix says *what* a user may do. `project_members` says *which projects* they may do it
to, and the two are separate on purpose: membership is not a second role, and a user has one
global role everywhere. (Per-project roles — project manager on one job, viewer on another —
would be a redesign of the matrix rather than an addition to it, and are not implemented.)

| | |
| --- | --- |
| `GET /projects/{id}/members` | Who can see this project. Readable by any member. |
| `POST /projects/{id}/members` | Grant access. Needs `settings:project` **and** membership. |
| `DELETE /projects/{id}/members/{user_id}` | Take it away. |

Three things worth knowing:

**A non-member gets 404, not 403.** A 403 on a project a stranger was never told about confirms
that it exists, and project codes are short and guessable. A member who has genuinely lost access
sees the same 404 — worse for them, and the right trade, because they can ask a person and an
enumerator cannot.

**Administrators are exempt.** They hold `settings:system` and `user:manage`, so a deployment
where an admin could be locked out of a project would have nobody able to fix it except the
person locked out.

**Whoever creates a project is added to it,** along with anyone named as its project manager.
Otherwise a manager creating a job for their team would be the first person shut out of it.

List endpoints narrow rather than refuse: `GET /projects`, `GET /captures`, `GET /dashboard` and
`GET /sync` return only the caller's projects, and their totals narrow with them — a count that
included projects the rows omit would report how much other work exists. The `/api/events`
socket filters the same way, so a capture's processing status is only pushed to people who can
open it.

### Errors

Every failure uses one envelope:

```json
{
  "code": "not_found",
  "message": "Project abc123 not found",
  "details": { "optional": "context" }
}
```

| Status | `code` | Meaning |
| --- | --- | --- |
| 401 | `authentication_failed` | Missing, invalid or expired token |
| 403 | `permission_denied` | Authenticated but lacking the capability |
| 403 | `password_change_required` | An administrator reset this password; the account may reach `/auth/me` and `/auth/change-password` only |
| 404 | `not_found` | No such resource |
| 409 | `conflict` | Duplicate code, already processing, invalid state transition |
| 413 | `payload_too_large` | Upload exceeds `MAX_UPLOAD_BYTES` |
| 415 | `unsupported_media` | Extension or magic bytes not accepted |
| 422 | `validation_error` | Field-level detail in `details.fields` |
| 500 | `internal_error` | `details.correlation_id` ties the response to the log entry |

A 422 carries per-field messages a form can display directly:

```json
{
  "code": "validation_error",
  "message": "The request could not be validated",
  "details": {
    "fields": [
      { "field": "code", "message": "String should match pattern", "type": "string_pattern_mismatch" }
    ]
  }
}
```

### Pagination

Every list endpoint returns the same envelope and caps `limit` at 200 (§35):

```json
{ "items": [], "total": 0, "limit": 50, "offset": 0 }
```

Requesting `limit=5000` is a 422, not a silently truncated response.

"Every list endpoint" was a claim rather than a fact for four of them —
`/floors/{id}/paths`, `/locations/{id}/frames`, `/captures/{id}/files` and
`/projects/{id}/shares` — and they were the four that grow for as long as a deployment runs: one
path per visit (each carrying its whole `steps` array), one frame per visit to a place forever,
one row per uploaded file, and every share link ever created, because dead ones are kept on
purpose. `tests/integration/test_list_pagination.py` now reads the OpenAPI schema and fails on
any `GET` returning a bare array unless it is listed there with a reason it is bounded by
something other than time — a building's floors, a frame's annotations, a fixed catalogue.

A client that wants a whole short list still asks for `limit=200`; what it cannot do any more is
receive an unbounded one by accident.

### Project scoping

Every route that takes an id belonging to a project resolves it through a membership check, and
answers **404** when the caller is not a member — never 403, which would confirm the resource
exists to somebody who was never told about it.

Six routes did not. `/projects/{id}/timeline`, `/coverage`, `/frequency`, `/stale-areas` and
`/annotations`, plus `/floors/{id}/paths`, took the id, filtered a query on it, and never
resolved it: their signature was `_: RequireProjectRead`, which checks the *capability* and
discards the user, so there was no user left to check membership with and nothing for a type
checker to catch. Between them they answered what had been inspected, how often, what was
overdue, and every defect raised with its title — to any account in the deployment.

`tests/integration/test_project_scoping.py` now carries both a behavioural test (a non-member
asking for each of them) and a guard that walks the route table and fails on any route taking a
scoped id without calling a resolver. The resolver list in that guard is maintained by hand on
purpose: a new way to reach a project should be a decision somebody makes, and the test failing
is how it gets made.

### Signed assets

Binary objects are never exposed as filesystem paths. Any response referencing an image, video,
plan or report includes a signed, expiring URL:

```json
{
  "asset": {
    "key": "projects/<id>/floors/<id>/sessions/<id>/frames/<id>.jpg",
    "url": "http://localhost:8000/api/files/projects/...?expires=1757500000&signature=...",
    "expires_in": 3600
  }
}
```

`GET /api/files/{key}` is intentionally unauthenticated but HMAC-signed over `key:expires` — a
browser `<img>` tag and a WebGL texture loader cannot attach an Authorization header. A signature
for one object does not unlock another, and it expires. With `STORAGE_BACKEND=s3` this route is
unused and the provider issues native presigned URLs.

### Provenance

Anything positional or derived carries a `provenance` of `measured`, `estimated`, `ai_predicted`
or `manual`, and estimates carry a `confidence` (§46). Coverage responses additionally carry a
`method` string stating how the figure was produced.

### Correlation

Every response includes `X-Correlation-Id` and `X-Response-Time-Ms`. Send your own
`X-Correlation-Id` to trace a request through the logs.

---

## Endpoints

### Auth — `/api/auth`

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/auth/login` | Exchange credentials for a token pair. |
| POST | `/auth/refresh` | Rotate the refresh token; returns a new pair. |
| POST | `/auth/logout` | Revoke a refresh token. |
| GET | `/auth/me` | Current user plus their capability list. |
| POST | `/auth/change-password` | Change your own password; revokes all sessions. Must differ from the current one. |

### Users — `/api/users` *(admin)*

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/users` | Paginated user list. |
| POST | `/users` | Create a user with a role. |
| PATCH | `/users/{user_id}` | Change name, role or active state. |
| DELETE | `/users/{user_id}` | **Deactivate** (never delete — captures keep operator attribution). |
| POST | `/users/{user_id}/password` | Reset somebody else's password. The account is then held to replacing it. |

An admin cannot remove their own admin role, deactivate their own account, or reset their own password through `/users/{id}/password` — that last one would skip the current-password check `/auth/change-password` exists to make.

**There is no self-service password reset, and there cannot be one here.** Proving you own an address means sending something to it, and no feature of this platform may depend on a mail service to run locally. So recovery is an administrator action, and what keeps that honest is that the credential they set is explicitly temporary:

`POST /users/{id}/password` sets the password, revokes every session the account has open, and sets `must_change_password`. While that flag stands the account can reach `/auth/me` and `POST /auth/change-password` and **nothing else** — every other endpoint answers 403 with `code: "password_change_required"`. Branch on that code rather than on the status: the token is genuine, so a client that treats a 403 here as an expiry will refresh in a loop and never reach the screen it needs.

The flag is checked against the row on every request rather than written into the access token, so it applies to a session opened *before* the reset — which is the case that matters, since the person whose password is being reset because somebody else may be in the account is precisely the person already holding a token. Setting a password through `/auth/change-password` clears it, and the new password must differ from the temporary one, or the requirement could be satisfied by keeping the password the administrator chose.

The temporary password is never echoed in the response. The administrator typed it; repeating it would put it in every log between them and the browser for no gain.

### What the platform records

`GET /api/system/events` returns recent capture activity read from durable state, plus
`logged_event_names` — every domain event this build emits. That list is the registry in
`app/core/audit.py`, not a hand-maintained copy of it: it was a hard-coded list of eight names
while the code emitted thirty-five, which is worse than an omission, because it is a false
statement about the system served from the endpoint whose job is to describe it.
`tests/unit/test_audit_events.py` parses every `log_event()` call in `app/` and fails if the two
sets differ in either direction.

Destructive and privilege-changing actions are the ones the registry exists for, and none of them
were recorded at all until they were looked for: `auth.role_changed` (with both the old and new
role, since the old one cannot be reconstructed afterwards), `auth.password_reset`,
`auth.user_created`, `auth.user_activated`, `auth.user_deactivated`, `project.deleted`,
`capture.deleted` (with the frame and file counts, which nothing can recover once the row is
gone), `capture.file_deleted`, `floorplan.deleted`, `location.deleted` (with how many frames it
unplaced) and `annotation.deleted` (recording whether the deleter was the author).

### Projects, buildings, floors, zones

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/projects` | Create a project (§22 fields). |
| GET | `/projects` | List with roll-ups; `?q=` searches name, code and client. |
| GET | `/projects/{id}` | One project with roll-ups. |
| PATCH | `/projects/{id}` | Update. |
| DELETE | `/projects/{id}?confirm_code=CODE` | Destructive. Requires the project code repeated. |
| POST | `/projects/{id}/buildings` | Add a building. |
| GET | `/projects/{id}/buildings` | List buildings. |
| GET · PATCH · DELETE | `/buildings/{id}` | Read, update, delete a building. |
| POST | `/buildings/{id}/floors` | Add a floor (`level`, `area_sqft`, `elevation_m`). |
| GET | `/buildings/{id}/floors` | List a building's floors. |
| GET | `/projects/{id}/floors` | Every floor in the project, with roll-ups. |
| GET · PATCH · DELETE | `/floors/{id}` | Read, update, delete a floor. |
| POST | `/floors/{id}/zones` | Add a zone with `expected_capture_points`. |
| GET | `/floors/{id}/zones` | List zones. |
| PATCH · DELETE | `/zones/{id}` | Update or delete a zone. |
| PUT | `/projects/{id}/schedule` | Set capture cadence (project-wide or per floor). |
| GET | `/projects/{id}/schedule` | List configured schedules. |

`DELETE /projects/{id}` requires `confirm_code` to match the project code. That is not ceremony:
it destroys an entire site's capture history, and a mistyped id should not be enough.

### Floor plans and capture locations

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/floors/{id}/floorplan` | Upload PNG/JPG/PDF (multipart). Becomes the active plan. |
| GET | `/floors/{id}/floorplan` | The active plan. 404 when the floor has none. |
| GET | `/floorplans/{id}/sheets` | What is in the drawing set this plan came from. Nothing is rendered. |
| GET | `/floorplans/{id}/sheets/{page}/preview` | A small PNG of one sheet, for choosing by eye. |
| POST | `/floorplans/{id}/sheets/{page}` | Use a different sheet of the same set. Creates a revision. |
| GET | `/floors/{id}/floorplans` | Every revision, active first. |
| POST | `/floorplans/{id}/activate` | Make an earlier revision active again. |
| PUT | `/floorplans/{id}/calibration` | Two points + a real distance → pixels per metre. |
| DELETE | `/floorplans/{id}` | Refused while capture points reference it. |
| POST | `/floors/{id}/locations` | Create a capture point (`P01`, `P02`, …). |
| GET | `/floors/{id}/locations` | Points with visit counts and latest frame. |
| GET | `/floors/{id}/locations/nearest` | Points closest to `x`/`y`, with distances. `within_px` bounds it. |
| GET | `/floors/{id}/captures-near` | Time travel: nearby points and every dated visit, newest first. |
| PATCH | `/locations/{id}` | Move or relabel; rebuilds affected capture paths. |
| DELETE | `/locations/{id}` | Deletes the point; frames become unplaced, not deleted. |

Coordinates are floor-plan **pixels**. A point outside the raster is rejected rather than clamped —
silently clamping would hide a broken editor.

**PDF sheets are rasterised on upload.** That is the format drawings actually arrive in, and it
used to be the one format the product could not use: a PDF stored, reported zero dimensions, and
told the operator to go and export a PNG. Now the page is rendered to PNG at 150 DPI (PDFium via
`pypdfium2`), the render becomes the plan, and the original PDF is kept beside it.

Send `page` with the upload to pick a sheet (1-based, default 1). One sheet becomes one plan
deliberately: a 40-page set also contains sections, elevations and details, and importing them
all as floor plans would be worse than importing none.

The response carries a `render` object — `page`, `page_count`, `dpi`, page size in points, and
the source PDF's storage key. `page` is the load-bearing field: a plan's pixel coordinates belong
to one specific sheet, and a raster on its own cannot say which. It lives in its own column
rather than inside `calibration`, because `PUT /floorplans/{id}/calibration` replaces that value
wholesale.

**The render does not imply a scale.** A PDF knows its paper size and nothing about the building
— the drawing scale is printed on the sheet, not stored in the file — so `px_per_meter` stays
null until a human calibrates two points against a printed dimension. Deriving it from page
geometry would produce a confident, precise and wrong number.

### Captures

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/captures` | Create a session; resolves project/building from the floor. |
| GET | `/captures` | List; filter by `project_id`, `floor_id`, `status`, `since`. |
| GET | `/captures/{id}` | Detail with files, jobs and a quality summary. |
| PATCH | `/captures/{id}` | Update name, zone, notes, frame interval. |
| POST | `/captures/{id}/finish` | Close the recording window (does not start processing). |
| POST | `/captures/{id}/cancel` | Cancel; refused once `ready`. |
| DELETE | `/captures/{id}` | Delete the session and its stored objects. |
| POST | `/captures/{id}/upload` | Multipart upload of one or more files, validated as they stream. |
| POST | `/captures/{id}/upload-url` | Signed URL to PUT one file straight to storage (large videos). |
| POST | `/captures/{id}/files` | Register a directly-uploaded object, after the server inspects it. |
| POST | `/captures/{id}/multipart` | Start a chunked upload; returns a signed URL per part. |
| POST | `/captures/{id}/multipart/resume` | Which parts landed, plus fresh URLs for the rest. |
| POST | `/captures/{id}/multipart/complete` | Assemble the parts and register the object. |
| POST | `/captures/{id}/multipart/abort` | Discard an abandoned chunked upload. |
| GET | `/captures/{id}/files` | File metadata: EXIF, GPS, camera, dimensions, validation. |
| DELETE | `/captures/{id}/files/{file_id}` | Remove one uploaded file. |
| POST | `/captures/{id}/process` | Queue the pipeline. Returns immediately. |
| GET | `/captures/{id}/status` | Live per-stage status and overall progress. Also reclaims stages whose worker died. |
| POST | `/captures/{id}/retry` | Re-run after a failure. |
| GET | `/captures/{id}/frames` | Paginated frames; filter by `verdict` or `located`. |
| GET | `/captures/{id}/path` | The ordered capture path for this visit. |
| POST | `/captures/{id}/import` | Import from a registered camera device. |
| POST | `/captures/{id}/align` | Pin the estimated track to the plan from ≥2 manual anchors. Driven by the viewer's Plan tab. |
| POST | `/captures/{id}/localize` | Place the capture by recognising frames from earlier captures of the same floor. Runs automatically at the end of processing; this re-runs it. |
| DELETE | `/captures/{id}/placement` | Undo a placement: unplace every frame and remove the points that placement created. |
| GET | `/captures/{id}/segments` | Where this upload looks like it stopped being one walkthrough, with the evidence behind each break. |
| POST | `/captures/{id}/split` | Cut the upload into the walkthroughs an operator confirmed, each on the floor they say it was walked on. |

**A worker that dies does not strand the capture.** Reading `GET /captures/{id}/status` also
reclaims any stage left RUNNING by a worker that is gone: the stage becomes `failed` with
`error_code: worker_lost`, the capture becomes `failed` with a reason that says so, and
`POST /captures/{id}/retry` — which requires `failed` — starts working. Before this the capture
was unrecoverable through the API: `/retry` refused because the session was `processing`,
`/process` refused because a job was still active, and only a forced reprocess got out of it.

The queue decides, not a clock. A stage commits RUNNING and does not touch the row again until it
finishes, so elapsed time cannot tell a long video extraction from a dead worker; the backend is
asked instead and answers alive whenever it cannot be certain. A capture genuinely still being
processed is never failed by this, and neither is one whose job id was never recorded.

**One upload, several walkthroughs.** An operator who captures a building in a morning walks one
floor, takes the lift, and keeps going — and a capture session has exactly one `floor_id`, so
mapping estimates a single track straight through a lift ride. `GET /captures/{id}/segments`
reports where that happened. It reads what the MAPPING stage recorded rather than recomputing, so
`analysed` is `false` until the capture has been processed; that is an answer, not an error.

Each boundary carries its own evidence, and the two fields to read are `time_break` and
`scene_break`. A `scene_break` of `null` is the third answer and not a quiet `false`: the two
frames were too featureless for a failure to match to mean anything, so the imagery abstained.
`proposed` is what the platform would act on — a boundary can be real and unproposed, when
splitting there would leave fewer than four frames on one side, and `reason` says so.

`POST /captures/{id}/split` takes the plan, which need not be the proposal: the operator can move
a boundary or split where nothing was proposed, because they can see the building and the
detector cannot. What is enforced is that the segments tile the capture exactly — every frame in
one walkthrough, none in two — and that each carries a floor, **which the detector never supplies
and never could**. Nothing in the imagery says which floor a walk was on.

Two consequences worth knowing before calling it. The **first segment stays in this capture and
keeps its floor**: frame storage keys embed the floor the capture was created on, and the split
re-points database rows rather than copying hundreds of megabytes of imagery, so a row whose
floor moved would look for its own bytes in the wrong place. Passing a different `floor_id` for
segment 1 is refused rather than ignored. And the **estimated track is cleared from every
resulting walkthrough**, because a track that ran through a break was not measuring anything;
`reprocess` (default true) queues mapping again for each. Quality scores and ORB features
survive — those are facts about a frame, not about which capture it is filed under.

**Automatic placement.** `/localize` never fails for the ordinary refusals — a floor nobody has
captured before, or a walk nothing recognised, are answers rather than errors, and both leave the
capture untouched. Read `mode` first: `transform` placed the whole walk from a fitted similarity
transform, `recognised` placed only the frames actually matched and the rest still need
anchoring, `none` changed nothing. Branch on `reason` rather than on `note`, which is prose for a
person: `placed`, `no_references` (this floor has never been captured and placed — anchor one
walk by hand and every later one can place itself), `no_matches`, `already_placed`,
`not_processed`. They call for different responses and only `no_matches` means the recognition
actually looked at imagery and disagreed.

**Undoing a placement** is what makes running the automatic one unattended safe. `DELETE
/captures/{id}/placement` unplaces every frame *and deletes the points that placement created* —
leaving those behind would be worse than doing nothing, because an empty point in the wrong
corridor is still a perfectly good reference for the next capture's recognition, so a corrected
mistake would go on propagating. Points the capture merely joined are left alone: another capture
may be standing on them, and one an operator declared by hand is never machine residue. The
response says how many of each. Recognition can be re-run immediately afterwards.

Capture points carry `placement_generation`: 0 for a point a person placed or anchored, and one
more for each hop of automatic recognition between it and that person. Recognition refuses to
create points from references more than three hops out — it falls back to placing only the frames
it recognised, which adds no new coordinates — so a floor wants one hand-anchored capture
occasionally rather than never. `matches[].parallax_deg` is the evidence behind each placement
— near zero means the two cameras were in nearly the same spot — and `placement_method` plus
`localization_report` on the session keep the whole record. It will not overwrite frames that are
already placed unless called with `?replace_existing=true`.

**Which upload route.** `/upload` streams the body through the API and validates it *before*
anything is stored, and de-duplicates identical bytes by checksum — the right default, and what
the mobile app uses. `/upload-url` hands the client a signed PUT so a multi-gigabyte video does
not occupy an API worker for the length of the transfer; the server then checks size and type on
the stored object at `/files` and **deletes anything that fails**. The direct route has no
content checksum (computing one would mean reading the object back), so retries are idempotent by
key and `checksum_sha256` is null rather than unverified.

`/multipart` is the direct route cut into pieces, and it exists for one failure: a walkthrough
video that drops at ninety percent. A single PUT is one unit of retry, so that drop costs the
whole transfer, over the connection that has just proved it cannot hold. Here each part is its
own PUT (8 MiB by default, larger when the file would otherwise need more than S3's 10,000
parts), and a client that comes back asks `/multipart/resume` which parts survived.

Two details are load-bearing. **The server enumerates the parts** — `list_parts` on S3, a
directory listing locally — rather than trusting a client's list of ETags, which is what makes a
resume possible for a client that crashed and remembers nothing. And **a gap is refused**: S3
completes an upload out of whatever parts it is given, so parts 1, 2 and 4 would assemble into a
real object that is shorter than the original and passes every check downstream. `size_bytes` is
therefore required at both ends — it is where the part boundaries come from, and a client that
declares a different size slices the file somewhere the server will not reassemble it.

Both clients route by size: proxied up to 32 MiB, one direct PUT up to 64 MiB, chunked above
that (`frontend/src/lib/uploadStrategy.ts`, `mobile/src/lib/uploadPlan.ts`). The offline queues
on both sides persist the upload id alongside the file, so an interrupted upload continues on
the next drain instead of starting over.

`POST /captures` accepts `client_session_id`, making creation **idempotent** — an offline mobile
client keeps the id it generated, so a retried request returns the existing session rather than a
duplicate (§32). Identical uploaded bytes are de-duplicated by SHA-256 for the same reason.

`POST /captures/{id}/process` body (all optional):

```json
{ "frame_interval_seconds": 2.0, "force": false, "stages": ["quality", "features"] }
```

Stages always run in canonical order regardless of the order listed.

### Frames, viewer and comparison

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/frames/{id}` | Frame detail with signed thumbnail, preview and full URLs. |
| GET | `/frames/{id}/viewer` | Everything the 360 viewer needs in one request. |
| PUT | `/frames/{id}/location` | Place on the floor plan, by `location_id` or by `x`/`y`. |
| DELETE | `/frames/{id}/location` | Unplace; rebuilds the capture path. |
| GET | `/locations/{id}/frames` | Capture history at one point, newest first. |
| GET | `/compare/frames?frame_a=&frame_b=` | Pair two specific frames. |
| GET | `/compare/locations/{id}?date_a=&date_b=` | Pair the visits nearest two dates. |
| GET | `/floors/{id}/paths` | Every capture path on a floor — one polyline per visit. |

`/compare/locations/{id}` picks the visits *nearest* the requested dates rather than requiring an
exact match: nobody captures a site on the exact calendar day a report asks about. The response
names which frames it used, and warns when two frames are not bound to the same location.

The viewer manifest bundles the frame, its neighbours (sorted by estimated distance, so the frame
across the corridor is one click away), previous/next in sequence, and the location's history.

### Annotations

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/frames/{id}/annotations` | Pin a finding at a yaw/pitch inside the panorama. |
| GET | `/frames/{id}/annotations` | Annotations on one frame. |
| GET | `/projects/{id}/annotations` | Project log; filter by `status`, `priority`, `floor_id`. |
| GET · PATCH · DELETE | `/annotations/{id}` | Read, update, delete. |

Positions are stored in **spherical degrees**, not pixels, so an annotation stays put if the frame
is later re-encoded at another size. Resolving someone else's finding requires
`annotation:resolve` (Project Manager or above) — and so does resolving your own, because that is
a management decision.

### Monitoring

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/dashboard` | Cross-project portfolio with overdue count. |
| GET | `/projects/{id}/dashboard` | The §21 dashboard in one request. |
| GET | `/projects/{id}/coverage` | Coverage by floor and zone, with `provenance` and `method`. |
| GET | `/projects/{id}/timeline` | Activity by date; `?floor_id=`, `?limit=`. |
| GET | `/projects/{id}/frequency` | Last capture, days since, next expected, due/overdue. |
| GET | `/projects/{id}/stale-areas` | Points never captured, or not within `threshold_days`. |

Coverage always states its own method:

```json
{
  "coverage_ratio": 0.25,
  "provenance": "manual",
  "method": "Visited capture locations ÷ administrator-declared expected capture points. Square footage is apportioned from that ratio — it is not measured from imagery."
}
```

The project dashboard returns `is_empty` when nothing has been captured, so the UI shows an empty
state rather than a wall of confident zeroes. `never_captured` is a distinct frequency status from
`ok` — an absent number is absent, not zero.

### Reports

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/report-types` | Available report types and formats. |
| POST | `/projects/{id}/reports` | Generate; returns when ready. |
| GET | `/projects/{id}/reports` | Paginated list. |
| GET | `/reports/{id}` | Detail including the computed payload. |
| GET | `/reports/{id}/download` | The rendered file with a filename. |
| DELETE | `/reports/{id}` | Delete the report and its object. |

Types: `project_summary`, `floor_coverage`, `capture_history`, `annotation_log`.
Formats: `pdf`, `csv`, `json`.

The computed payload is persisted, so the same figures can be re-rendered in another format
without recomputing — two formats of one report cannot disagree. Every report carries a
provenance section, and the CSV export preserves it as leading `#` comment lines.

**The positions note is counted, not boilerplate.** A report is the artefact that leaves the
building, and it reaches people who will never see the interface explaining what "estimated"
means here — so `provenance.positions` describes *these* captures: how many were anchored by
hand, how many recognised themselves against earlier captures of the same floor, how many were
placed frame by frame, how many are not placed at all, and — when recognition was involved — how
far the furthest point used sits from one a person placed. `capture_history` additionally names
each capture's own route in a `placement` column, because the summary at the foot of a report is
no help when reading one line of a table.

Reports generate synchronously: they are database aggregations plus a layout pass — hundreds of
milliseconds, not minutes — so queueing them would add a polling round trip for no benefit.

### Share links

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/frames/{id}/share` | Create a read-only link to this view. **The only response carrying the token.** |
| GET | `/frames/{id}/shares` | Links to this frame, live or not. |
| GET | `/projects/{id}/shares` | Every link into this project — the audit page. |
| DELETE | `/shares/{id}` | Revoke now, whatever the expiry says. |
| GET | `/shared/{token}` | Open a shared view. **No authentication.** |

A share link exists so somebody without an account can see one capture — the client signing off a
slab, the consultant asked whether a detail was built as drawn. The alternative people actually
use is emailing a screenshot, which throws away the date, the floor, the location and the ability
to look around.

It is a **bearer credential**: whoever holds the link has the access. The design follows from
admitting that rather than treating a long URL as a permission system.

| | |
| --- | --- |
| Token | 256 bits from `secrets`, stored only as a SHA-256 hash |
| Shown | Once, at creation. There is no way to retrieve it again |
| Expiry | Mandatory; 7 days by default, 90 maximum |
| Revocation | Immediate, independent of expiry, and by any project member |
| Scope | One frame. No siblings, no other dates, no annotations, no usable ids |

**Every refusal is the same refusal.** A wrong token, an expired one, a revoked one and a deleted
frame all return the identical 404 body. Telling a visitor a link *has expired* confirms it was
once real — the one fact useful to somebody probing, and no help to the person it was sent to,
who must ask for a new link either way. A test asserts the three bodies are byte-identical.

**Two limitations, stated rather than designed around.** A token in a URL reaches everywhere the
URL reaches: the application redacts it from its own error logs, but a reverse proxy's access log
records full paths, so a deployment that ships those should treat them as containing credentials.
And a link cannot be password-protected — the token is the only factor.

**Choosing a sheet.** A set arrives as one PDF holding forty drawings, of which one or two are
the plans anybody wants to place capture points on. Upload has always taken a page number and the
operator has always had to *know* it; these three endpoints let them look instead. Listing is
cheap — paper sizes come off the page objects with nothing rasterised — and previews are rendered
on demand at low DPI, so a set with hundreds of sheets opens immediately.

Choosing a sheet creates a **new revision** rather than re-rendering in place, and that is the
part worth understanding. A capture point is stored in the pixels of the plan it was placed on;
another sheet is a different drawing at a different scale, so overwriting the raster would move
every placed point to a coordinate that means nothing, with nothing in the data recording it.
Revisions already exist for this (a re-upload deactivates the previous plan and keeps it) and
picking a sheet is the same event by another route.

**A sheet already rendered for this floor is re-activated, not rendered again.** That is not an
optimisation. A capture point references the revision it was placed on, so an operator who
switches to the section and back has to arrive at the *same* revision — a second revision of
sheet 1 renders the identical drawing and is still, to every point on the first, a different
plan, which would orphan them permanently. Re-rendering each time also grew the revision list
and the stored rasters without bound: toggling between two sheets five times left six revisions
describing two drawings. Because the revision is reused rather than recreated, a calibration
already measured on it survives the round trip.

The response carries `points_from_earlier_sheets`: points placed on a revision that is not the
one now active. They are not moved — that is the whole reason this is a revision — and the
floor-plan editor no longer draws them, because their coordinates describe a different drawing.
It reports how many it withheld and says they were placed on an earlier revision; switching back
to that revision shows them in place. The
calibration is deliberately *not* carried across: a pixel-to-metre scale measured on one drawing
is not the scale of another, and a plan that looks calibrated and reports wrong distances is
worse than one that admits it has none.

### Cameras

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/cameras/providers` | What the platform can talk to, and what it cannot. |
| POST | `/cameras` | Register a device *(admin)*. |
| GET | `/cameras` | Registered devices. |
| PATCH · DELETE | `/cameras/{id}` | Update or deactivate. |
| GET | `/cameras/{id}/health` | Whether the device is reachable. |
| GET | `/cameras/{id}/discover` | List files available for import. |
| POST | `/captures/{id}/import?device_id=` | Import into a capture session. |

`/cameras/providers` reports `implemented: false` for the OSC and Insta360 adapters, because they
are unverified against physical hardware. Imports go through the same validation, magic-byte
sniffing, size limits and de-duplication as a browser upload.

### Offline sync — `/api/sync`

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/sync?since=` | The project tree as a delta, for clients that must work offline. |

Returns projects, buildings, floors, zones and **active** floor plans. Omit `since` for a full
sync; otherwise pass back the `cursor` from the previous response.

It exists because the mobile app is offline-first in one direction only. A capture recorded
without signal is queued and uploaded later — but *starting* one needs three live requests
(`/projects`, `/projects/{id}/floors`, `/floors/{id}/zones`), so an operator standing on an
unconnected floor cannot pick the floor they are standing on, and the capture they came to record
never begins. No amount of upload queueing helps, because nothing is queued yet.

Each collection comes back in two halves:

```json
{
  "cursor": "2026-09-16T08:14:22Z",
  "full": false,
  "floors": {
    "changed":  [ { "id": "…", "building_id": "…", "name": "Level 01", "level": 1 } ],
    "manifest": [ { "id": "…", "updated_at": "2026-09-14T11:02:00Z" } ]
  }
}
```

`changed` holds full records — **apply them as upserts**, since the overlap below re-sends rows.
`manifest` describes the *complete* set, always, even when nothing changed.

**Why a manifest rather than a plain change feed.** A delta built on `updated_at` can report a
creation or an update and can never report a deletion: the row is gone, so nothing is left to
carry a timestamp. A client that only applies `changed` keeps a floor for ever and offers it to
an operator whose captures the server will then refuse. The usual fix is a tombstone table
written by every delete path; the manifest gets the same result with no migration and no new
obligation on code that deletes things — anything held locally and absent from the manifest was
deleted, and anything whose manifest timestamp is newer than the local copy was missed, so the
client knows to resynchronise in full. This is affordable only because reference data is small
(tens of floors, not thousands); the endpoint refuses above 20,000 rows rather than quietly
returning a multi-megabyte manifest. **Captures and frames are deliberately not synced this
way** — they are output rather than reference data, and `/captures?since=` already pages them.

**The cursor is a few seconds behind the present, on purpose.** A row's `updated_at` can be
earlier than the moment it became visible: SQLite stores `CURRENT_TIMESTAMP` to the second, and
PostgreSQL's `now()` is the transaction start rather than the commit. A cursor taken at the
present would step over anything written in the boundary tick, permanently. The overlap re-sends
a few rows instead. A `since` in the future is refused with 422 rather than answered with an
empty delta, because a client whose clock runs fast would otherwise never receive another change
while every request looked successful.

Floor plan **images** are not inlined — only metadata and a signed URL. A client that wants them
offline fetches them while it still has a connection.

### Events — `/api/events` (WebSocket)

A WebSocket that pushes capture-processing changes, so a client does not have to ask every
twenty seconds. The handshake is fixed:

1. the socket opens;
2. the client sends `{"type": "auth", "token": "<access token>"}`;
3. the server replies `{"type": "ready"}`, and events follow.

```json
{ "type": "capture.status", "session_id": "CAP-…", "status": "analyzing",
  "stage": "quality", "progress": 1.0, "at": "2026-09-16T08:14:22Z" }
```

**The token is a message, not a query parameter.** A browser cannot set headers on
`new WebSocket(url)`, and `?token=…` writes a live bearer credential into every access log,
every proxy log and the browser's own history. Nothing is subscribed until the token arrives; a
client that never sends one is closed with **1008** after ten seconds — a policy close, distinct
from a network failure, so the client can tell "stop retrying" from "try again".

**An event is a hint, never data.** It says a capture moved; the client refetches through the
normal authorised endpoints. That is what keeps one authorisation check at connect sufficient —
no event carries anything `GET /captures/{id}` would refuse — and it is what makes a dropped
message harmless.

**Push never replaces polling.** The database stays authoritative and clients keep a poll as a
floor, because a dropped socket, a missed frame or a sleeping tab all have to be survivable.
Measured against the running stack: a status change reached the UI in **124 ms**, where the poll
interval is 20 s.

Deployment: the bus follows `QUEUE_BACKEND` — Redis pub/sub when the queue is Redis (the worker
is a separate process and cannot reach the API's sockets), in-process otherwise. Behind a reverse
proxy the socket needs its own location with `Upgrade`/`Connection: upgrade` and a long
`proxy_read_timeout`; `infrastructure/nginx/nginx.conf` has it, and the general `/api/` block
cannot serve it because it sets `Connection ""`.

### Admin and system

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/jobs` | The §45 processing dashboard; filter by `status`, `type`, `session_id`. |
| GET | `/jobs/summary` | Counts by status and stage, mean durations, recent failures, queue health. |
| POST | `/jobs/{id}/retry` | Re-run a failed stage **and everything after it**. |
| GET | `/system/capabilities` | What is implemented, estimated, or unavailable (§46). |
| GET | `/system/events` | Recent capture lifecycle activity. |
| GET | `/system/health` | Database, queue and storage, each actually probed. |

**Two health endpoints, and the difference matters.** `GET /health` is the container's liveness
probe: no database access, no storage access, so a restart is never triggered by a dependency
being briefly unavailable. `GET /api/system/health` is the one an operator or a dashboard reads —
it opens a database connection, asks the queue for its state, and probes the object store.

`status` is `degraded` rather than `ok` when the database *or* storage cannot be reached, and the
per-component objects say which. That was not always true of storage: it used to report the
*name* of the configured backend, which is "local" or "s3" whether or not the bucket exists, so
an unreachable object store left the service answering `ok` while every image request failed —
the monitoring suppressing the alarm rather than raising it.

The local backend probes by writing a small file and deleting it, because a read still succeeds
on a volume that has gone read-only or filled up. S3 uses `head_bucket`, which answers in one
round trip whether the endpoint is reachable *and* the credentials still have access — a 403
after a key rotation stops imagery just as completely as a timeout.
| GET | `/health` | Liveness probe — no database access, no auth. |
| GET | `/files/{key}` | Signed asset delivery (local storage backend). |

`GET /system/capabilities` is the machine-readable §46 contract. The web app reads it rather than
hard-coding assumptions, so a capability removed from the server cannot keep appearing to work:

```json
{
  "vision": {
    "relative_positioning": {
      "implemented": true,
      "method": "two-view spherical essential matrix, dead-reckoned",
      "provenance": "estimated",
      "limitations": [
        "no metric scale — direction only",
        "assumes planar motion on one floor",
        "drifts over long sequences",
        "not SLAM; no loop closure or global optimisation"
      ]
    },
    "progress_detection": { "implemented": false, "phase": 3 }
  },
  "independence": {
    "third_party_monitoring_platforms": [],
    "statement": "This platform uses no OpenSpace (or other proprietary construction-monitoring vendor) API, SDK, application, account or code…"
  }
}
```

`POST /jobs/{id}/retry` re-enqueues from the failed stage **onward**, not just that stage: re-running
one stage in isolation would leave downstream stages holding stale output.

---

## A complete worked flow

The §52 Definition of Done, as HTTP calls. This is exactly what
`backend/tests/integration/test_definition_of_done.py` performs.

```bash
API=http://localhost:8000/api
TOKEN=$(curl -s $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@demo.example.com","password":"SiteScope360!demo"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
AUTH="Authorization: Bearer $TOKEN"

# 1. Project → building → floor → zone
PROJECT=$(curl -s $API/projects -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Chevron Phase 2","code":"CVX-P2","expected_captures_per_week":2}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

BUILDING=$(curl -s $API/projects/$PROJECT/buildings -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Main Office"}' | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

FLOOR=$(curl -s $API/buildings/$BUILDING/floors -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Level 03","level":3,"area_sqft":10000}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl -s $API/floors/$FLOOR/zones -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"North Wing","area_sqft":4000,"expected_capture_points":4}' > /dev/null

# 2. Floor plan
curl -s $API/floors/$FLOOR/floorplan -H "$AUTH" -F 'file=@plan.png' -F 'name=Level 03 GA'

# 3. Capture session → upload → process
SESSION=$(curl -s $API/captures -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"floor_id\":\"$FLOOR\",\"name\":\"Morning walkthrough\"}" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl -s $API/captures/$SESSION/upload -H "$AUTH" \
  -F 'files=@PANO_000.jpg' -F 'files=@PANO_001.jpg'

curl -s -X POST $API/captures/$SESSION/process -H "$AUTH"

# 4. Poll until ready
until curl -s $API/captures/$SESSION/status -H "$AUTH" | grep -q '"status": *"ready"'; do sleep 1; done

# 5. View, place, compare, report
FRAME=$(curl -s "$API/captures/$SESSION/frames" -H "$AUTH" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["id"])')

curl -s $API/frames/$FRAME/viewer -H "$AUTH"
curl -s -X PUT $API/frames/$FRAME/location -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"x":200,"y":300}'

curl -s $API/captures/$SESSION/path      -H "$AUTH"
curl -s $API/projects/$PROJECT/coverage  -H "$AUTH"
curl -s $API/projects/$PROJECT/frequency -H "$AUTH"

curl -s $API/frames/$FRAME/annotations -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"title":"Missing ceiling panel","yaw_deg":42,"pitch_deg":12,"priority":"high"}'

REPORT=$(curl -s $API/projects/$PROJECT/reports -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"report_type":"project_summary","report_format":"pdf"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl -s $API/reports/$REPORT/download -H "$AUTH" -o project-summary.pdf
```

---

## Capture status values (§34)

```
draft → recording → uploading → uploaded → processing
      → extracting_frames → analyzing → mapping → ready
                                                 ↘ failed  (+ failure_reason, retry available)
                                                 ↘ cancelled
```

Pipeline stages, in order: `validate`, `metadata`, `frame_extraction`, `quality`, `features`,
`mapping`, `viewer_data`. Each is an independent, idempotent, individually retryable
`ProcessingJob` row — which is why the admin dashboard is a plain query rather than queue
introspection, and why it survives a worker restart.
