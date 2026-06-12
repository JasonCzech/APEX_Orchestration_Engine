"""Async repository for environment_snapshots (+ the environment lookup the
inventory surface needs for project-scope checks).

Snapshot rows are append-only: every rescan inserts a NEW row, and "current
inventory" is simply the latest row by scanned_at — history stays queryable.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apex.persistence.models import Environment, EnvironmentSnapshot


class SnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_environment(self, environment_id: str) -> Environment | None:
        """Loaded with .application so routers can scope-check via project_id."""
        stmt = (
            select(Environment)
            .options(selectinload(Environment.application))
            .where(Environment.id == environment_id)
        )
        return await self._session.scalar(stmt)

    async def latest(self, environment_id: str) -> EnvironmentSnapshot | None:
        stmt = (
            select(EnvironmentSnapshot)
            .where(EnvironmentSnapshot.environment_id == environment_id)
            .order_by(EnvironmentSnapshot.scanned_at.desc())
            .limit(1)
        )
        return await self._session.scalar(stmt)

    async def add(
        self, environment_id: str, *, data: dict[str, Any], scanned_at: datetime
    ) -> EnvironmentSnapshot:
        """Insert one new snapshot row (append-only; never updates prior scans)."""
        row = EnvironmentSnapshot(environment_id=environment_id, scanned_at=scanned_at, data=data)
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row
