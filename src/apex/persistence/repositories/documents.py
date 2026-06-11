"""Async repository for uploaded document metadata (bytes live in the artifact store)."""

from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import Document


class DocumentsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, document: Document) -> Document:
        self._session.add(document)
        await self._session.commit()
        await self._session.refresh(document)
        return document

    async def get(self, document_id: str) -> Document | None:
        return await self._session.get(Document, document_id)

    async def get_by_artifact_key(self, artifact_key: str) -> Document | None:
        return await self._session.scalar(
            select(Document).where(Document.artifact_key == artifact_key)
        )

    async def list(
        self,
        *,
        project: str | None = None,
        q: str | None = None,
        allowed_project_ids: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        """Newest-first metadata listing.

        `allowed_project_ids=None` means unrestricted (admin/unscoped); otherwise rows
        must be global (project_id NULL) or belong to one of the allowed projects.
        """
        stmt = select(Document).order_by(Document.created_at.desc(), Document.id)
        if allowed_project_ids is not None:
            stmt = stmt.where(
                or_(
                    Document.project_id.is_(None),
                    Document.project_id.in_(list(allowed_project_ids)),
                )
            )
        if project is not None:
            stmt = stmt.where(Document.project_id == project)
        if q:
            needle = f"%{q}%"
            stmt = stmt.where(or_(Document.name.ilike(needle), Document.summary.ilike(needle)))
        stmt = stmt.limit(limit).offset(offset)
        result = await self._session.scalars(stmt)
        return list(result)

    async def delete(self, document: Document) -> None:
        await self._session.delete(document)
        await self._session.commit()
