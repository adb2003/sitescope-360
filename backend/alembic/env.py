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


#: Objects PostGIS creates and owns. ``CREATE EXTENSION postgis`` installs ``spatial_ref_sys``
#: (a table of 8000-odd coordinate systems) plus the ``geometry_columns``/``geography_columns``
#: views. None of them appear in ``Base.metadata``, so autogenerate sees them as rows the models
#: no longer want and writes ``op.drop_table("spatial_ref_sys")`` into the next migration.
_POSTGIS_OWNED_TABLES = frozenset({"spatial_ref_sys", "geometry_columns", "geography_columns"})

#: Columns the database computes and the models deliberately do not map, keyed by table.
#:
#: ``capture_locations.geom`` is ``GENERATED ALWAYS AS`` the ``x``/``y`` floats beside it, so it
#: cannot drift from them and no application code writes it. Mapping it would mean a
#: PostgreSQL-only column type on a model that also has to compile against SQLite, which is the
#: reason it is not mapped -- and the reason autogenerate has to be told, or it proposes dropping
#: the column on every run.
_DATABASE_OWNED_COLUMNS = {"capture_locations": frozenset({"geom"})}

#: Indexes on those columns, which have to be named separately.
#:
#: An index is its own autogenerate object: excluding the column it covers does not exclude it,
#: and the GiST index over ``geom`` was reported as drift even after the column stopped being.
#: An index over a column the models cannot see is one the models can never declare, so it
#: belongs to the migration for the same reason the column does.
_DATABASE_OWNED_INDEXES = frozenset({"ix_capture_locations_geom"})


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Hide database-owned objects from autogenerate.

    Only ever *excludes*, and only things the migrations themselves created. Anything the models
    own still drifts loudly, which is what the CI drift check depends on.
    """
    if type_ == "table" and name in _POSTGIS_OWNED_TABLES:
        return False
    if type_ == "index" and name in _DATABASE_OWNED_INDEXES:
        return False
    if type_ == "column":
        table = getattr(obj, "table", None)
        owned = _DATABASE_OWNED_COLUMNS.get(getattr(table, "name", ""), frozenset())
        if name in owned:
            return False
    return True


def _narrow_search_path(connection: object) -> None:
    """Reflect only the schemas this project actually uses.

    The ``postgis/postgis`` image installs the TIGER geocoder and topology extensions alongside
    PostGIS and appends their schemas to the *database's* ``search_path``. Alembic reflects the
    search path, so autogenerate found ~40 census tables it had never heard of and proposed
    dropping every one of them — which is exactly how this failed the first time it ran against
    PostGIS instead of a stock image.

    Excluding them by name would be a list to maintain against extensions we do not use, so this
    narrows the path instead: PostgreSQL's own documented default, plus wherever PostGIS itself
    was installed. That last part is not decoration — the migration declares a ``geometry``
    column, and dropping the extension's schema off the path would make the type stop resolving
    and turn a reflection problem into a broken migration.

    A deployment that keeps its tables in some other schema sets ``search_path`` per-connection
    through ``DATABASE_URL``; SQLAlchemy applies that after this runs, so it still wins.
    """
    if not settings.database_url.startswith("postgresql"):
        return

    postgis_schema = connection.exec_driver_sql(  # type: ignore[attr-defined]
        "SELECT n.nspname FROM pg_extension e "
        "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'postgis'"
    ).scalar()

    schemas = ['"$user"', "public"]
    if postgis_schema and postgis_schema != "public":
        schemas.append(f'"{postgis_schema}"')
    connection.exec_driver_sql(f"SET search_path TO {', '.join(schemas)}")  # type: ignore[attr-defined]

    # Not redundant, and not safe to delete. Reading the catalogue above opened a transaction on
    # this connection, and SQLAlchemy 2.0 rolls back a connection that is closed without one --
    # so Alembic, which only commits the transaction *it* opened, would find one already in
    # progress and every migration would be discarded at close. The first time this ran, all
    # three migrations logged success and the database came out empty.
    #
    # Committing here hands the connection back with no transaction open, which is the state
    # Alembic expects. `SET` is session state and survives the commit.
    connection.commit()  # type: ignore[attr-defined]


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
        include_object=include_object,
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
        _narrow_search_path(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_item=render_item,
            include_object=include_object,
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
