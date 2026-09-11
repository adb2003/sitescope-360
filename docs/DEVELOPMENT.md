# Development

## Requirements

| Tool | Version | Notes |
| --- | --- | --- |
| Python | 3.11+ (3.12 recommended) | `uv` will download it for you |
| [uv](https://docs.astral.sh/uv/) | 0.5+ | Or use `python -m venv` + `pip` |
| Node.js | 20+ (22 recommended) | For the frontend |
| Docker | 24+ | **Optional** — see below |

PostgreSQL and Redis are **not required** for local development. That is a deliberate design
choice, not a shortcut: see [Database and queue](#database-and-queue).

---

## Setup

```bash
# 1. Environment
cp .env.example .env

# 2. Backend
cd backend
uv venv --python 3.12
uv pip install -e ".[dev]"

# 3. Schema
python -m alembic upgrade head

# 4. Demo data (takes ~3 minutes: it renders the panoramas)
python -m app.seed --demo
#    Or, for a fast smoke test with smaller, fewer frames:
python -m app.seed --demo --quick

cd ..

# 5. Frontend
cd frontend
npm install
cd ..
```

The seeder prints the accounts it created and their password.

## Running

Two terminals:

```bash
# Terminal 1 — API, from the repository root
python -m uvicorn app.main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend && npm run dev
```

| | |
| --- | --- |
| Web app | <http://localhost:5173> |
| API | <http://localhost:8000> |
| Swagger UI | <http://localhost:8000/docs> |
| ReDoc | <http://localhost:8000/redoc> |
| Health | <http://localhost:8000/api/system/health> |

### Run the API from the repository root

`ENVIRONMENT`, `DATABASE_URL` and `STORAGE_ROOT` are read from `.env` at the repository root, and
relative paths in them are **anchored to the repository root** rather than the process working
directory (`app/core/config.py`). Without that anchoring, `alembic upgrade` run from `backend/`
would migrate `backend/var/dev.db` while `uvicorn` run from the root served an empty
`./var/dev.db` — a silent, genuinely confusing failure. The anchoring makes the directory you
launch from irrelevant, but the root is still the conventional place.

---

## Database and queue

The platform runs two ways, and the same code paths serve both.

| | Local default | Docker / production |
| --- | --- | --- |
| Database | SQLite (`./var/dev.db`) | PostgreSQL 17 |
| Queue | In-process thread pool | Redis + RQ, separate worker |
| Storage | Local filesystem (`./var/storage`) | Local volume, or S3-compatible |

**Why SQLite is supported.** The alternative is that nobody can run the platform without Docker,
and a contributor's first experience is a container debugging session. Every model uses portable
column types chosen at DDL-compile time (`JSONB` on PostgreSQL, `JSON` elsewhere), and CI runs the
migrations against a real PostgreSQL **and** asserts the models have not drifted from them — so a
SQLite-only schema cannot ship.

**Why the thread queue is supported.** The `JobQueue` protocol has two backends that run the *same*
handler functions and write the same `processing_jobs` rows. The thread backend gives up exactly
one thing: durability across a restart. The API logs a warning saying so at startup, and
`GET /api/jobs/summary` reports `"durable": false`. Set `QUEUE_BACKEND=redis` for the real thing.

To use PostgreSQL locally:

```bash
# In .env
DATABASE_URL=postgresql+psycopg://sitescope:sitescope@localhost:5432/sitescope
QUEUE_BACKEND=redis
REDIS_URL=redis://localhost:6379/0
```

Then run a worker in a third terminal: `python -m app.workers.run`.

---

## Testing

```bash
cd backend

python -m pytest -q                      # all 144
python -m pytest tests/vision -q          # vision only
python -m pytest tests/integration -q     # HTTP end-to-end
python -m pytest -q -m "not slow"         # skip the slow rendering tests
python -m pytest -q --cov=app --cov-report=term-missing
```

Quality gates — the same commands CI runs:

```bash
python -m ruff check app tests
python -m ruff format --check app tests
python -m mypy app
```

Frontend:

```bash
cd frontend
npm test            # 43 tests
npm run lint
npm run typecheck
npm run build
```

### The tests worth knowing about

| File | What it proves |
| --- | --- |
| `tests/integration/test_definition_of_done.py` | The whole §52 workflow, in order, through real HTTP. One test, shared state — because the requirement is that the *workflow* works, not that 18 endpoints each work alone. |
| `tests/vision/test_geometry.py` | The two-view geometry recovers a **known** camera pose from rendered projections (yaw to <1°, translation direction to cos > 0.99). The only way to know the maths is right rather than plausible. |
| `tests/vision/test_mapping.py` | Track accuracy vs ground truth — split into a deterministic geometry test (exact correspondences, centimetre bound, portable) and a `slow`-marked end-to-end test through ORB whose bound is a fraction of the room diagonal because the detector is platform-dependent. Also that wide frame spacing is refused rather than guessed. |
| `tests/integration/test_pipeline.py` | Video frame extraction at a configurable interval, failure/retry, idempotent reprocessing, and that reprocessing **never deletes an original upload**. |
| `tests/integration/test_permissions.py` | The §27 role matrix enforced at the HTTP boundary, token rotation, and that a role change invalidates an existing token immediately. |
| `frontend/src/components/viewer/geometry.test.ts` | The TypeScript viewer's spherical convention matches the Python backend term by term. |

### Regenerating the shared contracts

`shared/contracts.ts` and `shared/contracts.json` are generated from the backend's `StrEnum`
definitions. CI fails if they are stale.

```bash
python infrastructure/scripts/export_contracts.py
python infrastructure/scripts/export_contracts.py --check   # what CI runs
```

---

## Migrations

```bash
cd backend

python -m alembic upgrade head
python -m alembic downgrade -1
python -m alembic revision --autogenerate -m "add something"
python -m alembic history
```

Two things the setup does on purpose:

- **Migrations never import application code.** `alembic/env.py` installs a `render_item` hook
  that renders the project's custom column types as plain SQLAlchemy (`UTCDateTime` →
  `sa.DateTime(timezone=True)`, `JSONType` →
  `sa.JSON().with_variant(postgresql.JSONB(), "postgresql")`). Otherwise every migration would
  depend on live application code, and deleting a type later would break the ability to replay
  history.
- **`render_as_batch` is enabled for SQLite**, because SQLite cannot `ALTER` most columns; batch
  mode rewrites the table instead, so one migration script works on both backends.

Check for drift:

```bash
python -m alembic revision --autogenerate -m "drift check"
# The generated upgrade() should contain only `pass`. Then delete the file.
```

---

## Docker

```bash
cp .env.example .env
# Compose refuses to start without these two:
python -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET
python -c "import secrets; print(secrets.token_urlsafe(48))"   # STORAGE_SIGNING_SECRET

docker compose up --build
docker compose exec api python -m app.seed --demo
```

<http://localhost:8080> serves the SPA and proxies `/api` to the backend — one origin, which
matters because signed asset URLs are absolute against `PUBLIC_BASE_URL`.

```bash
docker compose logs -f api worker
docker compose exec api python -m alembic upgrade head
docker compose up -d --scale worker=3      # frame extraction is CPU-bound
docker compose down --volumes               # destroys the database and storage
```

The API and worker are the **same image** with different commands: they share the ORM models,
storage providers and vision engine, so two images would mean duplicating that code or
maintaining a third shared package.

> **Not verified here.** The Docker stack, Dockerfiles, nginx config and CI workflow are written
> but were not executed while building this, because Docker is not installed on the development
> machine used. The CI `stack` job runs `docker compose up --wait` and asserts the API is healthy
> behind nginx, the SPA is served, a seed produces frames, and the worker consumed Redis jobs.

---

### Inspecting the mobile app on a desktop

```bash
npm run web --prefix mobile     # http://localhost:8081
```

Renders the Expo app through `react-native-web`. Useful for reviewing the camera and capture
screens without a device, and **not a deployment target** — two things genuinely do not exist in
a browser:

| | On a device | Under `expo start --web` |
| --- | --- | --- |
| Tokens | `expo-secure-store`, the OS keychain | `localStorage`, which is not secure |
| Upload queue | `expo-sqlite` + the filesystem | absent; the queue screen says so |

`expo-sqlite` throws from its *import* rather than its first call, so it is required lazily after
a platform check — a runtime guard alone still crashes the web bundle at load.

The API must allow `http://localhost:8081` in `CORS_ORIGINS`; `.env.example` already does.

## Configuration

Everything is an environment variable; `.env.example` documents the full set with comments. The
ones that change behaviour most:

| Variable | Default | Effect |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./var/dev.db` | Relative SQLite paths anchor to the repository root. |
| `QUEUE_BACKEND` | `thread` | `redis` for durable, multi-worker processing. |
| `STORAGE_BACKEND` | `local` | `s3` also needs `uv pip install boto3`. |
| `FRAME_INTERVAL_SECONDS` | `2.0` | Video → one frame every N seconds. |
| `MAPPING_CONFIDENCE_THRESHOLD` | `0.45` | Below this, an automatic path is refused. Raise to be stricter. |
| `MAX_UPLOAD_BYTES` | 2 GiB | Enforced while streaming, not from `Content-Length`. |
| `JWT_SECRET`, `STORAGE_SIGNING_SECRET` | dev placeholders | The API logs an error if these are still defaults with `ENVIRONMENT=production`. |

---

## Windows notes

Development for this project was done on Windows, so a few specifics:

- Use `backend\.venv\Scripts\python.exe` rather than activating the venv, if activation is
  awkward in your shell.
- PowerShell has no `&&`. Use `A; if ($?) { B }`, or run commands separately.
- `python` on a stock Windows install may be the Microsoft Store stub. `uv venv --python 3.12`
  downloads a real interpreter and sidesteps it.
- The temporary file used to hand a video path to OpenCV is closed before OpenCV opens it, because
  Windows will not allow a second open on a file still held by Python
  (`app/processing/stages.py::_staged_file`).

---

## Troubleshooting

**`SettingsError: error parsing value for field "cors_origins"`**
A list variable in `.env` was written as JSON when the loader expected CSV, or vice versa. Both
work — `a,b,c` or `["a","b"]` — but a malformed mixture does not.

**Login returns 401 with correct credentials**
The seeded accounts use `@demo.example.com`. Addresses on reserved TLDs like `.local` are
rejected by email validation, which is how the original seed addresses were caught.

**Uploads succeed but processing fails at `frame_extraction`**
Usually a truncated or non-decodable file. Open the capture detail page: the failing stage names
the reason, and the retry button re-runs from that stage.

**Panoramas score `poor` on quality**
Probably genuinely low resolution. The panorama floor is 1024 px wide and the target is 4096 px;
the warning states both.

**The 360 viewer is blank**
Check the browser console for a texture load failure. Signed asset URLs expire
(`STORAGE_URL_TTL_SECONDS`, default one hour); reloading the page mints new ones.

**Mapping always reports low confidence**
Frames are probably too far apart. Below roughly 2 m of spacing the geometry has enough overlap;
at 4 m it does not. Use a shorter video frame interval or capture more positions.

**`alembic upgrade` seems to do nothing**
You may be looking at a different database file than the API. Print what the app resolves:
`python -c "from app.core.config import settings; print(settings.database_url)"`.
