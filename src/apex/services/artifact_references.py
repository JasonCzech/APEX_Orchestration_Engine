"""Required durable index writes for checkpoint-addressed artifacts."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.persistence.models import ArtifactReference, Connection
from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings


async def record_artifact_reference(
    *,
    artifact_key: str,
    connection_id: str,
    kind: str,
    thread_id: str,
    project_id: str | None,
    app_id: str | None,
) -> None:
    settings = get_settings()
    if connection_id.startswith("dev-") and not settings.is_locked_down:
        # Static development adapters have no persisted Connection row to protect.
        return
    database = settings.database
    engine = create_async_engine(
        database_asyncpg_uri(database.uri),
        poolclass=NullPool,
        connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
    )
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            connection = await session.scalar(
                select(Connection).where(Connection.id == connection_id).with_for_update()
            )
            if connection is None or not connection.enabled or connection.kind != "artifact_store":
                raise RuntimeError("artifact-store connection is missing or disabled")
            values: dict[str, Any] = {
                "artifact_key": artifact_key,
                "connection_id": connection_id,
                "kind": kind,
                "thread_id": thread_id,
                "project_id": project_id,
                "app_id": app_id,
            }
            if session.get_bind().dialect.name == "postgresql":
                statement = pg_insert(ArtifactReference).values(**values)
            elif session.get_bind().dialect.name == "sqlite":
                statement = sqlite_insert(ArtifactReference).values(**values)
            else:
                raise RuntimeError("artifact-reference upsert requires PostgreSQL or SQLite")
            statement = statement.on_conflict_do_update(
                index_elements=[ArtifactReference.artifact_key],
                set_={key: value for key, value in values.items() if key != "artifact_key"},
            )
            await session.execute(statement)
            await session.commit()
    finally:
        await engine.dispose()
