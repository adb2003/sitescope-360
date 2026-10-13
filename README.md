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
# Backend suite
cd backend && python -m pytest -q

# Backend quality gates
python -m ruff check app tests alembic && python -m ruff format --check app tests alembic && python -m mypy app

# Frontend suite, lint, types and a production build
cd frontend && npm test && npm run lint && npm run typecheck && npm run build
```

The suite includes a single test that walks the entire §52 Definition of Done through real HTTP —
`backend/tests/integration/test_definition_of_done.py` — and ground-truth geometry tests that
verify the vision engine recovers a *known* camera pose from rendered panoramas rather than
merely producing plausible output.

---

## Verification status

Stated plainly, because "it builds" and "it works" are different claims.

Test counts are deliberately not quoted here. They were — "427 tests", "117 tests" — and both
had drifted by roughly a hundred before anyone noticed, which makes a table about trustworthiness
the wrong place for a number nothing keeps true. The one count still quoted anywhere is the
endpoint total in [API.md](docs/API.md), and that one is correct because a test fails when it is
not. What runs is listed below; [CI](.github/workflows/ci.yml) runs all of it on every commit.

| Area | Status |
| --- | --- |
| Backend suite, ruff, ruff-format, mypy | **Verified** — all pass on this machine and in CI on every commit |
| Migrations on SQLite; PostgreSQL DDL generation; no model drift | **Verified** |
| Full §52 workflow end to end over HTTP | **Verified** |
| Two-view geometry vs ground truth | **Verified** — known pose recovered to <1° yaw, <0.05 m track error on exact correspondences |
| Relative positioning through ORB, end to end | **Verified, platform-dependent** — 0.13 m mean on Windows, 1.57 m on Linux CI. See [VISION_PIPELINE.md](docs/VISION_PIPELINE.md#measured-accuracy). |
| Mapping refusal when frames are too far apart | **Verified** |
| Cross-session placement: a second capture of a floor places itself | **Verified** — over HTTP against the live API and in `test_cross_session.py`; the revisit joined all five of the first capture's points rather than making its own. The parallax bar is a viewpoint tolerance, not an identity test, and points created by recognition record how many hops they are from a point a person placed — see [limitation 6](docs/ROADMAP.md). |
| Projects scoped to their members | **Verified** — over HTTP against the live API and in `test_project_scoping.py`: removing a member narrows their project list, portfolio totals and offline sync, and a direct fetch of that project returns 404 rather than 403. |
| Share links: one view, no account | **Verified** — created, opened with no `Authorization` header, and revoked against the live API; the public page checked in a browser with storage cleared. A wrong, expired and revoked token return byte-identical refusals. |
| Frontend suite, ESLint, TypeScript, production build | **Verified** — on this machine and in CI |
| One upload split into its separate walkthroughs | **Verified** — over HTTP and in a browser: a capture crossing two floors reported two walkthroughs from a 9-inlier scene break, split into a second capture on another floor, and the original's imagery survived deleting it. Which floor each walk was on is a person's answer, never inferred. |
| A forgotten password can be recovered | **Verified** — over HTTP and in a browser: an administrator reset holds the account to `/auth/me` and `/auth/change-password` until it sets its own, including for a session opened *before* the reset, and the temporary password cannot be kept. |
| Destructive and privilege-changing actions are recorded | **Verified** — the registry in `core/audit.py` is held to the `log_event()` calls in both directions by a unit test. |
| Capture points drawn only on the plan revision they were placed on | **Verified** — in a browser: switching sheets withholds the others and says so, and switching back restores them, because a sheet already rendered is re-activated rather than rendered again. |
| Reporting views scoped to project members | **Verified** — six endpoints answered for any project to any account; a route-table guard now fails on any scoped route that resolves no membership. |
| A capture survives the worker processing it dying | **Verified** — over HTTP: a stage stranded by a dead worker becomes `failed`/`worker_lost` on a status read, and the ordinary retry works. The queue is asked rather than a clock, so live work is never failed. |
| Hot endpoints cost a fixed number of queries, not one per row | **Verified** — measured before and after: the frame list 104 → 8 at twenty rows, the landing page 149 → 11 at twelve projects, the project list 87 → 10 at twelve, the viewer manifest 15 → 10. A sweep of the other fourteen list endpoints found them already fixed-cost. Held by `test_query_counts.py`. |
| A capture the server refuses does not block the ones behind it | **Verified** — the phone drained serially and stopped on any failure, so one file the API would never accept was tried first on every drain and every capture queued behind it stayed on the device. A refusal is now terminal and the drain continues past it; the status is read through the wrapper a chunked upload throws, which is the route large files take. Nothing is deleted, and the queue never says "everything is uploaded" while a refused file is still there. |
| Text in the interface clears WCAG AA contrast | **Verified** — measured in a browser on every screen, in both themes. Two tokens failed: `--ink-subtle` (3.37:1 light, 3.88:1 dark) and the light `--warn` (3.59:1), between them carrying section labels, column headings, the capture-point codes on a plan — 69 on one screen — and the open-issue count. Both raised, and `contrast.test.ts` now computes every ink and status token against every surface from the stylesheet itself. |
| The browser tab says which page and which job | **Verified** — every route set the same title, which is wrong where this product is actually used: two jobs open side by side to compare them. Set by `PageHeader`, so the tab and the `h1` cannot drift apart. |
| Keyboard and screen-reader basics | **Verified** — a skip link, first in the DOM and first in the tab order, checked in a browser with real keyboard focus; and a failure callout now carries `role="alert"`, so an upload the server refused is announced rather than silently appearing next to the button. |
| Security headers on every response, including the documentation pages | **Verified** — over HTTP and in a browser. The application had none of its own, so an API behind anything other than this repository's nginx sent none; and the gateway's `script-src 'self'` applied to Swagger and ReDoc, which load their viewer from a CDN, so the production stack served a blank `/docs`. Both pages were then loaded against the new policy and the console read — which is how a third origin nothing in the HTML names was found. |
| A role held on a project, not over the deployment | **Verified** — over HTTP and in a browser: the same site engineer started a capture on one job and was refused on another with the role named, and the capture wizard appeared and disappeared with nothing changing but that role. The account role is a ceiling, applied on every read, so demoting an account demotes it everywhere at once. Nobody can hand out a role above their own. |
| Demo seed, live API over HTTP | **Verified** |
| Docker Compose, Dockerfiles, nginx | **Written, not executed locally** — Docker is not installed on this machine. CI builds both images and runs a stack smoke test, and `test_gateway_conf.py` parses the gateway config for the properties that were got wrong before. Neither is nginx actually running it. |
| Mobile: TypeScript, 31 camera-protocol tests | **Verified** — the OSC client takes its transport as a dependency, so it runs under Node in CI. |
| Expo app rendering under `expo start --web` | **Verified** — the camera and capture screens render and sign in against the live API. No keychain and no durable queue exist in a browser, and the UI says so. |
| Expo mobile app on a device | **Complete and type-checked, not executed** — no iOS/Android toolchain here. |
| Camera control against physical hardware | **Not verified** — no 360 camera on this project. The protocol is tested against a fake transport; a real device may differ. |

---

## Licence

Apache-2.0. See [docs/OPEN_SOURCE_LICENSES.md](docs/OPEN_SOURCE_LICENSES.md) for dependencies.
