"""Async repository for the `engine_runs` history projection (M3).

Read side of the projection that the execution phase upserts best-effort (see
apex.services.engine_runs) — dashboard history queries plus the abort path's
direct status update. No project column exists in v1: rows key on (thread_id,
attempt) only, so caller-side scoping is role-based in the router.
"""

from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import EngineRun

TERMINAL_STATUSES = ("completed", "failed", "aborted")


class EngineRunsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_runs(
        self,
        *,
        engine: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EngineRun], int]:
        """One page of runs, newest started first, plus the unpaged filtered total.

        (Named list_runs, not list: a method named `list` would shadow the builtin
        in the class scope used by later methods' annotations.)
        """
        filters: list[ColumnElement[bool]] = []
        if engine is not None:
            filters.append(EngineRun.engine == engine)
        if status is not None:
            filters.append(EngineRun.status == status)
        stmt = (
            select(EngineRun)
            .where(*filters)
            .order_by(EngineRun.started_at.desc(), EngineRun.id)
            .limit(limit)
            .offset(offset)
        )
        rows = list(await self._session.scalars(stmt))
        total = await self._session.scalar(
            select(func.count()).select_from(EngineRun).where(*filters)
        )
        return rows, int(total or 0)

    async def list_for_thread(self, thread_id: str) -> list[EngineRun]:
        """All attempts for one thread, newest attempt first."""
        stmt = (
            select(EngineRun)
            .where(EngineRun.thread_id == thread_id)
            .order_by(EngineRun.attempt.desc())
        )
        return list(await self._session.scalars(stmt))

    async def get_latest_for_thread(self, thread_id: str) -> EngineRun | None:
        rows = await self.list_for_thread(thread_id)
        return rows[0] if rows else None

    async def mark_aborted(self, thread_id: str) -> int:
        """Flip every non-terminal row for the thread to "aborted" (+ ended_at).

        Returns the number of rows updated (0 when the projection already shows a
        terminal status — the abort endpoint treats that as fine, not an error).
        """
        stmt = (
            update(EngineRun)
            .where(
                EngineRun.thread_id == thread_id,
                EngineRun.status.not_in(TERMINAL_STATUSES),
            )
            .values(status="aborted", ended_at=datetime.now(UTC))
        )
        result = await self._session.execute(stmt)
        await self._session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
