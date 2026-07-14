"""Best-effort recorder for the engine_runs history projection.

Called from the execution phase's engine nodes. Checkpointed graph state remains the
source of truth — projection writes must never fail a pipeline run, so every DB error
is swallowed and logged. Upsert key: (thread_id, attempt).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.persistence.models import EngineRun
from apex.ports.artifact_store import engine_artifact_namespace
from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings

logger = structlog.get_logger(__name__)

_TERMINAL = {"completed", "failed", "aborted"}


def _upsert_statement(values: dict[str, Any], dialect: str) -> Any:
    """Build a replay-safe upsert for the supported runtime/test databases."""
    if dialect == "postgresql":
        statement = pg_insert(EngineRun).values(**values)
    elif dialect == "sqlite":
        statement = sqlite_insert(EngineRun).values(**values)
    else:
        raise RuntimeError(f"engine-run upsert is unsupported for database dialect {dialect!r}")
    update_cols = {
        key: value for key, value in values.items() if key not in {"thread_id", "attempt"}
    }
    return statement.on_conflict_do_update(
        index_elements=[EngineRun.thread_id, EngineRun.attempt],
        set_=update_cols,
    )


async def record_engine_run(
    thread_id: str,
    attempt: int,
    engine: str,
    handle: dict[str, Any],
    status: str,
    *,
    project_id: str | None = None,
    app_id: str | None = None,
    external_run_id: str | None = None,
    artifact_namespace: str | None = None,
    artifact_connection_id: str | None = None,
    summary: dict[str, Any] | None = None,
) -> None:
    """Upsert one engine-run row; terminal statuses stamp ended_at. Never raises."""
    try:
        # Throwaway engine per call: graph nodes run on worker threads with
        # short-lived event loops, so pooled connections must not outlive them.
        database = get_settings().database
        engine_db = create_async_engine(
            database_asyncpg_uri(database.uri),
            poolclass=NullPool,
            connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
        )
        try:
            session_factory = async_sessionmaker(engine_db, expire_on_commit=False)
            async with session_factory() as session:
                values: dict[str, Any] = {
                    "thread_id": thread_id,
                    "attempt": attempt,
                    "engine": engine,
                    "handle": handle,
                    "status": status,
                    "ownership_known": True,
                }
                if project_id is not None:
                    values["project_id"] = project_id
                if app_id is not None:
                    values["app_id"] = app_id
                if external_run_id is not None:
                    values["external_run_id"] = external_run_id
                handle_idempotency_key = handle.get("idempotency_key")
                if artifact_namespace is None and handle_idempotency_key:
                    artifact_namespace = engine_artifact_namespace(str(handle_idempotency_key))
                if artifact_namespace is not None:
                    values["artifact_namespace"] = artifact_namespace.rstrip("/")
                if artifact_connection_id is not None:
                    values["artifact_connection_id"] = artifact_connection_id
                if summary is not None:
                    values["summary"] = summary
                if status in _TERMINAL:
                    values["ended_at"] = datetime.now(UTC)
                stmt = _upsert_statement(values, session.get_bind().dialect.name)
                await session.execute(stmt)
                await session.commit()
        finally:
            await engine_db.dispose()
    except Exception as exc:  # noqa: BLE001 — projection writes never fail a run
        logger.warning("engine_runs.record_failed", thread_id=thread_id, error=str(exc))


def record_engine_run_sync(*args: Any, **kwargs: Any) -> None:
    """Sync bridge that is safe when called from either sync or async graph nodes."""
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(record_engine_run(*args, **kwargs))
        else:
            loop.create_task(record_engine_run(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001
        logger.warning("engine_runs.record_failed", error=str(exc))
