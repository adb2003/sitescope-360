# Open-source dependencies and licences

SiteScope 360 is licensed **Apache-2.0**.

Every dependency below is permissively licensed (MIT, BSD, Apache-2.0, ISC, PSF, HPND or
Unlicense). There are no copyleft (GPL/LGPL/AGPL) dependencies, and no commercial or
source-available licences.

> **No proprietary construction-monitoring dependency.** Nothing here is an OpenSpace SDK, client,
> wrapper or scraper — nor any other construction-monitoring vendor's. The list is exhaustive for
> direct dependencies; versions are those actually installed and verified while building this.

---

## Backend (Python)

### Runtime

| Package | Version | Licence | Used for |
| --- | --- | --- | --- |
| [fastapi](https://github.com/fastapi/fastapi) | 0.141.1 | MIT | HTTP framework, OpenAPI generation |
| [starlette](https://github.com/encode/starlette) | 1.6.0 | BSD-3-Clause | ASGI toolkit under FastAPI |
| [uvicorn](https://github.com/encode/uvicorn) | 0.52.4 | BSD-3-Clause | ASGI server |
| [pydantic](https://github.com/pydantic/pydantic) | 2.13.5 | MIT | Request/response validation |
| [pydantic-settings](https://github.com/pydantic/pydantic-settings) | 2.15.0 | MIT | Environment configuration |
| [email-validator](https://github.com/JoshData/python-email-validator) | 2.3.0 | Unlicense | Email format validation |
| [python-multipart](https://github.com/Kludex/python-multipart) | 0.0.32 | Apache-2.0 | Multipart upload parsing |
| [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) | 2.0.52 | MIT | ORM and query construction |
| [alembic](https://github.com/sqlalchemy/alembic) | 1.19.2 | MIT | Database migrations |
| [psycopg](https://github.com/psycopg/psycopg) | 3.3.5 | LGPL-3.0-or-later¹ | PostgreSQL driver |
| [bcrypt](https://github.com/pyca/bcrypt) | 4.3.0 | Apache-2.0 | Password hashing |
| [PyJWT](https://github.com/jpadilla/pyjwt) | 2.13.0 | MIT | JWT signing and verification |
| [redis](https://github.com/redis/redis-py) | 8.1.0 | MIT | Redis client (queue backend) |
| [rq](https://github.com/rq/rq) | 2.12.0 | BSD-3-Clause | Job queue |
| [numpy](https://github.com/numpy/numpy) | 2.5.3 | BSD-3-Clause | Vision maths, array operations |
| [opencv-python-headless](https://github.com/opencv/opencv-python) | 5.0.0.93 | Apache-2.0² | ORB features, image/video I/O |
| [Pillow](https://github.com/python-pillow/Pillow) | 12.3.0 | MIT-CMU (HPND) | EXIF extraction, floor-plan rendering |
| [reportlab](https://github.com/MrBitBucketer/reportlab) | 5.0.1 | BSD-3-Clause | PDF report generation |
| [httpx](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause | HTTP client for camera adapters |

¹ **psycopg 3 is LGPL-3.0-or-later.** It is used as an unmodified, dynamically imported library —
which LGPL explicitly permits without affecting this project's licensing. It is also the *only*
non-permissive dependency and is needed **only for PostgreSQL**: the platform runs on SQLite with
`psycopg` uninstalled. If your policy forbids LGPL entirely, remove it from
`backend/pyproject.toml` and use SQLite, or substitute a permissive driver.

² OpenCV's Python wheels are Apache-2.0 from version 4.5 onward (earlier releases were
BSD-3-Clause). The **headless** build is used deliberately: it excludes the GUI/Qt components, so
no additional licence obligations and a much smaller container image. It also means the vision
engine cannot accidentally try to open a window on a server.

### Development only — not shipped

| Package | Version | Licence | Used for |
| --- | --- | --- | --- |
| [pytest](https://github.com/pytest-dev/pytest) | 9.1.1 | MIT | Test runner |
| [pytest-cov](https://github.com/pytest-dev/pytest-cov) | 7.1.0 | MIT | Coverage |
| [ruff](https://github.com/astral-sh/ruff) | 0.16.6 | MIT | Lint and format |
| [mypy](https://github.com/python/mypy) | 2.3.1 | MIT | Static type checking |
| [uv](https://github.com/astral-sh/uv) | 0.5+ | Apache-2.0 / MIT | Dependency management |

Python itself is under the **PSF License** (BSD-compatible).

---

## Frontend (TypeScript)

### Runtime

| Package | Version | Licence | Used for |
| --- | --- | --- | --- |
| [react](https://github.com/facebook/react) | 18.3.1 | MIT | UI framework |
| [react-dom](https://github.com/facebook/react) | 18.3.1 | MIT | DOM renderer |
| [react-router-dom](https://github.com/remix-run/react-router) | 6.30.6 | MIT | Routing |
| [@tanstack/react-query](https://github.com/TanStack/query) | 5.102.8 | MIT | Server state, caching, polling |
| [three](https://github.com/mrdoob/three.js) | 0.171.0 | MIT | The 360 panorama viewer (WebGL) |
| [lucide-react](https://github.com/lucide-icons/lucide) | 0.468.0 | ISC | Icons |
| [clsx](https://github.com/lukeed/clsx) | 2.1.1 | MIT | Conditional class names |
| [tailwind-merge](https://github.com/dcastil/tailwind-merge) | 2.6.1 | MIT | Tailwind class conflict resolution |
| [class-variance-authority](https://github.com/joe-bell/cva) | 0.7.1 | Apache-2.0 | Component variants |

### Build and development only — not shipped

| Package | Version | Licence |
| --- | --- | --- |
| [vite](https://github.com/vitejs/vite) | 6.4.3 | MIT |
| [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react) | 4.x | MIT |
| [typescript](https://github.com/microsoft/TypeScript) | 5.9.3 | Apache-2.0 |
| [tailwindcss](https://github.com/tailwindlabs/tailwindcss) | 3.4.19 | MIT |
| [postcss](https://github.com/postcss/postcss) | 8.5.28 | MIT |
| [autoprefixer](https://github.com/postcss/autoprefixer) | 10.5.5 | MIT |
| [vitest](https://github.com/vitest-dev/vitest) | 2.1.9 | MIT |
| [jsdom](https://github.com/jsdom/jsdom) | 25.0.1 | MIT |
| [@testing-library/react](https://github.com/testing-library/react-testing-library) | 16.3.3 | MIT |
| [@testing-library/jest-dom](https://github.com/testing-library/jest-dom) | 6.x | MIT |
| [@testing-library/user-event](https://github.com/testing-library/user-event) | 14.x | MIT |
| [eslint](https://github.com/eslint/eslint) | 9.39.5 | MIT |
| [typescript-eslint](https://github.com/typescript-eslint/typescript-eslint) | 8.x | MIT |
| [eslint-plugin-react-hooks](https://github.com/facebook/react) | 5.x | MIT |
| [globals](https://github.com/sindresorhus/globals) | 15.x | MIT |
| [@types/*](https://github.com/DefinitelyTyped/DefinitelyTyped) | — | MIT |

### shadcn/ui

The UI primitives in `frontend/src/components/ui/index.tsx` are **written by hand in the
shadcn/ui style** (Tailwind + `class-variance-authority`), not copied from the project.
shadcn/ui is MIT-licensed and its model is that you own the component source; vendoring only the
handful this app needs keeps the dependency surface to `cva` + `tailwind-merge` instead of
inheriting a generator and forty files.

### Panorama viewer

Three.js is used **directly** rather than via [Pannellum](https://github.com/mpetroff/pannellum)
(MIT) or [Marzipano](https://github.com/google/marzipano) (Apache-2.0), both of which were
considered. The reason is functional, not licensing: this viewer projects annotation pins and
neighbour hotspots into screen space, synchronises orientation with a second viewer for date
comparison, and reports the yaw of a click so a finding can be pinned. All three need the camera
matrix, which a wrapped viewer hides.

---

## Mobile (Expo / React Native)

| Package | Licence | Used for |
| --- | --- | --- |
| [expo](https://github.com/expo/expo) | MIT | App framework |
| [react-native](https://github.com/facebook/react-native) | MIT | Native runtime |
| [expo-sqlite](https://github.com/expo/expo) | MIT | Durable offline capture queue |
| [expo-file-system](https://github.com/expo/expo) | MIT | Holding capture files until upload |
| [expo-secure-store](https://github.com/expo/expo) | MIT | Tokens in the OS keychain |
| [expo-image-picker](https://github.com/expo/expo) | MIT | Importing from the photo library |
| [expo-document-picker](https://github.com/expo/expo) | MIT | Importing 360 files |
| [expo-location](https://github.com/expo/expo) | MIT | Optional GPS on a capture session |
| [expo-network](https://github.com/expo/expo) | MIT | Connectivity detection for queue drain |
| [expo-constants](https://github.com/expo/expo) | MIT | Build-time API address |
| [expo-status-bar](https://github.com/expo/expo) | MIT | Status bar styling |
| [react-native-safe-area-context](https://github.com/th3rdwave/react-native-safe-area-context) | MIT | Safe-area insets |

---

## Infrastructure

| Image | Licence | Used for |
| --- | --- | --- |
| [postgres:17-alpine](https://www.postgresql.org/about/licence/) | PostgreSQL License (BSD-like) | Database |
| [redis:7-alpine](https://github.com/redis/redis) | RSALv2 / SSPLv1³ | Job queue |
| [nginx:1.27-alpine](https://nginx.org/LICENSE) | BSD-2-Clause | Reverse proxy, static assets |
| [python:3.12-slim-bookworm](https://hub.docker.com/_/python) | PSF + Debian | Backend base image |
| [node:22-alpine](https://hub.docker.com/_/node) | MIT + Alpine | Frontend build image |

³ **Redis 7.4+ is dual-licensed RSALv2/SSPLv1**, which is not OSI-approved. It is used as an
**unmodified, network-attached service**, not linked into or redistributed with this code — the
licence restricts offering Redis itself as a managed service, which this does not. Two
alternatives if that is unacceptable in your context:

- Pin `redis:7.2-alpine` (BSD-3-Clause), which is fully compatible with the RQ backend, **or**
- Set `QUEUE_BACKEND=thread` and run no Redis at all. The `JobQueue` abstraction means the same
  handlers run either way; you give up durability across restarts. See
  [ARCHITECTURE.md](ARCHITECTURE.md#jobqueue-abstraction).

The `docker-compose.yml` in this repository pins `redis:7-alpine`. Change that one line if your
policy requires it.

---

## Data and assets

| | |
| --- | --- |
| **Demo panoramas** | Generated by `backend/app/processing/synthetic.py`. Every pixel is produced by that file — a ray-cast 3D room. No third-party imagery is downloaded, bundled or referenced. |
| **Demo floor plans** | Generated by `backend/app/processing/floorplan_render.py` using Pillow. |
| **Icons** | Lucide (ISC). |
| **Fonts** | None bundled. The UI uses the system font stack, so nothing is fetched from a font CDN and no font licence applies. |

Generated rather than downloaded for two reasons: the licensing question disappears entirely, and
the imagery has *real geometry*, so the vision engine's output can be checked against the camera
path that produced it. See [VISION_PIPELINE.md §9](VISION_PIPELINE.md#9-demo-and-test-imagery).

---

## Algorithms

The computer vision is implemented from published, unencumbered methods — no proprietary
algorithm is used or reimplemented:

| Method | Source | Patent status |
| --- | --- | --- |
| ORB features | Rublee et al., ICCV 2011 | Free — created explicitly as an unencumbered SIFT/SURF alternative |
| Lowe's ratio test | Lowe, IJCV 2004 | The *test* is a threshold heuristic and unencumbered. SIFT's own patent expired in 2020; SIFT is not used here. |
| Essential matrix / 8-point algorithm | Longuet-Higgins 1981; Hartley 1997 | Textbook, unencumbered |
| RANSAC | Fischler & Bolles 1981 | Textbook, unencumbered |
| Orthogonal Procrustes rotation fit | Schönemann 1966 | Textbook, unencumbered |
| Umeyama similarity transform | Umeyama, PAMI 1991 | Textbook, unencumbered |
| Variance of Laplacian (blur) | Pech-Pacheco et al., ICPR 2000 | Textbook, unencumbered |
| dHash perceptual hash | Krawetz, published method | Unencumbered |

SIFT and SURF are deliberately **not** used. SIFT's patent expired in March 2020 and OpenCV now
ships it in the main module, but ORB is faster, adequate for this task, and avoids the question
entirely for anyone building on an older OpenCV.

---

## Verifying this list

```bash
# Backend
cd backend
uv pip list
uv pip install pip-licenses && python -m piplicenses --format=markdown

# Frontend
cd frontend
npm ls --depth=0
npx license-checker --summary        # includes transitive dependencies
```

Direct dependencies are declared in `backend/pyproject.toml` and `frontend/package.json`.
