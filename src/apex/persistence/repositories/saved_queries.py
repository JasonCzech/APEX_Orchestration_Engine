"""Async repository for saved work-tracking queries (JQL/WIQL snippets by name).

Duplicate (project_id, name) pairs violate the table's unique constraint; the
repository converts that IntegrityError into a ValueError so routers (and fake
repositories in tests) share one error contract: ValueError -> 409.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import SavedQuery

_MUTABLE_FIELDS = frozenset({"name", "provider", "query", "description", "project_id"})


class SavedQueriesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, row: SavedQuery) -> SavedQuery:
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ValueError(
                f"a saved query named {row.name!r} already exists for project {row.project_id!r}"
            ) from exc
        await self._session.refresh(row)
        return row

    async def get(self, saved_query_id: str) -> SavedQuery | None:
        return await self._session.get(SavedQuery, saved_query_id)

    async def list(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        allowed_project_ids: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SavedQuery]:
        """Name-ordered listing. `allowed_project_ids=None` means unrestricted
        (unscoped admin); otherwise rows must be global (project_id NULL) or
        belong to one of the allowed projects."""
        stmt = select(SavedQuery).order_by(SavedQuery.name, SavedQuery.id)
        if allowed_project_ids is not None:
            stmt = stmt.where(
                or_(
                    SavedQuery.project_id.is_(None),
                    SavedQuery.project_id.in_(list(allowed_project_ids)),
                )
            )
        if project is not None:
            stmt = stmt.where(SavedQuery.project_id == project)
        if provider is not None:
            stmt = stmt.where(SavedQuery.provider == provider)
        stmt = stmt.limit(limit).offset(offset)
        return list(await self._session.scalars(stmt))

    async def update(self, row: SavedQuery, changes: dict[str, Any]) -> SavedQuery:
        unknown = set(changes) - _MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"saved query fields not updatable: {sorted(unknown)}")
        for key, value in changes.items():
            setattr(row, key, value)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ValueError(
                f"a saved query named {row.name!r} already exists for project {row.project_id!r}"
            ) from exc
        await self._session.refresh(row)
        return row

    async def delete(self, row: SavedQuery) -> None:
        await self._session.delete(row)
        await self._session.commit()
