"""Alembic environment.

The database URL comes from ``DATABASE_URL`` via application settings, so migrations always run
against the same database the application uses and no credential lives in ``alembic.ini``.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings

# Importing the models package registers every mapper on Base.metadata, which is what
# autogenerate compares against.
from app.models import Base  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render the project's custom column types as plain SQLAlchemy.

    Without this, autogenerate emits ``app.database.base.UTCDateTime(...)`` into the migration,
    which makes every migration depend on live application code — so refactoring or deleting a
    type later breaks the ability to replay history. Rendering the equivalent SQLAlchemy
    expression keeps migrations self-contained *and* dialect-correct:

    * ``UTCDateTime`` → ``sa.DateTime(timezone=True)`` (identical DDL).
    * ``JSONType``    → ``sa.JSON().with_variant(postgresql.JSONB(), "postgresql")``, which is
      exactly the JSONB-on-PostgreSQL / JSON-elsewhere behaviour the type implements.
    """
    if type_ != "type":
        return False

    name = obj.__class__.__name__
    if name == "UTCDateTime":
        return "sa.DateTime(timezone=True)"
    if name == "EnumType":
        # Persisted as a plain VARCHAR; the enum class lives in application code and must not
        # be referenced from a migration.
        return f"sa.String(length={obj.length})"
    if name == "JSONType":
        autogen_context.imports.add(  # type: ignore[attr-defined]
            "from sqlalchemy.dialects import postgresql"
        )
        return 'sa.JSON().with_variant(postgresql.JSONB(), "postgresql")'
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
        # SQLite cannot ALTER most columns; batch mode rewrites the table instead, which keeps
        # the same migration scripts working on both backends.
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = settings.database_url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_item=render_item,
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
