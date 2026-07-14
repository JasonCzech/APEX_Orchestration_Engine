"""Async repository for new-test wizard drafts (server-side so drafts roam)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.auth.identity import ScopeRef
from apex.persistence.models import Draft


class DraftsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(
        self,
        *,
        project_id: str | None = None,
        allowed_scopes: tuple[ScopeRef, ...] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Draft]:
        """All drafts, optionally narrowed to one project (identity scoping is the
        router's job — this is a plain storage filter)."""
        stmt = select(Draft).order_by(Draft.updated_at.desc(), Draft.id)
        if allowed_scopes is not None:
            project_wide = sorted(
                {scope.project_id for scope in allowed_scopes if scope.app_id is None}
            )
            visibility = [Draft.project_id.is_(None)]
            if project_wide:
                visibility.append(Draft.project_id.in_(project_wide))
            stmt = stmt.where(or_(*visibility))
        if project_id is not None:
            stmt = stmt.where(Draft.project_id == project_id)
        stmt = stmt.limit(limit).offset(offset)
        result = await self._session.scalars(stmt)
        return list(result)

    async def get(self, draft_id: str) -> Draft | None:
        return await self._session.get(Draft, draft_id)

    async def get_for_update(self, draft_id: str) -> Draft | None:
        return await self._session.scalar(
            select(Draft)
            .where(Draft.id == draft_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )

    async def create(
        self,
        *,
        title: str,
        project_id: str | None,
        payload: dict[str, Any],
        created_by: str | None,
        created_by_consumer_id: str | None = None,
    ) -> Draft:
        draft = Draft(
            title=title,
            project_id=project_id,
            payload=payload,
            created_by=created_by,
            created_by_consumer_id=created_by_consumer_id,
        )
        self._session.add(draft)
        await self._session.commit()
        await self._session.refresh(draft)
        return draft

    async def replace(
        self,
        draft_id: str,
        *,
        title: str,
        project_id: str | None,
        payload: dict[str, Any],
    ) -> Draft | None:
        """Full replace of the editable fields; bumps updated_at explicitly."""
        draft = await self.get_for_update(draft_id)
        if draft is None:
            return None
        return await self.replace_existing(
            draft,
            title=title,
            project_id=project_id,
            payload=payload,
        )

    async def replace_existing(
        self,
        draft: Draft,
        *,
        title: str,
        project_id: str | None,
        payload: dict[str, Any],
    ) -> Draft:
        """Replace editable fields on an already locked draft row."""

        draft.title = title
        draft.project_id = project_id
        draft.payload = payload
        draft.updated_at = datetime.now(UTC)
        await self._session.commit()
        await self._session.refresh(draft)
        return draft

    async def delete(self, draft_id: str) -> bool:
        draft = await self.get_for_update(draft_id)
        if draft is None:
            return False
        return await self.delete_existing(draft)

    async def delete_existing(self, draft: Draft) -> bool:
        """Delete an already locked draft row."""

        await self._session.delete(draft)
        await self._session.commit()
        return True
