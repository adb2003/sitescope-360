# SiteScope 360 backend — the API and the worker share this image (§38).
#
# Multi-stage: the builder resolves dependencies into a virtualenv; the runtime carries only that
# virtualenv plus the application. The vision stack (numpy, OpenCV, Pillow) is most of the image
# size, so keeping compilers and build tooling out of the final layer is worth the extra stage.

FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv==0.5.11

WORKDIR /build

# Dependencies are resolved and installed from the manifest alone, before any source is copied,
# so editing a Python file does not invalidate the (slow, large) dependency layer.
COPY pyproject.toml ./
RUN uv venv /opt/venv \
    && uv pip compile pyproject.toml --output-file /tmp/requirements.txt \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --requirement /tmp/requirements.txt

# Now the source. Installed with --no-deps because everything is already present above.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-deps .

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
