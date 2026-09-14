# SiteScope 360 — Architecture

SiteScope 360 is an independent construction-site 360° capture and monitoring platform.

> **Independence statement.** This platform does not use, call, wrap, scrape, or embed OpenSpace
> (or any other proprietary construction-monitoring vendor's) API, SDK, application, account,
> private endpoints, or code. Every subsystem listed below is implemented in this repository on top
> of permissively licensed open-source components. See
> [OPEN_SOURCE_LICENSES.md](OPEN_SOURCE_LICENSES.md) for the full dependency inventory.

---

## 1. System overview

```
                      ┌──────────────────────────────────────────────┐
                      │  Clients                                     │
                      │                                              │
   Desktop browser ──▶│  frontend/   Vite + React + TS               │
                      │    dashboard · projects · floor plans ·      │
                      │    360 viewer · timeline · compare ·         │
                      │    reports · settings · capture (PWA)        │
                      │                                              │
   Phone / tablet  ──▶│  mobile/     Expo (React Native)             │
                      │    offline-first capture + upload queue      │
                      └──────────────────┬───────────────────────────┘
                                         │ HTTPS / JSON  (JWT bearer)
                                         ▼
                      ┌──────────────────────────────────────────────┐
                      │  backend/  FastAPI (ASGI)                    │
                      │                                              │
                      │  api/      REST routes, OpenAPI docs         │
                      │  auth/     JWT, password hashing, RBAC       │
                      │  schemas/  Pydantic request/response models  │
                      │  services/ domain logic (coverage, timeline, │
                      │            frequency, capture sessions)      │
                      │  storage/  StorageProvider abstraction       │
                      │  cameras/  CameraProvider abstraction        │
                      │  processing/ pipeline stage definitions      │
                      │  vision/   VisionEngine (quality, features,  │
                      │            motion, position, keyframes, map) │
                      │  reports/  CSV / PDF generation              │
                      │  bim/      future BIM interfaces (stubs)     │
                      └────┬──────────────────────┬──────────────────┘
                           │                      │
              enqueue jobs │                      │ metadata / queries
                           ▼                      ▼
        ┌──────────────────────────┐   ┌────────────────────────────┐
        │  JobQueue abstraction    │   │  PostgreSQL                │
        │   ├── RedisQueue (RQ)    │   │   (SQLite for local dev)   │
        │   └── InlineQueue (dev)  │   │   Alembic migrations       │
        └───────────┬──────────────┘   └────────────────────────────┘
                    │
                    ▼
        ┌──────────────────────────────────────────┐
        │  worker  (backend.app.workers)           │
        │  runs pipeline stages out-of-process     │
        └───────────┬──────────────────────────────┘
                    │ read / write binaries
                    ▼
        ┌──────────────────────────────────────────┐
        │  StorageProvider                         │
        │   ├── LocalStorage   (filesystem, MVP)   │
        │   └── S3Storage      (S3-compatible)     │
        └──────────────────────────────────────────┘
```

Large binaries (panoramas, videos, floor plans, thumbnails) **never** go into PostgreSQL. The
database stores metadata plus a storage *key*; the storage provider resolves a key to bytes or to a
short-lived signed URL.

---

## 2. Repository layout

The brief (§48) proposed top-level `worker/`, `vision/` and `shared/` directories. We deviate,
deliberately, in two places:

| Brief | Here | Reason |
| --- | --- | --- |
| `worker/` top-level | `backend/app/workers/` | The worker executes the *same* domain models, storage providers and DB session factory as the API. A separate top-level package would need either a copy of those modules or a third shared package plus two installs. One installable Python package (`backend/`) with two entrypoints (`uvicorn app.main:app`, `python -m app.workers.run`) is simpler, keeps types consistent, and builds one Docker image instead of two. |
| `vision/` top-level | `backend/app/vision/` | The vision engine is consumed only by pipeline stages inside the same process. Keeping it in-package avoids a cross-package import boundary while still being a self-contained module with its own interfaces — it can be extracted into its own distribution later without touching call sites, because all access goes through `VisionEngine`. |
| `shared/` top-level | `shared/` (kept) | Holds the language-neutral contract: canonical enum values as JSON, generated into TypeScript. Prevents status strings drifting between Python and TS. |

Final layout:

```
.
├── backend/                 FastAPI app, worker, vision, storage, migrations, tests
│   ├── app/
│   │   ├── api/routes/      one module per resource group
│   │   ├── auth/            JWT issue/verify, password hashing, RBAC dependencies
│   │   ├── bim/             future BIM import/compare interfaces (no implementation)
│   │   ├── cameras/         CameraProvider registry + adapters
│   │   ├── core/            settings, logging, ids, errors
│   │   ├── database/        engine, session, Base
│   │   ├── models/          SQLAlchemy ORM models
│   │   ├── processing/      pipeline definition + stage implementations
│   │   ├── queue/           JobQueue abstraction (inline + RQ backends)
│   │   ├── reports/         report builders, CSV + PDF renderers
│   │   ├── schemas/         Pydantic v2 models
│   │   ├── services/        domain services used by routes and workers
│   │   ├── storage/         StorageProvider abstraction + local/S3 adapters
│   │   ├── vision/          VisionEngine and its replaceable components
│   │   └── workers/         worker entrypoint + job handlers
│   ├── alembic/             migrations
│   └── tests/               unit · vision · integration
├── frontend/                Vite + React + TypeScript + Tailwind web app
├── mobile/                  Expo capture app (offline-first)
├── shared/                  cross-language enum/contract definitions
├── infrastructure/          nginx config, helper scripts
├── sample-data/             generated demo floor plans + panoramas (public domain, generated here)
├── docs/                    this file and the rest of the documentation set
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## 3. Domain model

```
User ──┬── owns ──▶ Project ──▶ Building ──▶ Floor ──┬──▶ Zone
       │                                             │
       │                                             ├──▶ FloorPlan (image, scale, calibration)
       │                                             │
       │                                             └──▶ CaptureLocation (x, y on floor plan)
       │                                                        ▲
       └── operates ──▶ CaptureSession ──▶ CaptureFile          │ bound to
                              │                 │              │
                              │                 └──▶ CaptureFrame ─┘
                              │                            │
                              │                            └──▶ QualityResult
                              ├──▶ ProcessingJob (one row per pipeline stage attempt)
                              ├──▶ CapturePath (ordered sequence of locations)
                              └──▶ Annotation (pinned to a frame at yaw/pitch)
```

Cardinality and lifecycle notes:

- A **CaptureSession** is the unit of field work: one operator, one floor, one visit. Its ID is
  human-legible and sortable: `CAP-20260910-103022-A8F3`.
- A **CaptureFile** is one uploaded artefact (a panorama or a 360 video).
- A **CaptureFrame** is one viewable panorama. A still image yields exactly one frame; a video
  yields N frames at the configured extraction interval. Frames are the atom the viewer, the
  timeline, the comparison view and annotations all address.
- A **CaptureLocation** is a *place on a floor plan* (`P01`, `P02`, …), not a photograph. It is
  stable across visits, which is precisely what makes date-over-date comparison possible: to
  compare, we look up the frame taken at the same location on two different sessions.
- **CapturePath** stores the ordered walk through locations for one session, so the same floor can
  have a different route each visit without mutating the locations themselves.

Provenance is first-class. Anything positional or analytical carries a `source` of
`measured | estimated | ai_predicted | manual`, and estimates carry a `confidence`. The UI renders
this distinction rather than presenting every number as fact (§46).

---

## 4. Capture processing pipeline

Stages are independent, idempotent, and individually retryable. Each stage run is a
`ProcessingJob` row, so the admin job dashboard is a straight query rather than queue introspection.

```
   POST /api/captures/{id}/upload         (multipart, streamed → StorageProvider)
                │
                ▼
        ┌──────────────────┐
        │ 1 VALIDATE       │  magic-byte sniff, extension allow-list, size cap,
        │                  │  dimension sanity, equirectangular heuristic
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ 2 METADATA       │  EXIF, GPS, camera make/model, capture timestamp,
        │                  │  XMP GPano block, video duration/fps/codec
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ 3 FRAMES         │  stills → 1 frame; video → frame every N seconds
        │                  │  (configurable per session), plus thumbnails
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ 4 QUALITY        │  blur (variance of Laplacian), brightness, contrast,
        │                  │  resolution, seam completeness, near-duplicate hash
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ 5 FEATURES       │  ORB keypoints + descriptors per frame, persisted
        │                  │  as a compact blob for later matching
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ 6 MAPPING        │  pairwise match → relative motion → dead-reckoned
        │                  │  track → pose-graph SLAM re-optimises it against
        │                  │  detected loop closures (`vision/slam.py`) →
        │                  │  confidence. Low confidence ⇒ "manual placement
        │                  │  required", never a fake path.
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ 7 VIEWER DATA    │  per-frame viewer manifest: texture keys, tile hints,
        │                  │  neighbours, initial yaw, location binding
        └────────┬─────────┘
                 ▼
              READY
```

### Deployment shape

The same pipeline at the coarser grain a deployment diagram uses, naming the module behind each
box. Two of them have more than one answer, covered underneath:

```
[Web app / Mobile] ──(upload)──> [Object storage]
  frontend/, mobile/              storage/{local,s3}.py
  proxied:  POST /captures/{id}/upload      (validated before the bytes land)
  direct:   POST /captures/{id}/upload-url  → client PUT → POST /captures/{id}/files
                                        │
                                POST /captures/{id}/process
                                   services/captures.py
                                        │
                                        ▼
                                 [Python worker]
                          workers/run.py — RQ on Redis,
                          ThreadQueue fallback for laptops
                                        │
               ┌────────────────────────┴────────────────────────┐
               ▼                                                 ▼
      [Frame extraction]                              [VIO / SLAM processing]
   processing/media.py                             vision/motion.py  → relative
   cv2.VideoCapture, FFmpeg-backed —               vision/slam.py    → pose graph,
   asserted by test_media.py, because a            loop closure, LM + Huber
   build without it yields zero frames
   rather than an error
               │                                                 │
               └────────────────────────┬────────────────────────┘
                                        ▼
                        [Floor plan alignment algorithm]
                            vision/mapbuilder.py — similarity
                            transform (rotation + uniform scale +
                            translation) fitted to ≥2 operator
                            anchors; refuses collinear anchors
                                        │
                                        ▼
                          [Database insertion & storage]
                       PostgreSQL rows + object storage, served as
                       expiring signed URLs, optionally through
                       CDN_BASE_URL. Capture locations carry a
                       PostGIS point (SRID 0, generated from x/y)
                       with a GiST index; services/spatial.py falls
                       back to exact squared distance off PostgreSQL
```

#### Uploads: two routes, and why both exist

`POST /captures/{id}/upload` streams the body through the API to the provider while hashing it,
enforcing the extension allow-list, the magic-byte sniff and the size cap *before* the bytes
land. A rejected upload never becomes an object, and identical bytes are de-duplicated by
checksum. It holds an API worker for the length of the transfer, which is right for a panorama
and wasteful for a multi-gigabyte video.

`POST /captures/{id}/upload-url` → client PUTs to storage → `POST /captures/{id}/files` is the
direct route. The key is minted by the server (a client-named key is a client-chosen write
target), and registration re-establishes by hand what the proxied route got for free: size from
a HEAD, type from a ranged read of the first 64 bytes, and **deletion of anything that fails**,
so a rejected upload does not stay billable or reachable.

One guarantee genuinely does not survive the move: there is no content checksum, because
computing one means reading the whole object back through the API, which is the cost the caller
chose this route to avoid. Retries are idempotent *by key* instead, and `checksum_sha256` is
left null rather than filled with something unverified.

With `local` storage the PUT target is `PUT /api/files/{key}`, signed the same way reads are —
except the HMAC covers the verb. Without that domain separation every asset URL ever handed to a
browser would also authorise overwriting the object it points at.

The web app chooses per file, in `frontend/src/lib/uploadStrategy.ts`: over 32 MiB goes direct,
at or under it stays proxied. The threshold sits above any panorama this app produces and below
any walkthrough video, so the common upload keeps content de-duplication and only the transfers
that would tie up a worker for minutes give it up. The offline retry queue routes by the same
rule — when it did not, a failed large upload was re-queued and then retried *through* the API,
which is the one transfer the route exists to keep off it.

**Deploying the direct route on S3 needs a bucket CORS policy.** The PUT is issued by the
browser, cross-origin, with a `Content-Type` header, so it is preflighted. In development
FastAPI's own CORS middleware answers that; a bucket answers nothing by default, and the upload
fails in production having worked locally:

```json
[{ "AllowedMethods": ["PUT"],
   "AllowedOrigins": ["https://app.example.com"],
   "AllowedHeaders": ["Content-Type"],
   "MaxAgeSeconds": 3000 }]
```

#### Trajectory and alignment: what the estimate does and does not know

The MAPPING stage recovers where the operator walked *relative to where they started* — frame to
frame ORB matching, an essential-matrix motion estimate, then a pose graph that re-optimises the
dead-reckoned track against detected loop closures.

What comes out has a shape and nothing else. It is **not measured in metres**, and no amount of
better estimation will make it so: a single moving camera recovers translation only up to an
unknown scale factor, which is why monocular SLAM is scale-free and why a phone that *does*
report metres is fusing an IMU or a depth sensor to get them. Calling the output "1.2 m" would
be inventing the unit (§46). The track is stored as `rel_x`/`rel_y` and described as relative and
scale-free everywhere it is surfaced.

Anchoring (`POST /captures/{id}/align`, `services/alignment.py`) is where scale, rotation and
origin all arrive at once. The operator names two or more frames and says where on the plan each
was taken; a transform is fitted to those correspondences and applied to every other frame.

That transform is a **similarity** — rotation, uniform scale, translation — and deliberately not
an affine or projective one. "Stretching" the path to meet its anchors sounds accommodating and
is the failure mode: independent axis scaling or shear lets the fit absorb a badly-placed anchor
by quietly distorting the building, and report a small residual while doing it. A similarity
transform has no freedom to hide that error, so a bad anchor surfaces as a large `rms_error_px`
and the fit is refused rather than applied with a caveat — nothing downstream distinguishes a
position that was trusted from one that was merely tolerated.

Intermediate frames are *transformed*, not interpolated between the anchors. Interpolating would
throw away the measured shape of the walk — the reason for estimating a track in the first place
— and replace it with a straight line through points nobody visited.

#### Spatial data: PostGIS, on PostgreSQL only

`capture_locations.geom` is a `geometry(Point, 0)` column, `GENERATED ALWAYS AS
ST_SetSRID(ST_MakePoint(x, y), 0) STORED`, with a GiST index. Three consequences worth knowing:

- **SRID 0, not 4326.** These are floor-plan pixels in a local Cartesian frame with no datum.
  Labelling them lat/lon would make every distance PostGIS returns meaningless.
- **Generated, so it cannot drift.** No application code writes a geometry, nothing needed
  backfilling, and the `x`/`y` floats remain the source of truth. It is not mapped in the ORM —
  a PostgreSQL-only type on a model that also compiles against SQLite — so `alembic/env.py`
  tells autogenerate to ignore it, along with PostGIS's own `spatial_ref_sys`.
- **PostgreSQL deployments need the extension.** `docker-compose.yml` and CI both use
  `postgis/postgis:17-3.5-alpine`; the migration fails with an actionable message on an image
  without it.

`app/services/spatial.py` has two implementations of one contract: KNN over the index via `<->`
on PostgreSQL, and an exact ordering by squared distance over `x`/`y` everywhere else. Same rows,
same order, same distances — only the query plan differs, which is what lets the suite prove the
behaviour on SQLite while `tests/integration/test_postgis_spatial.py` (run by the migrations job,
which has a PostGIS server) proves PostgreSQL agrees.

A click on the minimap does two things, because it asks two questions with different answers.
"Which frame of *this* walkthrough is nearest" is resolved in the browser from points already in
memory — a round trip would be the wrong trade for a click. "What else was captured near here,
on the other dates" is one only the server can answer, and
`GET /floors/{id}/captures-near?x=&y=&within_px=` answers it: the nearby capture points and every
visit to each, newest first. That is the time-travel query, asked by coordinate rather than by
location id, because the click lands on the drawing rather than on a point.

The radius is part of the SQL, not a filter over its results. It has to run before `LIMIT` or
"everything within this radius" silently becomes "whichever of the nearest N fell inside it", and
on PostgreSQL it is what lets the GiST index prune — `ST_DWithin` is index-assisted, a distance
computed per row and compared afterwards is not.

`GET /floors/{id}/locations/nearest` remains the plain "which point is this" lookup for callers
that hold a coordinate and no loaded plan.

Status is surfaced to the client exactly as §34 specifies:
`uploading → uploaded → processing → extracting_frames → analyzing → mapping → ready`, or
`failed` with a machine-readable reason and a retry endpoint.

The API never performs stage work inline on a request thread. `POST /captures/{id}/process`
enqueues and returns immediately.

### JobQueue abstraction

```python
class JobQueue(Protocol):
    def enqueue(self, job: JobSpec) -> str: ...
    def status(self, job_id: str) -> JobState: ...
```

| Backend | When |
| --- | --- |
| `RedisQueue` (RQ + Redis) | Docker Compose and production. Real durability, multiple workers. |
| `ThreadQueue` | Local development and tests without Redis. Background thread pool in the API process; same handler code, same `ProcessingJob` rows. |

This is why the platform runs end-to-end on a laptop with no Docker while remaining a real queued
system in deployment.

---

## 5. Vision engine

```
VisionEngine
 ├── FeatureDetector    ORB (OpenCV). Interface allows SIFT/AKAZE/learned features.
 ├── FeatureMatcher     BFMatcher + Lowe ratio test + RANSAC geometric verification.
 ├── MotionEstimator    Equirectangular-aware bearing shift → relative translation direction.
 ├── PositionEstimator  Incremental relative track with per-step confidence.
 ├── KeyframeSelector   Drops near-duplicates and low-quality frames.
 └── MapBuilder         Assembles frames + relative positions into a floor-plan-fittable track.
```

Honest scope, stated plainly in [VISION_PIPELINE.md](VISION_PIPELINE.md):

- This is **relative, monocular, scale-free** position estimation from equirectangular frames. It
  produces a topologically plausible walk order and relative offsets.
- It is **not** SLAM, and it does **not** claim metric or centimetre accuracy.
- When geometric verification is weak — too few inliers, degenerate motion, large gaps — the
  engine reports low confidence and the platform requires manual placement instead of drawing a
  path that looks automatic but is wrong (§14, §46).
- Every component is behind an interface so ORB-SLAM3, OpenVSLAM, COLMAP or OpenSfM can replace the
  estimator without touching the pipeline or the API.

---

## 6. Storage abstraction

```python
class StorageProvider(Protocol):
    def put(self, key: str, data: BinaryIO, content_type: str) -> StoredObject: ...
    def open(self, key: str) -> BinaryIO: ...
    def delete(self, key: str) -> None: ...
    def signed_url(self, key: str, ttl_seconds: int) -> str: ...
```

Key scheme, chosen so a key is self-describing and cheap to prefix-scan:

```
projects/{project_id}/floors/{floor_id}/sessions/{session_id}/{kind}/{uuid}.{ext}
kind ∈ { source, frames, thumbs, floorplans }
```

`LocalStorage` writes under `STORAGE_ROOT` and issues HMAC-signed, expiring URLs served by the API —
raw filesystem paths are never exposed to clients (§44). `S3Storage` issues native presigned URLs.

---

## 7. Camera abstraction

The MVP path is deliberately camera-agnostic (§6, §43):

```
360 camera → user copies files → app imports → pipeline → viewer
```

```
CameraProvider (Protocol)
 ├── LocalUploadProvider     browser / mobile multipart upload      [implemented]
 ├── FileImportProvider      watch/import a local folder or SD card [implemented]
 ├── Generic360Provider      OSC (Open Spherical Camera) over HTTP   [interface + partial]
 ├── Insta360Provider        vendor adapter                          [interface only]
 └── FutureCameraProvider    template for new adapters               [documented]
```

Capture *sources* are modelled separately from camera *transports*, so a future robot, drone or
fixed camera is a new `CaptureSource` value plus an adapter, not a schema change (§47):
`human_operator | robot | drone | fixed_camera`.

See [CAMERA_INTEGRATION.md](CAMERA_INTEGRATION.md).

---

## 8. Authentication and authorization

- Local email + password, hashed with bcrypt (via `passlib`).
- Short-lived JWT access token + rotating refresh token; both signed HS256 with `JWT_SECRET`.
- Four roles with a strict ordering: `admin > project_manager > site_engineer > viewer`.
- Route-level guards are FastAPI dependencies (`require_role`, `require_project_access`), so
  authorization is declared next to the route and cannot be forgotten in a service.

| Role | Projects | Captures | Upload | Annotate | Settings |
| --- | --- | --- | --- | --- | --- |
| Admin | CRUD | CRUD | ✓ | ✓ | ✓ |
| Project Manager | CRUD (own) | CRUD | ✓ | ✓ | project only |
| Site Engineer | read | create | ✓ | ✓ | — |
| Viewer | read | read | — | — | — |

---

## 9. Performance strategy

- Every list endpoint is cursor- or offset-paginated with a hard `limit` cap.
- The frontend never loads a project's frames eagerly: the viewer strip is virtualized and requests
  a page of thumbnails at a time.
- Three sizes per frame — `thumb` (256px), `preview` (1024px), `full` — so the filmstrip and the
  floor plan never fetch full panoramas.
- Panorama textures are loaded on demand, cached with an LRU bounded by count, and disposed on
  unmount to keep WebGL memory flat.
- Aggregates (coverage, timeline buckets, dashboard tiles) are computed with SQL aggregation, not
  by materializing rows in Python.
- Signed URLs are cacheable and CDN-frontable via `CDN_BASE_URL`, which replaces the scheme, host
  and any path prefix while carrying the signed query string through untouched. With `local`
  storage that is unconditional (the HMAC does not cover the host); with `s3` the distribution
  must use the same S3 endpoint as its origin, because SigV4 signs `Host`. The API is stateless,
  so it scales horizontally.

---

## 10. Observability

Structured JSON logs with a correlation id per request and per job, emitting the domain events
required by §45: `capture.created`, `file.uploaded`, `processing.started`, `processing.stage.done`,
`processing.failed`, `processing.completed`, `mapping.completed`, `viewer.opened`. The admin
processing dashboard reads `processing_jobs` directly, giving the exact table in §45.

---

## 11. BIM-ready design

`backend/app/bim/` contains interfaces only — `BimSource`, `BimElement`, `BimComparator`,
`CoordinateAlignment` — plus a documented target flow (IFC/Revit export → element graph → shared
coordinate system → align to capture locations → deviation report). Nothing is implemented, and the
API does not advertise BIM endpoints yet. The database already carries what alignment will need:
floor-plan pixel-to-real scale, floor elevation, and per-location coordinates.

---

## 12. Roadmap

Phase 1 (this MVP), Phase 2 (intelligent mapping), Phase 3 (construction intelligence) are detailed
in [ROADMAP.md](ROADMAP.md).
