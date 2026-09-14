"""Application settings.

Everything configurable is an environment variable so no secret is ever committed (§44).
See `.env.example` at the repository root for the documented set.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", Path.cwd() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- app -------------------------------------------------------------
    app_name: str = "SiteScope 360"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = True
    api_prefix: str = "/api"
    public_base_url: str = "http://localhost:8000"

    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
        ]
    )

    # ---- database --------------------------------------------------------
    # Postgres is the production target. A SQLite URL is accepted so the whole
    # platform can run on a laptop with no Docker (see docs/DEVELOPMENT.md).
    database_url: str = "sqlite:///./sitescope.db"
    db_echo: bool = False

    # ---- auth ------------------------------------------------------------
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60
    refresh_token_ttl_days: int = 14
    bcrypt_rounds: int = 12

    # ---- storage ---------------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    storage_root: Path = REPO_ROOT / "var" / "storage"
    storage_url_ttl_seconds: int = 3600
    storage_signing_secret: str = "dev-only-insecure-signing-secret"

    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    #: Serve asset URLs from a CDN instead of straight from the storage origin.
    #:
    #: Unset by default, because turning it on is a deployment claim rather than a preference:
    #: the CDN must already front the storage origin. Only the scheme, host and optional path
    #: prefix are rewritten -- the object path, the expiry and the signature carry through
    #: untouched, which is what lets one setting serve either provider:
    #:
    #: * ``local`` -- the signature is an HMAC over key and expiry and does not cover the host,
    #:   so any cache that forwards to the API validates it identically.
    #: * ``s3`` -- a SigV4 presigned URL *does* sign the ``Host`` header, so the distribution
    #:   must use the same S3 endpoint as its origin and forward the ``X-Amz-*`` query string
    #:   unmodified. It then sends the origin host upstream and the signature still verifies.
    #:   Pointing this at a distribution with a different origin breaks every asset, which is
    #:   why it is never inferred.
    cdn_base_url: str | None = None

    # ---- uploads ---------------------------------------------------------
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB
    allowed_image_extensions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [".jpg", ".jpeg", ".png"]
    )
    allowed_video_extensions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [".mp4", ".mov"]
    )
    allowed_floorplan_extensions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [".png", ".jpg", ".jpeg", ".pdf"]
    )

    # ---- queue -----------------------------------------------------------
    queue_backend: Literal["thread", "redis"] = "thread"
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "sitescope"
    thread_queue_workers: int = 2

    # ---- processing ------------------------------------------------------
    #: Seconds between extracted video frames.
    #:
    #: 1.0 rather than 2.0, measured. A person walks about 1.2 m/s, so a 2 s interval puts 2.4 m
    #: between consecutive frames — far enough that ORB finds only ~38 verified matches per pair
    #: and loop closure detects no revisits at all. On three synthetic routes the mean track
    #: error was 3.64 m at 2.0 s and 0.62 m at 1.0 s, and 2.0 s was the worse choice on every
    #: individual route.
    #:
    #: Denser is not automatically better: 0.75 s and 0.5 s both measured *worse* again (2.87 m
    #: and 2.68 m on the closed circuit), because the chain grows faster than the per-step
    #: geometry improves. The useful band is roughly 1.0–1.5 s; below it the extra frames cost
    #: storage and accuracy alike.
    frame_interval_seconds: float = 1.0
    max_frames_per_video: int = 600
    thumbnail_width: int = 256
    preview_width: int = 1024
    orb_features: int = 1200
    # Below this mapping confidence we refuse to publish an automatic path (§14, §46).
    mapping_confidence_threshold: float = 0.45

    # ---- monitoring defaults --------------------------------------------
    default_expected_captures_per_week: int = 2

    @field_validator(
        "cors_origins",
        "allowed_image_extensions",
        "allowed_video_extensions",
        "allowed_floorplan_extensions",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            if v.strip().startswith("["):
                return v
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("database_url", mode="after")
    @classmethod
    def _anchor_sqlite_path(cls, value: str) -> str:
        """Resolve a relative SQLite path against the repository root, not the process CWD.

        Without this, ``sqlite:///./var/dev.db`` means a different file depending on where the
        command was run from — so ``alembic upgrade`` from ``backend/`` would migrate one
        database while ``uvicorn`` from the repository root served an empty one. That failure is
        silent and genuinely confusing, and it only affects the SQLite development path;
        PostgreSQL URLs have no such ambiguity and are left untouched.
        """
        if not value.startswith("sqlite"):
            return value
        prefix, _, path = value.partition(":///")
        if not path or path.startswith((":memory:", "/")) or ":" in path[:2]:
            return value  # in-memory, POSIX-absolute, or Windows-absolute — leave as given
        resolved = (REPO_ROOT / path.lstrip("./")).resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return f"{prefix}:///{resolved.as_posix()}"

    @field_validator("cdn_base_url", mode="after")
    @classmethod
    def _normalise_cdn_base(cls, value: str | None) -> str | None:
        """Reject a CDN base that cannot yield an absolute URL, and drop any trailing slash.

        An empty string means "unset" rather than "an empty base", because that is what an
        unfilled ``CDN_BASE_URL=`` line in a ``.env`` file produces; joining against it would
        otherwise give a host-relative URL, which the browser resolves and the mobile app -- with
        no origin of its own -- cannot. Validating at start-up rather than at the call site means
        a typo fails on boot instead of on the first image an operator opens.
        """
        if value is None or not value.strip():
            return None
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("CDN_BASE_URL must start with http:// or https://")
        return cleaned

    @field_validator("storage_root", mode="after")
    @classmethod
    def _anchor_storage_root(cls, value: Path) -> Path:
        """Same reasoning as the SQLite path: a relative storage root must not follow the CWD."""
        return value if value.is_absolute() else (REPO_ROOT / value).resolve()

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def allowed_capture_extensions(self) -> list[str]:
        return [*self.allowed_image_extensions, *self.allowed_video_extensions]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
