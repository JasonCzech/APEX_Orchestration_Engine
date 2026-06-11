"""Async repository for new-test wizard drafts (server-side so drafts roam)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import Draft


class DraftsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self, *, project_id: str | None = None) -> list[Draft]:
        """All drafts, optionally narrowed to one project (identity scoping is the
        router's job — this is a plain storage filter)."""
        stmt = select(Draft).order_by(Draft.updated_at.desc(), Draft.id)
        if project_id is not None:
            stmt = stmt.where(Draft.project_id == project_id)
        result = await self._session.scalars(stmt)
        return list(result)

    async def get(self, draft_id: str) -> Draft | None:
        return await self._session.get(Draft, draft_id)

    async def create(
        self,
        *,
        title: str,
        project_id: str | None,
        payload: dict[str, Any],
        created_by: str | None,
    ) -> Draft:
        draft = Draft(title=title, project_id=project_id, payload=payload, created_by=created_by)
        self._session.add(draft)
        await self._session.commit()
        await self._session.refresh(draft)
        return draft

    async def replace(self, draft_id: str, *, title: str, payload: dict[str, Any]) -> Draft | None:
        """Full replace of the editable fields; bumps updated_at explicitly."""
        draft = await self.get(draft_id)
        if draft is None:
            return None
        draft.title = title
        draft.payload = payload
        draft.updated_at = datetime.now(UTC)
        await self._session.commit()
        await self._session.refresh(draft)
        return draft

    async def delete(self, draft_id: str) -> bool:
        draft = await self.get(draft_id)
        if draft is None:
            return False
        await self._session.delete(draft)
        await self._session.commit()
        return True
