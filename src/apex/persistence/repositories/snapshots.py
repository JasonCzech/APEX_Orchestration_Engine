"""Async repository for bounded environment inventory snapshot history."""

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apex.persistence.models import Environment, EnvironmentSnapshot

SNAPSHOT_HISTORY_LIMIT = 32


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
        return await self._session.scalar(_latest_snapshot_statement(environment_id))

    async def add(
        self, environment_id: str, *, data: dict[str, Any], scanned_at: datetime
    ) -> EnvironmentSnapshot:
        """Insert a snapshot and retain only the newest bounded history."""

        # Serialize inserts/pruning per environment. Without this lock,
        # concurrent rescans can each prune against a stale visible set and let
        # the append-only JSON history grow beyond its configured bound.
        locked_environment = await self._session.scalar(
            select(Environment.id).where(Environment.id == environment_id).with_for_update()
        )
        if locked_environment is None:
            raise ValueError(f"environment {environment_id!r} does not exist")
        row = EnvironmentSnapshot(environment_id=environment_id, scanned_at=scanned_at, data=data)
        self._session.add(row)
        await self._session.flush()
        await self._session.execute(_expired_snapshots_statement(environment_id))
        # A provider can report an out-of-order scan time. In that case the new
        # row may itself be pruned; return the actual retained latest snapshot
        # instead of refreshing a deleted ORM instance after commit.
        retained_latest = await self._session.scalar(_latest_snapshot_statement(environment_id))
        if retained_latest is None:
            raise RuntimeError("snapshot retention removed every environment snapshot")
        await self._session.commit()
        return retained_latest


def _latest_snapshot_statement(environment_id: str):  # noqa: ANN202
    return (
        select(EnvironmentSnapshot)
        .where(EnvironmentSnapshot.environment_id == environment_id)
        .order_by(EnvironmentSnapshot.scanned_at.desc(), EnvironmentSnapshot.id.desc())
        .limit(1)
    )


def _expired_snapshots_statement(environment_id: str):  # noqa: ANN202
    expired_ids = (
        select(EnvironmentSnapshot.id)
        .where(EnvironmentSnapshot.environment_id == environment_id)
        .order_by(EnvironmentSnapshot.scanned_at.desc(), EnvironmentSnapshot.id.desc())
        .offset(SNAPSHOT_HISTORY_LIMIT)
    )
    return delete(EnvironmentSnapshot).where(EnvironmentSnapshot.id.in_(expired_ids))
