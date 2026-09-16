# API reference

99 endpoints across 71 paths. Base prefix `/api`.

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

Only `GET /health`, `GET /api/system/health`, `POST /api/auth/login`, `POST /api/auth/refresh` and
`GET /api/files/{key}` are reachable without a token. Authentication is checked **before** body
validation, so an unauthenticated caller learns nothing about whether their payload was
acceptable.

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
| POST | `/auth/change-password` | Change password; revokes all sessions. |

### Users — `/api/users` *(admin)*

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/users` | Paginated user list. |
| POST | `/users` | Create a user with a role. |
| PATCH | `/users/{user_id}` | Change name, role or active state. |
| DELETE | `/users/{user_id}` | **Deactivate** (never delete — captures keep operator attribution). |

An admin cannot remove their own admin role or deactivate their own account.

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
silently clamping would hide a broken editor. A PDF is stored but reports zero dimensions and
explains that it cannot be used for pixel placement until rasterised.

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
| GET | `/captures/{id}/status` | Live per-stage status and overall progress. |
| POST | `/captures/{id}/retry` | Re-run after a failure. |
| GET | `/captures/{id}/frames` | Paginated frames; filter by `verdict` or `located`. |
| GET | `/captures/{id}/path` | The ordered capture path for this visit. |
| POST | `/captures/{id}/import` | Import from a registered camera device. |
| POST | `/captures/{id}/align` | Pin the estimated track to the plan from ≥2 manual anchors. Driven by the viewer's Plan tab. |

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

Reports generate synchronously: they are database aggregations plus a layout pass — hundreds of
milliseconds, not minutes — so queueing them would add a polling round trip for no benefit.

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

### Admin and system

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/jobs` | The §45 processing dashboard; filter by `status`, `type`, `session_id`. |
| GET | `/jobs/summary` | Counts by status and stage, mean durations, recent failures, queue health. |
| POST | `/jobs/{id}/retry` | Re-run a failed stage **and everything after it**. |
| GET | `/system/capabilities` | What is implemented, estimated, or unavailable (§46). |
| GET | `/system/events` | Recent capture lifecycle activity. |
| GET | `/system/health` | Database, queue and storage status. |
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
