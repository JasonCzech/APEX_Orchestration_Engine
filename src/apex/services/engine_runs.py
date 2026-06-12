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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.persistence.models import EngineRun
from apex.settings import get_settings

logger = structlog.get_logger(__name__)

_TERMINAL = {"completed", "failed", "aborted"}


async def record_engine_run(
    thread_id: str,
    attempt: int,
    engine: str,
    handle: dict[str, Any],
    status: str,
    *,
    external_run_id: str | None = None,
    summary: dict[str, Any] | None = None,
) -> None:
    """Upsert one engine-run row; terminal statuses stamp ended_at. Never raises."""
    try:
        # Throwaway engine per call: graph nodes run on worker threads with
        # short-lived event loops, so pooled connections must not outlive them.
        engine_db = create_async_engine(get_settings().database.uri, poolclass=NullPool)
        try:
            session_factory = async_sessionmaker(engine_db, expire_on_commit=False)
            async with session_factory() as session:
                values: dict[str, Any] = {
                    "thread_id": thread_id,
                    "attempt": attempt,
                    "engine": engine,
                    "handle": handle,
                    "status": status,
                    "external_run_id": external_run_id,
                }
                if summary is not None:
                    values["summary"] = summary
                if status in _TERMINAL:
                    values["ended_at"] = datetime.now(UTC)
                stmt = pg_insert(EngineRun).values(**values)
                update_cols = {k: v for k, v in values.items() if k not in ("thread_id", "attempt")}
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_engine_runs_thread_id", set_=update_cols
                )
                await session.execute(stmt)
                await session.commit()
        finally:
            await engine_db.dispose()
    except Exception as exc:  # noqa: BLE001 — projection writes never fail a run
        logger.warning("engine_runs.record_failed", thread_id=thread_id, error=str(exc))


def record_engine_run_sync(*args: Any, **kwargs: Any) -> None:
    """Sync bridge for graph nodes (which run sync on worker threads)."""
    try:
        asyncio.run(record_engine_run(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001
        logger.warning("engine_runs.record_failed", error=str(exc))
