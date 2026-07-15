"""Async repository for uploaded document metadata (bytes live in the artifact store)."""

from collections.abc import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from apex.auth.identity import ScopeRef
from apex.persistence.models import Connection, Document
from apex.settings import get_settings


class DocumentsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, document: Document) -> Document:
        if document.artifact_connection_id is not None and not (
            document.artifact_connection_id.startswith("dev-") and not get_settings().is_locked_down
        ):
            connection = await self._session.scalar(
                select(Connection)
                .where(Connection.id == document.artifact_connection_id)
                .with_for_update()
            )
            if connection is None or not connection.enabled or connection.kind != "artifact_store":
                raise RuntimeError("artifact-store connection is missing or disabled")
        self._session.add(document)
        await self._session.commit()
        await self._session.refresh(document)
        return document

    async def is_persisted(self, document_id: str) -> bool:
        """Resolve an ambiguous commit/refresh failure before object compensation."""
        await self._session.rollback()
        return (
            await self._session.scalar(select(Document.id).where(Document.id == document_id))
            is not None
        )

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
        allowed_scopes: Sequence[ScopeRef] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        """Newest-first metadata listing.

        `allowed_scopes=None` means unrestricted (unscoped admin). Otherwise rows
        must be global, project-level in a scoped project, in an exact app scope,
        or in a project carrying a project-wide scope.
        """
        stmt = select(Document).order_by(Document.created_at.desc(), Document.id)
        if allowed_scopes is not None:
            project_ids = sorted({scope.project_id for scope in allowed_scopes})
            project_wide = sorted(
                {scope.project_id for scope in allowed_scopes if scope.app_id is None}
            )
            app_scopes = sorted(
                {
                    (scope.project_id, scope.app_id)
                    for scope in allowed_scopes
                    if scope.app_id is not None
                }
            )
            visibility: list[ColumnElement[bool]] = [Document.project_id.is_(None)]
            if project_ids:
                visibility.append(
                    and_(
                        Document.project_id.in_(project_ids),
                        Document.app_id.is_(None),
                    )
                )
            if project_wide:
                visibility.append(Document.project_id.in_(project_wide))
            visibility.extend(
                and_(Document.project_id == project_id, Document.app_id == app_id)
                for project_id, app_id in app_scopes
            )
            stmt = stmt.where(or_(*visibility))
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
