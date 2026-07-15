import asyncio
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from apex.persistence.audit_lock import AUDIT_CHAIN_LOCK_KEY
from apex.persistence.migration_lineage import (
    CREATE_LINEAGE_TABLE_SQL,
    INSERT_LINEAGE_SQL,
    LINEAGE_TABLE,
    packaged_revision_lineage,
)
from apex.persistence.models import Base
from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

APEX_SCHEMA = "apex"
AUDIT_LOCK_SQL = f"SELECT pg_advisory_xact_lock({AUDIT_CHAIN_LOCK_KEY})"
ENSURE_APEX_SCHEMA_SQL = f"""
DO $apex_schema$
BEGIN
    IF to_regnamespace('{APEX_SCHEMA}') IS NULL THEN
        EXECUTE 'CREATE SCHEMA {APEX_SCHEMA}';
    END IF;
END
$apex_schema$
""".strip()


def _database_uri() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database.uri


def _include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    """Only autogenerate against apex-schema objects (LangGraph owns the default schema)."""
    if type_ == "table" and name == LINEAGE_TABLE.rsplit(".", 1)[1]:
        # This is Alembic infrastructure, refreshed by env.py even when there
        # are no application revisions to apply, rather than an ORM-owned table.
        return False
    if type_ == "table":
        return obj.schema == APEX_SCHEMA
    return True


def _packaged_lineage_rows() -> list[dict[str, str]]:
    scripts = ScriptDirectory.from_config(config)
    return [
        {"revision_num": revision, "parent_revision_num": parent}
        for revision, parent in sorted(packaged_revision_lineage(scripts))
    ]


def _register_packaged_lineage(connection: Connection) -> None:
    """Persist immutable revision ancestry before Alembic changes the DB head."""

    connection.execute(text(CREATE_LINEAGE_TABLE_SQL))
    rows = _packaged_lineage_rows()
    if rows:
        connection.execute(text(INSERT_LINEAGE_SQL), rows)


def _emit_packaged_lineage() -> None:
    """Include the same provenance bootstrap in offline migration SQL."""

    context.execute(text(CREATE_LINEAGE_TABLE_SQL))
    for row in _packaged_lineage_rows():
        statement = text(INSERT_LINEAGE_SQL).bindparams(**row)
        context.execute(statement)


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        version_table_schema=APEX_SCHEMA,
        include_schemas=True,
        include_object=_include_object,
        compare_type=True,
        compare_server_default=True,
        literal_binds=connection is None,
    )


def run_migrations_offline() -> None:
    _configure(url=_database_uri())
    # PostgreSQL checks database-level CREATE even for ``CREATE SCHEMA IF NOT
    # EXISTS`` when the schema already exists.  The split migration role owns
    # ``apex`` but deliberately lacks that database-wide privilege, so branch
    # before executing CREATE.  Empty databases still fail closed unless the
    # migration principal is actually allowed to create the schema.
    context.execute(text(ENSURE_APEX_SCHEMA_SQL))
    with context.begin_transaction():
        # Match the runtime writer's lock order before any revision can acquire
        # an audit-table lock.  This is emitted into offline SQL as well so a
        # generated upgrade script has the same zero-downtime safety property.
        context.execute(text(AUDIT_LOCK_SQL))
        _emit_packaged_lineage()
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # The apex schema must exist before Alembic writes its version table into it.
    # Use a conditional block rather than CREATE SCHEMA IF NOT EXISTS so an
    # existing schema owner does not also need database-wide CREATE.
    connection.execute(text(ENSURE_APEX_SCHEMA_SQL))
    if connection.dialect.name == "postgresql":
        # Acquire before Alembic runs even its first revision.  In particular,
        # 0015 must never hold an audit-table lock while 0016 waits behind an
        # appender that acquired this advisory lock in the opposite order.
        connection.execute(text(AUDIT_LOCK_SQL))
        _register_packaged_lineage(connection)
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    uri = _database_uri()
    database = get_settings().database
    engine = create_async_engine(
        database_asyncpg_uri(uri),
        poolclass=pool.NullPool,
        connect_args=database_ssl_connect_args(uri, database.ssl_mode),
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
    finally:
        # Failed connects/migrations must release driver resources too. NullPool
        # avoids retained connections, but the async engine still owns teardown.
        await engine.dispose()


def run_migrations_with_supplied_connection(connection: Connection) -> None:
    """Run on a caller-owned connection that may hold deployment session locks."""

    do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    supplied_connection = config.attributes.get("connection")
    if isinstance(supplied_connection, Connection):
        run_migrations_with_supplied_connection(supplied_connection)
    else:
        asyncio.run(run_migrations_online())
