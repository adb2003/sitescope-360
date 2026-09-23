# SiteScope 360 backend — the API and the worker share this image (§38).
#
# Multi-stage: the builder resolves dependencies into a virtualenv; the runtime carries only that
# virtualenv plus the application. The vision stack (numpy, OpenCV, Pillow) is most of the image
# size, so keeping compilers and build tooling out of the final layer is worth the extra stage.

FROM python:3.12-slim-bookworm AS builder

# UV_PROJECT_ENVIRONMENT points uv at /opt/venv instead of a project-local .venv, so the runtime
# stage still has exactly one directory to copy across.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN pip install --no-cache-dir uv==0.5.11

WORKDIR /build

# Installed from `uv.lock`, not resolved here. This used to run `uv pip compile pyproject.toml`,
# which resolved afresh at build time: the image could ship different versions from the ones CI
# had just tested, and two builds of the same commit could differ from each other. CI installs
# the same lock with the same `--locked`, so the tested environment and the shipped one are now
# the same set of pins.
#
# `--locked` asserts the lock is consistent with pyproject rather than quietly updating it. A
# container build is the wrong place to discover that a dependency was added and never locked,
# and the wrong place to fix it.
#
# Still before any source is copied, so editing a Python file does not invalidate the (slow,
# large) dependency layer — `--no-install-project` is what keeps that split.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

# Now the source. The project itself installs here, against dependencies already present above.
#
# `--no-editable` matters in a container and nowhere else. `uv sync` installs the project editable
# by default, which writes a `.pth` file pointing at this builder's /build — a directory that does
# not exist in the runtime stage, because only /opt/venv is copied across. The result still
# *appears* to work, since the runtime copies the source to /app and imports resolve from the
# working directory; it breaks the moment anything runs from elsewhere. A non-editable install
# puts the code in site-packages, which is what the previous `uv pip install .` produced and what
# survives the stage boundary.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --locked --no-dev --no-editable

# ---- runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# opencv-python-headless still links a couple of system libraries for image/video codecs.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runs as a non-root user; the only writable path is the storage volume (§44).
RUN useradd --create-home --uid 10001 sitescope

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=sitescope:sitescope app ./app
COPY --chown=sitescope:sitescope alembic ./alembic
COPY --chown=sitescope:sitescope alembic.ini pyproject.toml ./

RUN mkdir -p /var/lib/sitescope/storage && chown -R sitescope:sitescope /var/lib/sitescope

USER sitescope
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

# Overridden in docker-compose.yml: the API runs uvicorn, the worker runs app.workers.run.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
