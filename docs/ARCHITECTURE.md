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
        │                  │  (1.0 s default, configurable per session), minus
        │                  │  the stretches where the operator stood still,
        │                  │  plus thumbnails
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
  chunked:  POST /captures/{id}/multipart   → a PUT per part → .../multipart/complete
                                              (resumable; .../multipart/resume)
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

**Registration, not a storage event.** The usual sketch of this pattern finishes with the bucket
firing an event (SQS, a webhook) that wakes the worker. This finishes with the client calling
`/files` instead, for two reasons. It keeps the size and type checks: the server inspects the
stored object and *deletes* it when it fails, which an event-driven worker discovering the object
later cannot do before it has been paid for. And it works identically on `local` storage, so the
whole chain runs on a laptop with no bucket, no queue service and no cloud account — which a
storage-event trigger cannot.

The cost is that a client which uploads and then dies leaves an unregistered object. That is a
lifecycle-rule problem (expire unregistered keys under `source/`), not a correctness one: nothing
references it and no capture claims it.

With `local` storage the PUT target is `PUT /api/files/{key}`, signed the same way reads are —
except the HMAC covers the verb. Without that domain separation every asset URL ever handed to a
browser would also authorise overwriting the object it points at.

#### The third route: chunked and resumable

`POST /captures/{id}/multipart` → a signed PUT per part → `/multipart/complete` exists for the
failure the other two share: they are all-or-nothing. A two-gigabyte walkthrough that drops at
ninety percent has to be sent again from zero, over the connection that has just proved it
cannot hold — and the operator who walked the floor is the one who waits.

The part numbers, sizes and limits are S3's, stated once in `storage/multipart.py`: every part
but the last at least 5 MiB, at most 10,000 parts, the object being their concatenation in
order. The default part size is 8 MiB and grows when a file would otherwise exceed the part
limit, because a plan with 12,000 parts is rejected by S3 at part 10,001 — after hours of
transfer.

Two decisions carry the design:

**The server enumerates the parts.** `list_parts` on S3, a directory listing under
`staging/{upload_id}/` locally. A client that crashed mid-upload has no record of what
succeeded, and the only component that does is the storage backend — so "resumable" is a
behaviour rather than a word. It also removes a class of mistake, since a part the client
*believes* it sent cannot become part of the object unless the bytes are really there.

**A gap is refused, not assembled.** S3 completes an upload out of exactly the parts it is
given, so parts 1, 2 and 4 produce a real object: shorter than the original, decodable for the
first two thirds, and wrong in a way no size cap or magic-byte sniff downstream can detect. The
client therefore declares `size_bytes`, the server derives the part plan from it, and completion
checks the landed parts against that plan. The same declaration is what lets an oversized upload
be refused *before* any bytes move, which the presigned single PUT cannot do at all.

Locally, parts are ordinary objects under a staging prefix, which is what lets them reuse the
signed `PUT /api/files/{key}` route unchanged. The prefix deliberately sits outside the project
tree, so a part can never satisfy the "does this key belong to that session" check applied to a
finished object.

**Collecting what nobody finished.** An upload that is neither completed nor aborted leaves its
parts behind. On S3 that is the bucket's `AbortIncompleteMultipartUpload` rule; a filesystem has
no equivalent, so `LocalStorage.purge_stale_uploads` is the substitute — and it is *called*, which
is worth stating because for a while it was not. It existed, this document claimed it was the
safeguard, and nothing invoked it: every abandoned upload stranded its parts permanently, so a
2 GB walkthrough failing at ninety percent left nearly 2 GB behind for ever.

It runs at API startup, and again when a chunked upload begins, throttled to once an hour per
process (`storage/maintenance.py`). Tying it to the act that creates the garbage means a busy
deployment sweeps often and an idle one does not spin — and it needs no cron, no beat worker and
no second process, which is what keeps the whole stack runnable on a laptop. Age comes from the
staging manifest rather than directory mtime, because writing a part touches the directory and
would keep a stalled upload looking fresh indefinitely.

#### Which route a file takes

Both clients choose per file, by size alone, and by the same rule
(`frontend/src/lib/uploadStrategy.ts`, `mobile/src/lib/uploadPlan.ts`):

| Size | Route | What it buys, and what it gives up |
| --- | --- | --- |
| ≤ 32 MiB | proxied | Validation before storage and checksum de-duplication; holds a worker. |
| ≤ 64 MiB | direct | Frees the worker; a failed attempt re-sends up to 64 MiB. |
| > 64 MiB | chunked | A failed attempt re-sends one part; costs two extra round trips. |

32 MiB sits above any panorama this app produces and below any walkthrough video. 64 MiB is
roughly four minutes on a couple of megabits, which is about the longest single attempt worth
re-sending from the start.

Both offline queues route by the same rule — when the web one did not, a failed large upload was
re-queued and then retried *through* the API, which is the one transfer the route exists to keep
off it. Both also **persist the upload id next to the file** (IndexedDB in the browser, a column
on the SQLite queue on the phone), so an interrupted chunked upload continues from the last part
that landed rather than starting over. Losing that handle would leave the parts in storage
unreachable and still billable, and make the route two round trips more expensive than the one
it replaced.

React Native has no `Blob.slice` for a file on disk, so `mobile/src/lib/uploadIO.ts` reads each
part's byte range out and writes it to a temporary file before streaming it. One part exists
briefly as a base64 string; the cost is bounded by the part size rather than the file size,
which is the trade being made.

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

#### The queue has to be recoverable, not just durable

Durability was never the problem — SQLite and the device filesystem had that. Two ways a capture
could get *stuck* were:

**A file nobody could remove.** `queue.ts` had `enqueueFile` and `discardUploaded` and nothing for
a pending one, so a wrong file — wrong floor, wrong take, a 2 GiB video picked by mistake — could
not be taken out. It held device storage, and because files upload serially and a failure stops
the run, one that kept failing blocked every capture queued behind it. The only escape was
reinstalling the app, which throws away the captures the operator *does* want. `removeFile` fixes
that, behind a confirmation that names the file and says the copy is the only one, because it is.

A chunked upload already in flight is abandoned server-side first so its parts do not linger —
best effort, because the operator is usually removing a file *because* they are short of space or
signal, and refusing to free their storage until the server answers would deny them the one thing
they asked for. Stranded parts are bounded by `LocalStorage.purge_stale_uploads` or S3's
`AbortIncompleteMultipartUpload`; a device with no room is not.

**A file stuck in `uploading`.** `sync` marks a row `uploading` before the transfer and moves it
on afterwards. A phone killing the app in between is ordinary, and the row then kept that status
for ever — invisible to `sync`, which picks up only `pending` and `failed`. The capture silently
never uploaded and nothing on screen explained why. `openQueue` now returns those rows to
`pending`: nothing can legitimately be uploading at the moment the database opens, so they are
all orphans.

Both rules live in `mobile/src/lib/queuePolicy.ts` rather than in `queue.ts`, so the Node test
runner reaches them without booting expo-sqlite — the same split as `syncPolicy.ts`.

#### Offline in both directions: the reference-data cache

The mobile app was offline-first in one direction only. A capture recorded without signal was
held in SQLite and uploaded later — the hard half — but *starting* one needed three live requests
(`/projects`, `/projects/{id}/floors`, `/floors/{id}/zones`). An operator standing on a
half-built floor could not pick the floor they were standing on, so the capture they came to
record never began, and the upload queue could not help because nothing was queued yet.

`GET /sync` answers the whole question in one request and `mobile/src/lib/refData.ts` keeps the
answer, in the same SQLite database the upload queue uses. The capture screen reads only from
that cache; a connection refreshes it and is never a precondition.

**The manifest, and why it is not a plain change feed.** A delta on `updated_at` can report a
creation or an update and can never report a deletion — the row is gone, so nothing is left to
carry a timestamp. A client that only applies changes keeps a deleted floor for ever and offers
it to an operator whose captures the server will then refuse. So every response also carries the
id and timestamp of *every* row that should exist: absent from the manifest means deleted, newer
in the manifest than locally means missed. `mobile/src/lib/referenceSync.ts` is those rules as
pure arithmetic, and `backend/app/services/sync.py` is the other half of the same contract — one
has a test asserting a quiet sync still returns a full manifest, because the client is entitled
to treat an empty manifest as "hold nothing".

Affordable only because reference data is small: the seeded demo project tree is 9.8 KB in full
and 2.2 KB for a quiet delta, so the manifest's whole cost is about two kilobytes a sync. The
endpoint refuses above 20,000 rows rather than quietly returning a multi-megabyte one; at that
size the honest answer is a tombstone table. Captures and frames are deliberately not synced
this way — they are output, not reference data, and `/captures?since=` already pages them.

Three details that look like fussiness and are not:

* **The cursor is five seconds behind the present.** A row's `updated_at` can be *earlier* than
  the moment it became visible: SQLite stores `CURRENT_TIMESTAMP` to the second, and
  PostgreSQL's `now()` is the transaction start rather than the commit. A cursor at the present
  steps over anything written in the boundary tick, permanently. The first version did exactly
  that, and a test caught it.
* **SQLite compares DATETIME as text.** `func.now()` writes no fractional part while a bound
  Python datetime renders microseconds, so `'…12:00:01' >= '…12:00:01.000000'` is false as a
  string. The `since` bound is widened to an earlier whole second to compensate.
* **Timestamps are compared as instants on the client, never as strings.** ISO-8601 does not
  order lexicographically across formats — `"…00Z"` sorts *after* `"…00.5+00:00"` because `Z` is
  a later codepoint than `.` — so a string comparison calls a newer server record older and
  never resyncs it.

Cached timestamps come from the manifest rather than from the records, because only `ProjectOut`
carries `updated_at`; `BuildingOut`, `FloorOut` and `ZoneOut` carry `created_at` alone. Reading
it off the record cached `undefined` for three of the four collections, after which nothing could
be proved current and every sync became a full one.

#### Push, and why polling stays

The processing status page polled every twenty seconds. Correct, and slow in both directions: an
operator watching a five-minute walkthrough saw a bar that appeared stalled, a four-second stage
was invisible entirely, and every open tab asked a question whose answer was almost always
"nothing has changed".

So the pipeline announces what it did (`app/events/`), and `GET /api/events` — a WebSocket —
carries it to the browser. Measured against the running stack: **124 ms** from `POST /process`
to the capture list showing *Processing*, and all fifteen stage transitions of a real pipeline
run delivered live.

**Polling stays.** Push is an optimisation, never the source of truth: a dropped socket, a
missed frame, a tab that was asleep all have to be survivable, so the database stays
authoritative and the client keeps its poll as a floor. This makes the status fast; it does not
make it correct. Everything else follows from that — an event is a *hint* ("this capture moved,
ask again") rather than data, which is what lets a single authorisation check at connect suffice
and what makes a lost message harmless.

**The bus follows `QUEUE_BACKEND`, and does not get a setting of its own.** With
`QUEUE_BACKEND=redis` the worker is a separate process and cannot reach the API's open sockets,
so the message travels through the Redis the queue already needs. With `thread` the worker is a
thread of the API process, and an in-process fan-out is better — no broker, so the whole chain
still runs on a laptop. The broken combination, a Redis queue with an in-process bus, publishes
into the worker's own memory where nobody is listening, and *looks* fine: the pipeline runs, the
status is committed, the poll eventually finds it. Deriving the choice removes the option.

Three details that each cost something to get wrong:

* **Authentication is a message, not a query parameter.** A browser cannot set headers on
  `new WebSocket(url)`, and `?token=…` writes a live bearer credential into access logs, proxy
  logs and browser history. The socket opens subscribed to nothing and is closed with 1008 if no
  token arrives in ten seconds.
* **Subscribing is eager.** The first version returned an async generator, which registers
  nothing until its consumer first iterates — so an event published between "ready" and the first
  `async for` went to nobody, and the subscription could not be closed while an `__anext__` was
  pending, which is a waiting socket's normal state. `Subscription` is an object instead.
* **The reverse proxy needs its own location.** The general `/api/` block sets `Connection ""`
  to pool connections upstream, which is precisely the header an upgrade must carry instead —
  without `/api/events` as a separate block the socket fails behind nginx while working against
  the dev server, and the client falls back to polling so nothing looks broken. The idle timeout
  is raised to an hour there, because an idle socket is the normal state between captures.

A slow subscriber is dropped after 256 queued messages rather than growing without bound, which
is safe for the same reason everything else here is: it loses the notifications, not the state.

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
was taken; a transform is fitted to those correspondences and applied to every other frame. The
viewer's Plan tab drives it: open a frame, press *Anchor the current frame*, click the plan, then
repeat on a frame further along and press *Place N frames*.

Two anchors are the minimum and also a trap worth knowing about. A similarity transform has four
degrees of freedom and two correspondences supply exactly four constraints, so the fit reproduces
them perfectly however wrong they were — `rms_error_px` comes back 0.0 and means nothing. The UI
says so rather than printing a zero that reads as "perfect", and asks for a third anchor if the
operator wants the fit actually tested.

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

#### Recognising a floor that has been captured before

Anchoring by hand is the right answer exactly once per floor. After that the floor is covered in
frames whose plan coordinates are known, and a new walk can be placed by recognising where it
stands rather than being told: `vision/relocalize.py` shortlists the new frames against earlier
captures' placed frames by perceptual hash, matches with ORB, verifies against the spherical
epipolar geometry, and then asks whether a *rotation alone* explains the correspondences. Two
photographs from one spot differ only by rotation; move the camera and parallax appears
immediately. Three surviving matches feed the same similarity fit anchoring uses, and the walk
places itself. `services/relocalization.py` runs at the end of MAPPING and again on demand at
`POST /captures/{id}/localize`.

Three decisions in there are worth stating, because each is a place the feature could have been
quietly wrong instead:

**The parallax bar is 8°, not loop closure's 0.3°.** Slam asks whether a walk returned to a point
it stood on, and it did, so it can demand a centimetre. This asks whether an operator stood close
enough *a week later*, and they did not stand in the same place at all — 0.3° is 1.6 cm at 3 m
depth, and holding a revisit to that would recognise nothing, which is a feature that does not
exist. 8° is roughly half a metre at that depth and roughly 1.7 m across a 12 m space, because
parallax is an angle and monocular vision still has no scale. So a match means the cameras were
*close*, and the metres are an illustration rather than a measurement.

**The nearest candidate wins, not the first acceptable one.** That tolerance can be wider than
the spacing between capture points, so two adjacent reference points may both clear the bar for
one frame. Taking whichever hashed closest would place it at its neighbour — and because every
frame of the walk would shift the same way, the result is a pure translation, which a similarity
transform reproduces *with a zero residual*. Nothing downstream would ever see it. Parallax is
the only signal available that orders candidates by distance, so every shortlisted candidate is
verified and the least-parallax one is taken.

**Three matches, not two.** The same trap as manual anchoring, without the person: two
correspondences are fitted exactly, so the residual is zero whatever they were. Below three, only
the frames actually recognised are placed — directly, on their own evidence — and the rest of the
walk still needs anchoring. That is reported as mode `recognised` rather than `transform`.

A recognised frame **joins the earlier capture's location row** instead of getting a point of its
own nearby. Comparing a place across dates is built on two frames sharing a location, so a
capture placed two pixels from last month's is on the plan and compares against nothing. Frames
placed by the transform rather than recognised get new locations, because a fit says where a
frame probably was, not that it was where some earlier frame was.

**Drift is counted, not merely hoped against.** The points recognition creates become the
references for the next capture, so the tolerance above compounds along a chain — and every
generation looks exactly like the first in the coordinates, which is the whole danger.
`capture_locations.placement_generation` records how many hops separate a point from a person: 0
for one somebody placed or anchored, 1 for one fitted from generation-0 points, and so on.
`MAX_ANCHOR_GENERATION` (3) refuses to *create* points from references deeper than that; the walk
falls back to placing only what it recognised, and the note tells the operator to anchor one
capture by hand to reset the chain.

Reusing a point is never refused, however deep. Joining an existing point introduces no new
coordinate — the frame is exactly as right or wrong as that point already was, and shares that
error with every other capture standing there, which is precisely what date-to-date comparison
needs. Creating a point is where a new number enters the system, so that is the only thing
bounded.

**Undoing it removes the points too.** `DELETE /captures/{id}/placement` is the counterpart, and
the reason automatic placement is safe to run unattended. Unplacing the frames alone would leave
the invented points behind — empty, absent from every frame list, and still perfectly eligible as
references for the next capture, so a mistake someone had already caught would go on propagating.
The ids of created points are recorded in the report rather than inferred from provenance and
emptiness, because a wrong guess either strands a phantom point or deletes one an operator
declared by hand. Points the capture only joined are never removed: another capture may be
standing on them.

`placement_method` records `manual_anchors` or `cross_session`, and `localization_report` keeps
the evidence — every match with its inlier count and residual parallax, everything rejected and
why, and the fitted transform. The coordinates from the two paths are indistinguishable; how much
they should be trusted is not (§46).

#### Noticing that one upload was several walks

The same operator who walks a floor a week later also walks *three* floors in a morning and
uploads the lot, and a capture session has one `floor_id`. `vision/grouping.py` asks, per
consecutive pair, whether the walk continued — from the gap between frames and from the verified
inlier counts the MAPPING stage has already computed, so it adds no vision work. Both bars are
relative to the session's own behaviour: a gap counts against the median interval this capture
has been keeping, and an inlier count against what this capture's own links typically carry. A
fixed bar cannot serve both video sampled every two seconds and hand-taken stills, and real
imagery showed a fixed inlier floor failing too — two structurally identical spaces with
different surfaces still match by coincidence.

A pair too featureless to judge is reported as **unknown** rather than as a break, because
"nothing matched" and "there was nothing to match" look identical and only one is evidence.

`services/grouping.py` applies what a person confirms, and the thing they confirm is the part no
algorithm supplies: which floor each walk was on. The split moves no bytes — copying imagery
cannot be made atomic with the transaction that re-points the rows, so a walkthrough's frames stay
under the storage prefix of the upload they came from and `captures.delete_session_storage` stops
assuming a capture owns everything beneath its own prefix.

#### Project membership: which projects a role reaches

Roles answered what a user may do and nothing answered which projects they may do it to, so every
authenticated account could read every project in the deployment. `project_members` closes that,
and the shape of the fix matters more than the table:

**Enforced in the resource resolvers, not route by route.** `api/deps.py` already loads the
project behind every path — a frame resolves to a session, a session to a project — so the
membership check lives there and a new route that loads a frame cannot forget it. The `user`
argument on those resolvers is **required**, which is the whole trick: adding the parameter turned
66 call sites into type errors, and a missed one is a failed build rather than a silent hole.

**The required-argument trick has a hole, and six routes sat in it.** Making `user` required on
the resolvers turns a forgotten call site into a type error — but only for a route that calls a
resolver at all. `/projects/{id}/timeline`, `/coverage`, `/frequency`, `/stale-areas` and
`/annotations`, plus `/floors/{id}/paths`, took the project or floor id, filtered a query on it,
and resolved nothing. Their signature was `_: RequireProjectRead`: the capability guard runs, and
the user it returns is thrown away, so there was no argument to require and nothing to type-check.
`project_dashboard` sits directly above `project_timeline` in the same module doing it correctly,
which is how the pattern survived being read.

They answered, to any account in the deployment, what had been inspected, how often, what was
overdue, and every defect raised with its title and description. Found by walking the route table
rather than by reading it, and the walk is now a test: `test_project_scoping` fails on any route
taking a scoped id that calls no resolver. The lesson worth keeping is that "enforced in the
resolvers" is only a guarantee for code that goes through them, and nothing structural was
stopping a route from not.

That covered most of the API and not all of it, and the gap was instructive. Routes that address
a resource by *its own* id — `/annotations/{id}`, `/reports/{id}`, `/locations/{id}`,
`/floorplans/{id}`, `/zones/{id}` — resolve no parent, so they had no resolver to check. Each got
one. `POST /captures` was the same shape in reverse: the service loaded the floor itself to find
the project, which answers no authorization question, so an engineer could start a capture on any
floor id in the deployment. That is a write into another company's job, and a scoping test caught
it rather than a reading of the code.

**List endpoints narrow rather than refuse,** and their totals narrow with them — a count that
included projects whose rows were filtered out would report how much other work exists. `/sync`
matters most here: it is the widest read in the API and a phone keeps its copy offline.

**The event socket filters too.** Every authenticated socket used to receive every capture-status
event in the deployment. `publish_capture_status` now requires a `project_id` — required, not
optional, because an event with no project is an event nobody can filter — and the socket resolves
its visible set once at authentication rather than per event, since the bus fans out to every
listener and a query per listener per event would cost more than the polling it replaced. Access
revoked mid-connection is therefore honoured on reconnect; acceptable, because an event is a hint
to refetch and the refetch is checked.

**404, not 403.** A 403 on a project a stranger was never told about confirms it exists, and
codes are guessable. Administrators are exempt from membership entirely: they hold
`settings:system`, and a deployment where the admin could be locked out of a project would have
nobody able to fix it but the person locked out.

**The migration grants everyone access to everything.** That reads like a defeat and is the only
honest option: nobody ever recorded who should see what, because there was nowhere to record it,
so any narrower rule would be a migration inventing an access policy and enforcing it on a live
deployment. It preserves exactly today's access and changes nothing for anyone; enforcement is
real from that point on, and an administrator removes the backfilled rows once they know who
belongs where.

#### Getting an account back, without pretending to have email

There was no way to recover a forgotten password. `/auth/change-password` needs the current one,
and an administrator could change a user's name, role and active flag but not their credential —
so the most ordinary support request there is required somebody with database access. The Users
tab compounded it: a read-only table, while `POST /users`, `PATCH /users/{id}` and `DELETE
/users/{id}` had always existed on the server, so a fresh deployment could not add one person
without calling the API by hand.

Self-service recovery is impossible here and the fix does not pretend otherwise. Proving you own
an address means sending something to it, and nothing in this platform may depend on a mail
service to run locally. So recovery is an administrator action, and the whole design question is
whether the credential they set stays temporary — because a "reset" that merely sets a password
hands out a working account protected by a secret somebody else chose and very possibly said out
loud.

`users.must_change_password` is the answer, and three things about it are deliberate.

**It is checked against the row on every request, not written into the token.** A login-time check
would miss the case that matters: the person whose password is being reset because somebody else
may be in the account is precisely the person already holding a valid token. Reading the flag in
`api/deps.get_current_user` means a hold applies to a session opened before the reset, and no
token outlives the requirement — the same reasoning that already governs a role change there.

**The exemption is a second dependency, not an allow-list.** `/auth/me` and
`/auth/change-password` have to stay reachable or the holder cannot recover, and the obvious
implementation is a set of route names consulted inside the check. That puts the decision next to
the check, where a route added to the set by accident is invisible. Instead there is
`get_user_setting_password`, and an endpoint is reachable by a held account only because its
signature says so. Every other endpoint is protected by not saying so.

**The new password must differ from the temporary one.** Otherwise the requirement could be
satisfied by keeping the administrator's password, which is the single thing the hold exists to
prevent.

The refusal is a 403 carrying `code: "password_change_required"` rather than a 401. The token is
genuine, so a client treating it as an expiry would refresh in a loop and never reach the screen
it needs; the code is what says "send them to the change-password form". The web client honours
that by replacing the application shell entirely — navigation rendered around it would be a grid
of panels each reporting its own 403, with the one available action the hardest thing on screen
to find.

#### What the platform records, and who says so

`GET /api/system/events` advertises the event names this build emits, because a deployment with no
log aggregator attached has nothing else to go on. It carried a hard-coded list of eight while the
code emitted thirty-five — not a gap in the logging but a false statement about it, served from
the endpoint whose job is to describe the system.

The names now live in `core/audit.py` and the endpoint reads them. What keeps that true is not
diligence: `tests/unit/test_audit_events.py` parses every `log_event()` call in `app/` and fails
if the two sets differ **in either direction**, because a registry that over-claims is the same
kind of lie as one that under-claims and is the easier one to leave behind after deleting a
feature. It found one immediately — a call that chose between two names with a conditional
expression, greppable by neither, which is exactly what `log_event` exists to prevent.

The events worth having were the ones missing entirely. Deleting a capture destroys imagery;
deleting a capture location silently unplaces every frame that ever stood there, across sessions
and dates; changing a role hands somebody the platform. `project.deleted` was logged, so the
intent had been there and simply not finished. Each of these now records what cannot be
reconstructed after the fact — the counts that go with a deletion, and both sides of a role
change — and the specific list is asserted by name in that test rather than inferred from a
prefix, so a refactor that quietly drops one fails there and not in an incident.

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

#### A capture has to survive the worker processing it dying

That status list assumed every stage reaches an end. A worker killed mid-stage reached none: its
`ProcessingJob` stayed RUNNING, the capture stayed `processing`, and nothing ever reconsidered
either. The capture was then unrecoverable *through the product* — `/retry` refused because the
session was `processing` rather than `failed`, `/process` refused because a job was still
active, and the web client rendered no button at all, since `processing` counts as in-flight and
hides both. An operator's day of walking a site sat behind a spinner that would never stop, and
the only way out was a forced reprocess through the API by hand.

**Elapsed time cannot detect this.** A stage commits RUNNING and does not touch the row again
until it finishes, so a long video extraction and a dead worker look identical from outside. A
timeout would eventually kill live work and tell somebody to redo something that was running.

So the queue is asked, and each backend can answer exactly:

* `ThreadQueue` runs work in a pool inside the API process, so a job that started before the
  current queue existed cannot be running in it. That is a fact about the backend, not a guess.
* `RedisQueue` asks RQ, which knows whether a job is still queued, started or gone.

`is_abandoned` answers **False for "alive *or* cannot tell"** — the two are deliberately the
same answer, because failing a capture tells an operator their work needs rerunning and that is
not a decision to take on a maybe. A job with no recorded queue id, or a Redis that cannot be
reached, is left exactly where it is.

`pipeline.reclaim_abandoned_jobs` runs on every status read rather than from a scheduler: one
indexed query scoped to the capture somebody is already looking at, so it needs no new
infrastructure and the operator staring at the stuck capture is the thing that triggers the
repair. The stage is marked `failed` with `error_code: worker_lost`, the session is marked
failed with a reason that says what happened, and the ordinary retry button — which needed
`failed` all along — appears.

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
  by materializing rows in Python — and the portfolio aggregates across *all* visible projects in
  one set of grouped queries rather than looping. It did loop, and the cost was measured rather
  than suspected: 77 queries for six projects, 149 for twelve, so twelve per project on the first
  screen after signing in. A contractor with fifty live jobs would have issued around six hundred.
  It is now a fixed eleven.
- Detail for a *page* of frames is assembled in a fixed number of queries, not one set per row.
  `viewer.frame_details` fetches the session context once per distinct session — a list shares
  one — plus one quality query and one grouped annotation count for the whole page. Per frame it
  cost five: 9 queries for one frame, 54 for ten, 104 for twenty, so the default page of fifty
  ran to about 254. It is now eight at any page size.
- Neither is visible on demo data, which is why both are held by `tests/integration/
  test_query_counts.py` — it counts statements and asserts the count does not change with the
  number of rows. A ceiling alone would not do: the frame list passed `< 15` while still growing
  per row, because the remaining lazy load arrived *through* the serialiser rather than from any
  line that looked like a query.
- Signed URLs are cacheable and CDN-frontable via `CDN_BASE_URL`, which replaces the scheme, host
  and any path prefix while carrying the signed query string through untouched. With `local`
  storage that is unconditional (the HMAC does not cover the host); with `s3` the distribution
  must use the same S3 endpoint as its origin, because SigV4 signs `Host`. The API is stateless,
  so it scales horizontally.

---

## 10. Observability

Structured JSON logs with a correlation id per request and per job. The domain event names are
registered in `app/core/audit.py` and reported by `GET /api/system/events`; a unit test holds that
registry to the `log_event()` calls in the package, in both directions. They are deliberately not
listed again here — this paragraph previously named eight of them, one of which
(`processing.stage.done`) had never existed, and that is the failure mode a registry exists to
prevent. The admin processing dashboard reads `processing_jobs` directly, giving the exact table
in §45.

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
