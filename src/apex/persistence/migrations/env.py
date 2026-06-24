import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from apex.persistence.models import Base
from apex.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

APEX_SCHEMA = "apex"


def _database_uri() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database.uri


def _include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    """Only autogenerate against apex-schema objects (LangGraph owns the default schema)."""
    if type_ == "table":
        return obj.schema == APEX_SCHEMA
    return True


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        version_table_schema=APEX_SCHEMA,
        include_schemas=True,
        include_object=_include_object,
        compare_type=True,
        literal_binds=connection is None,
    )


def run_migrations_offline() -> None:
    _configure(url=_database_uri())
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # The apex schema must exist before Alembic writes its version table into it.
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {APEX_SCHEMA}"))
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_uri(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
