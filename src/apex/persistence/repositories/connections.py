"""Async repository for connections + host mappings (runtime adapter config).

Rows hold only configuration and `secret_ref` REFERENCE strings ("env:NAME",
"vault:path#key", ...) — raw secret values never enter this table.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import Connection, Document, EngineRun, HostMapping

_TERMINAL_ENGINE_STATUSES = ("completed", "failed", "aborted")


class DuplicateConnectionNameError(Exception):
    """The global unique `name` constraint was violated."""


class ConnectionsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_connections(
        self, *, kind: str | None = None, project: str | None = None
    ) -> list[Connection]:
        """`project` filters exact project_id; global rows have project_id NULL."""
        stmt = select(Connection).order_by(Connection.kind, Connection.name)
        if kind is not None:
            stmt = stmt.where(Connection.kind == kind)
        if project is not None:
            stmt = stmt.where(Connection.project_id == project)
        return list((await self._session.scalars(stmt)).all())

    async def get(self, connection_id: str) -> Connection | None:
        return await self._session.get(Connection, connection_id)

    async def create(
        self,
        *,
        kind: str,
        provider: str,
        name: str,
        project_id: str | None = None,
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
        secret_ref: str | None = None,
    ) -> Connection:
        conn = Connection(
            kind=kind,
            provider=provider,
            name=name,
            project_id=project_id,
            base_url=base_url,
            options=dict(options or {}),
            secret_ref=secret_ref,
        )
        self._session.add(conn)
        await self._commit_and_refresh(conn)
        return conn

    async def update(self, conn: Connection, changes: dict[str, Any]) -> Connection:
        for field, value in changes.items():
            setattr(conn, field, value)
        await self._commit_and_refresh(conn)
        return conn

    async def set_enabled(self, conn: Connection, enabled: bool) -> Connection:
        conn.enabled = enabled
        await self._commit_and_refresh(conn)
        return conn

    async def delete(self, conn: Connection) -> None:
        await self._session.delete(conn)
        await self._session.commit()

    async def durable_reference_reason(self, conn: Connection) -> str | None:
        """Return why disabling/deleting this row would break durable state."""

        if conn.kind == "artifact_store":
            document_id = await self._session.scalar(
                select(Document.id).where(Document.artifact_connection_id == conn.id).limit(1)
            )
            if document_id is not None:
                return "stored documents"
            engine_artifact_id = await self._session.scalar(
                select(EngineRun.id).where(EngineRun.artifact_connection_id == conn.id).limit(1)
            )
            if engine_artifact_id is not None:
                return "stored engine artifacts"
        if conn.kind == "execution_engine":
            handles = await self._session.scalars(
                select(EngineRun.handle).where(EngineRun.status.not_in(_TERMINAL_ENGINE_STATUSES))
            )
            if any(
                isinstance(handle, dict) and handle.get("connection_id") == conn.id
                for handle in handles
            ):
                return "active engine runs"
        return None

    async def replace_host_mappings(
        self, conn: Connection, mappings: Sequence[dict[str, Any]]
    ) -> Connection:
        """PUT semantics: the provided list fully replaces the existing one."""
        conn.host_mappings = [
            HostMapping(
                pattern=m["pattern"], target=m["target"], enabled=bool(m.get("enabled", True))
            )
            for m in mappings
        ]
        await self._commit_and_refresh(conn)
        return conn

    async def _commit_and_refresh(self, conn: Connection) -> None:
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise DuplicateConnectionNameError(str(exc.orig)) from exc
        await self._session.refresh(conn)
