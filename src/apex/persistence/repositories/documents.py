"""Async repository for uploaded document metadata (bytes live in the artifact store)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from apex.auth.identity import ScopeRef
from apex.domain.durable_evidence import sanitize_durable_text
from apex.persistence.models import Connection, Document
from apex.settings import get_settings


class DocumentUploadNotPendingError(RuntimeError):
    """An upload was already finalized or claimed for durable cleanup."""


def sanitize_document_text(value: str | None) -> str | None:
    """Make document Text values acceptable to PostgreSQL's text protocol."""

    if value is None or "\x00" not in value:
        return value
    return value.replace("\x00", "\ufffd")


def _sanitize_document_text_fields(document: Document) -> None:
    document.summary = sanitize_document_text(document.summary)
    document.extracted_text = sanitize_document_text(document.extracted_text)
    document.parse_error = sanitize_durable_text(document.parse_error, 4096)


class DocumentsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, document: Document) -> Document:
        await self._prepare_artifact_affinity(document)
        _sanitize_document_text_fields(document)
        self._session.add(document)
        await self._session.commit()
        await self._session.refresh(document)
        return document

    async def _prepare_artifact_affinity(self, document: Document) -> None:
        """Normalize dev ids and lock/validate production store affinity."""

        artifact_connection_id = document.artifact_connection_id
        if (
            artifact_connection_id is not None
            and artifact_connection_id.startswith("dev-")
            and not get_settings().is_locked_down
        ):
            # Static adapters have no Connection row. Persisting their synthetic
            # id would violate the durable FK; NULL deliberately re-resolves the
            # development default when the document is read or deleted.
            document.artifact_connection_id = None
        elif artifact_connection_id is not None:
            result = await self._session.execute(
                select(
                    Connection.id,
                    Connection.kind,
                    Connection.enabled,
                    Connection.project_id,
                )
                .where(Connection.id == artifact_connection_id)
                .with_for_update()
            )
            connection = result.one_or_none()
            if connection is None or not connection.enabled or connection.kind != "artifact_store":
                raise RuntimeError("artifact-store connection is missing or disabled")
            if connection.project_id is not None and connection.project_id != document.project_id:
                raise RuntimeError("artifact-store connection is outside the document project")

    async def is_persisted(self, document_id: str) -> bool:
        """Resolve an ambiguous commit/refresh failure before object compensation."""
        await self._session.rollback()
        return (
            await self._session.scalar(select(Document.id).where(Document.id == document_id))
            is not None
        )

    async def get(self, document_id: str) -> Document | None:
        return await self._session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.deletion_pending_at.is_(None),
                Document.upload_pending_at.is_(None),
            )
        )

    async def get_any(self, document_id: str) -> Document | None:
        """Load live or hidden metadata without retaining a row lock."""

        return await self._session.scalar(select(Document).where(Document.id == document_id))

    async def get_any_for_update(self, document_id: str) -> Document | None:
        """Load live or hidden metadata for an administrative affinity repair."""

        return await self._session.scalar(
            select(Document).where(Document.id == document_id).with_for_update()
        )

    async def get_by_artifact_key(self, artifact_key: str) -> Document | None:
        return await self._session.scalar(
            select(Document).where(
                Document.artifact_key == artifact_key,
                Document.deletion_pending_at.is_(None),
                Document.upload_pending_at.is_(None),
            )
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
        stmt = (
            select(Document)
            .where(
                Document.deletion_pending_at.is_(None),
                Document.upload_pending_at.is_(None),
            )
            .order_by(Document.created_at.desc(), Document.id)
        )
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

    async def mark_deletion_pending(self, document: Document) -> None:
        """Commit logical deletion before any irreversible object-store IO."""

        if document.deletion_pending_at is None:
            document.deletion_pending_at = datetime.now(UTC)
            document.cleanup_retry_at = None
            document.cleanup_attempt_count = 0
            document.cleanup_last_error = None
        await self._session.commit()

    async def stage_upload(self, document: Document) -> Document:
        """Commit a hidden upload intent before any object-store write."""

        await self._prepare_artifact_affinity(document)
        _sanitize_document_text_fields(document)
        document.upload_pending_at = datetime.now(UTC)
        self._session.add(document)
        await self._session.commit()
        await self._session.refresh(document)
        return document

    async def finalize_upload(self, document: Document) -> Document:
        if document.upload_pending_at is None:
            raise DocumentUploadNotPendingError(document.id)
        _sanitize_document_text_fields(document)
        document_id = document.id
        result = await self._session.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.upload_pending_at.is_not(None),
                Document.deletion_pending_at.is_(None),
            )
            .values(
                summary=document.summary,
                extracted_text=document.extracted_text,
                extracted_chars=document.extracted_chars,
                parse_status=document.parse_status,
                parse_error=document.parse_error,
                upload_pending_at=None,
                cleanup_retry_at=None,
                cleanup_attempt_count=0,
                cleanup_last_error=None,
            )
            .returning(Document)
            .execution_options(autoflush=False, populate_existing=True)
        )
        finalized = result.scalar_one_or_none()
        if finalized is None:
            await self._session.rollback()
            # Rollback expires ORM attributes. Keep the primitive identifier so
            # this conflict path never triggers an implicit async refresh.
            raise DocumentUploadNotPendingError(document_id)
        await self._session.commit()
        return finalized

    async def mark_upload_deletion_pending(self, document_id: str) -> Document | None:
        """Atomically tombstone one still-pending upload by primitive ID.

        Callers may invoke this after a rollback has expired their original ORM
        instance. A missing return value means ownership is uncertain and object
        bytes must not be compensated by the caller.
        """

        result = await self._session.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.upload_pending_at.is_not(None),
                Document.deletion_pending_at.is_(None),
            )
            .values(
                deletion_pending_at=datetime.now(UTC),
                cleanup_retry_at=None,
                cleanup_attempt_count=0,
                cleanup_last_error=None,
            )
            .returning(Document)
            .execution_options(autoflush=False, populate_existing=True)
        )
        tombstone = result.scalar_one_or_none()
        if tombstone is None:
            await self._session.rollback()
            return None
        await self._session.commit()
        return tombstone

    async def renew_upload_lease(self, document_id: str) -> bool:
        """Keep an active provider write from being mistaken for a crashed one."""

        result = await self._session.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.upload_pending_at.is_not(None),
                Document.deletion_pending_at.is_(None),
            )
            .values(
                upload_pending_at=datetime.now(UTC),
                cleanup_retry_at=None,
                cleanup_attempt_count=0,
                cleanup_last_error=None,
            )
            .returning(Document.id)
            .execution_options(autoflush=False)
        )
        renewed = result.scalar_one_or_none() is not None
        await self._session.commit()
        return renewed

    async def claim_stale_upload(self, document: Document) -> Document | None:
        """Atomically win cleanup against a concurrent successful finalizer."""

        pending_at = document.upload_pending_at
        if pending_at is None:
            return None
        result = await self._session.execute(
            update(Document)
            .where(
                Document.id == document.id,
                Document.upload_pending_at == pending_at,
                Document.deletion_pending_at.is_(None),
            )
            .values(
                deletion_pending_at=datetime.now(UTC),
                cleanup_retry_at=None,
                cleanup_last_error=None,
            )
            .returning(Document)
            .execution_options(autoflush=False, populate_existing=True)
        )
        claimed = result.scalar_one_or_none()
        if claimed is None:
            await self._session.rollback()
            return None
        await self._session.commit()
        return claimed

    async def resolve_finalized_upload(self, document_id: str) -> Document | None:
        """Return authoritative live metadata after an ambiguous finalize commit."""

        await self._session.rollback()
        return await self._session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.upload_pending_at.is_(None),
                Document.deletion_pending_at.is_(None),
            )
        )

    async def get_pending_deletion(self, document_id: str) -> Document | None:
        return await self._session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.deletion_pending_at.is_not(None),
            )
        )

    async def get_pending_upload(self, document_id: str) -> Document | None:
        return await self._session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.upload_pending_at.is_not(None),
                Document.deletion_pending_at.is_(None),
            )
        )

    async def list_pending_deletions(self, *, limit: int = 100) -> list[Document]:
        now = datetime.now(UTC)
        result = await self._session.scalars(
            select(Document)
            .where(
                Document.deletion_pending_at.is_not(None),
                or_(
                    Document.cleanup_retry_at.is_(None),
                    Document.cleanup_retry_at <= now,
                ),
            )
            .order_by(Document.deletion_pending_at, Document.id)
            .limit(limit)
        )
        return list(result)

    async def list_stale_pending_uploads(
        self,
        *,
        before: datetime,
        limit: int = 100,
    ) -> list[Document]:
        now = datetime.now(UTC)
        result = await self._session.scalars(
            select(Document)
            .where(
                Document.upload_pending_at.is_not(None),
                Document.upload_pending_at <= before,
                Document.deletion_pending_at.is_(None),
                or_(
                    Document.cleanup_retry_at.is_(None),
                    Document.cleanup_retry_at <= now,
                ),
            )
            .order_by(Document.upload_pending_at, Document.id)
            .limit(limit)
        )
        return list(result)

    async def defer_cleanup(self, document_id: str, *, error: str) -> bool:
        """Back off one poison cleanup row so later intents receive a turn."""

        document = await self._session.scalar(
            select(Document).where(Document.id == document_id).with_for_update()
        )
        if document is None or (
            document.deletion_pending_at is None and document.upload_pending_at is None
        ):
            await self._session.rollback()
            return False
        attempts = int(document.cleanup_attempt_count or 0) + 1
        delay_s = min(30 * (2 ** min(attempts - 1, 10)), 6 * 60 * 60)
        document.cleanup_attempt_count = attempts
        document.cleanup_retry_at = datetime.now(UTC) + timedelta(seconds=delay_s)
        document.cleanup_last_error = sanitize_durable_text(error, 4096)
        await self._session.commit()
        return True

    async def assign_artifact_connection(
        self,
        document: Document,
        connection_id: str,
    ) -> Document:
        """One-time operator repair for legacy rows whose store affinity is unknown."""

        if document.upload_pending_at is not None and document.deletion_pending_at is None:
            raise ValueError("cannot change affinity while a document upload is active")
        if document.artifact_connection_id is not None:
            if document.artifact_connection_id == connection_id:
                return document
            raise ValueError("document artifact-store affinity is already fixed")
        result = await self._session.execute(
            select(Connection.id, Connection.kind, Connection.enabled, Connection.project_id)
            .where(Connection.id == connection_id)
            .with_for_update()
        )
        connection = result.one_or_none()
        if connection is None or not connection.enabled or connection.kind != "artifact_store":
            raise ValueError("artifact-store connection is missing or disabled")
        if connection.project_id is not None and connection.project_id != document.project_id:
            raise ValueError("artifact-store connection is outside the document project")
        document.artifact_connection_id = connection.id
        document.cleanup_retry_at = None
        document.cleanup_attempt_count = 0
        document.cleanup_last_error = None
        await self._session.commit()
        return document

    async def complete_deletion(self, document_id: str) -> None:
        """Idempotently remove a tombstone after the object is confirmed absent."""

        await self._session.execute(
            delete(Document).where(
                Document.id == document_id,
                Document.deletion_pending_at.is_not(None),
            )
        )
        await self._session.commit()
