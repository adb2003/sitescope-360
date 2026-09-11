# SiteScope 360

An independent construction 360° site capture and monitoring platform.

Create a project, add buildings, floors and zones, upload 360 imagery or video from any camera,
process it into viewable panoramas, place those panoramas on a floor plan, walk the site in a
browser, compare dates, track coverage and capture cadence, raise findings, and export reports.

> ### Independence
>
> This platform does **not** use, wrap, proxy, scrape or embed OpenSpace — or any other
> proprietary construction-monitoring vendor's — API, SDK, application, account, private
> endpoints or code. Capture processing, panorama viewing, floor-plan mapping, coverage and
> reporting are all implemented in this repository on permissively licensed open-source
> components. The running server states this itself at `GET /api/system/capabilities`, and the
> claim is asserted by an automated test.

> ### Honesty about analysis
>
> Every positional or derived value carries a provenance of `measured`, `estimated`,
> `ai_predicted` or `manual`, and the interface shows it. Automatic mapping reports a confidence
> and **refuses to publish a capture path it does not believe**, asking for manual placement
> instead. Coverage is the ratio of visited capture points to points an administrator declared —
> it is never inferred from imagery. Progress detection, defect detection, object detection and
> BIM comparison are **not implemented**, and the platform says so rather than producing
> plausible numbers.

---

## Quick start

Two supported paths. The Docker one is the production-shaped stack; the local one needs no
Docker, Postgres or Redis and is what most development uses.

### Docker (full stack)

```bash
cp .env.example .env
# Set JWT_SECRET and STORAGE_SIGNING_SECRET — compose refuses to start without them:
python -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('STORAGE_SIGNING_SECRET=' + secrets.token_urlsafe(48))"

docker compose up --build
```

Then open <http://localhost:8080>. Seed the demo data:

```bash
docker compose exec api python -m app.seed --demo
```

Brings up postgres, redis, the API, a worker, the built frontend and an nginx reverse proxy.

### Local (no Docker)

```bash
# 1. Backend
cd backend
uv venv --python 3.12
uv pip install -e ".[dev]"
cd ..
cp .env.example .env          # defaults use SQLite + an in-process queue

cd backend && python -m alembic upgrade head && cd ..
cd backend && python -m app.seed --demo && cd ..    # ~3 min: renders the demo imagery

# 2. API (from the repository root, so ./var paths resolve consistently)
python -m uvicorn app.main:app --reload --port 8000

# 3. Frontend, in a second terminal
cd frontend && npm install && npm run dev
```

Open <http://localhost:5173> and sign in with the credentials the seeder prints
(`admin@demo.example.com`). API docs are at <http://localhost:8000/docs>.

Full detail, including the Windows notes and the SQLite/Postgres trade-off:
**[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)**.

---

## What you can do

| | |
| --- | --- |
| **Dashboard** | Coverage, capture cadence and overdue status across every project. |
| **Projects** | Project → Building → Floor → Zone, with declared area and expected capture points. |
| **Site Capture** | Start a session, upload 360 images or video, watch the pipeline, retry a failure. |
| **Floor Plans** | Upload a drawing, calibrate it, place and drag capture points, see the capture path. |
| **360 Viewer** | Drag, zoom, pinch, fullscreen, walk between positions, pin findings, review quality. |
| **Timeline** | Capture activity by date with the share of capture points visited. |
| **Compare** | Two dates at the same point, side by side or wiped, orientation-synchronised. |
| **Annotations** | Findings pinned inside a panorama, with priority and resolution workflow. |
| **Reports** | Project summary, floor coverage, capture history, annotation log — PDF, CSV or JSON. |
| **Settings** | Capture cadence, camera devices, processing jobs, users, and a capability report. |

Roles: **Admin**, **Project Manager**, **Site Engineer**, **Viewer** — enforced at the route, not
just in the UI.

---

## Repository layout

```
.
├── backend/           FastAPI app, worker, vision engine, storage, migrations, tests
│   ├── app/
│   │   ├── api/       REST routes, one module per resource group
│   │   ├── auth/      JWT, password hashing, capability-based RBAC
│   │   ├── bim/       future BIM interfaces — declared, deliberately unimplemented
│   │   ├── cameras/   CameraProvider registry and adapters
│   │   ├── processing/ pipeline stages, media decoding, synthetic demo imagery
│   │   ├── queue/      JobQueue abstraction (RQ/Redis, or an in-process pool)
│   │   ├── reports/    report builders and PDF/CSV renderers
│   │   ├── services/   domain logic shared by routes and workers
│   │   ├── vision/     VisionEngine: quality, features, motion, position, mapping
│   │   └── workers/    worker entrypoint and stage handlers
│   ├── alembic/       migrations
│   └── tests/         unit · vision · integration
├── frontend/          Vite + React + TypeScript + Tailwind web app
├── mobile/            Expo capture app — offline-first upload queue
├── shared/            generated cross-language enum contracts
├── infrastructure/    nginx config, helper scripts
├── sample-data/       generated demo imagery (see sample-data/README.md)
├── docs/              architecture, API, development, vision, cameras, licences, roadmap
├── docker-compose.yml
└── .env.example
```

Why `worker/` and `vision/` live inside `backend/` rather than at the top level is explained in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#2-repository-layout).

---

## Documentation

| Document | What it covers |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, domain model, pipeline, storage, auth, performance. |
| [API.md](docs/API.md) | Every endpoint, the error envelope, pagination, signed assets. |
| [DEVELOPMENT.md](docs/DEVELOPMENT.md) | Running it, testing it, the database choice, troubleshooting. |
| [VISION_PIPELINE.md](docs/VISION_PIPELINE.md) | How the CV actually works, measured accuracy, and its limits. |
| [CAMERA_INTEGRATION.md](docs/CAMERA_INTEGRATION.md) | Adding support for a camera, robot or drone. |
| [OPEN_SOURCE_LICENSES.md](docs/OPEN_SOURCE_LICENSES.md) | Every dependency and its licence. |
| [ROADMAP.md](docs/ROADMAP.md) | Phase 1 (this MVP), Phase 2, Phase 3. |
| [mobile/README.md](mobile/README.md) | The Expo capture app and its verification status. |

---

## Testing

```bash
# Backend: 144 tests
cd backend && python -m pytest -q

# Backend quality gates
python -m ruff check app tests && python -m ruff format --check app tests && python -m mypy app

# Frontend: 43 tests
cd frontend && npm test && npm run lint && npm run typecheck && npm run build
```

The suite includes a single test that walks the entire §52 Definition of Done through real HTTP —
`backend/tests/integration/test_definition_of_done.py` — and ground-truth geometry tests that
verify the vision engine recovers a *known* camera pose from rendered panoramas rather than
merely producing plausible output.

---

## Verification status

Stated plainly, because "it builds" and "it works" are different claims.

| Area | Status |
| --- | --- |
| Backend: 144 tests, ruff, ruff-format, mypy | **Verified** — all pass on this machine |
| Migrations on SQLite; PostgreSQL DDL generation; no model drift | **Verified** |
| Full §52 workflow end to end over HTTP | **Verified** |
| Two-view geometry vs ground truth | **Verified** — known pose recovered to <1° yaw, <0.05 m track error on exact correspondences |
| Relative positioning through ORB, end to end | **Verified, platform-dependent** — 0.13 m mean on Windows, 1.57 m on Linux CI. See [VISION_PIPELINE.md](docs/VISION_PIPELINE.md#measured-accuracy). |
| Mapping refusal when frames are too far apart | **Verified** |
| Frontend: 43 tests, ESLint, TypeScript, production build | **Verified** |
| Demo seed, live API over HTTP | **Verified** |
| Docker Compose, Dockerfiles, nginx | **Written, not executed locally** — Docker is not installed on this machine. CI builds both images and runs a stack smoke test. |
| Expo mobile app | **Complete and syntax-checked, not executed** — no iOS/Android toolchain here. |

---

## Licence

Apache-2.0. See [docs/OPEN_SOURCE_LICENSES.md](docs/OPEN_SOURCE_LICENSES.md) for dependencies.
