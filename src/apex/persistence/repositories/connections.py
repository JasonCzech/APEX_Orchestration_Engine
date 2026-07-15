"""Async repository for connections + host mappings (runtime adapter config).

Rows hold only configuration and supported ``env:NAME`` reference strings in
``secret_ref`` — raw secret values never enter this table.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, false, func, not_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import (
    ArtifactReference,
    ArtifactUploadIntent,
    Connection,
    Document,
    EngineRun,
    HostMapping,
    WorkItemMutation,
)
from apex.services.connection_credentials import (
    connection_options_require_repair,
    connection_url_requires_repair,
    reject_raw_secret_options,
    validate_secret_ref,
)

_TERMINAL_ENGINE_STATUSES = ("completed", "failed", "aborted")
_CONNECTION_TEXT_FIELDS = frozenset(
    {"kind", "provider", "name", "project_id", "base_url", "secret_ref"}
)
_RUNTIME_CONFIGURATION_FIELDS = frozenset(
    {"kind", "provider", "project_id", "base_url", "options", "secret_ref", "enabled"}
)


def _validate_persisted_connection_values(values: dict[str, Any]) -> None:
    """Defend non-HTTP writers from invalid or secret-bearing values."""

    for field in _CONNECTION_TEXT_FIELDS.intersection(values):
        value = values[field]
        if isinstance(value, str) and "\x00" in value:
            raise ValueError(f"connection {field} must not contain U+0000")
    if "options" in values:
        options = values["options"]
        if not isinstance(options, dict):
            raise ValueError("connection options must be a JSON object")
        reject_raw_secret_options(options)
        if connection_options_require_repair(options):
            raise ValueError(
                "connection options contain unsafe credential-bearing transport configuration"
            )

    if "base_url" in values and connection_url_requires_repair(values["base_url"]):
        raise ValueError("connection base_url contains unsafe credential-bearing configuration")

    if "secret_ref" in values:
        secret_ref = values["secret_ref"]
        if secret_ref is not None and not isinstance(secret_ref, str):
            raise ValueError("connection secret_ref must be a string or null")
        validate_secret_ref(secret_ref)


class DuplicateConnectionNameError(Exception):
    """The global unique `name` constraint was violated."""


class ConnectionsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_connections(
        self,
        *,
        kind: str | None = None,
        project: str | None = None,
        manageable_project_ids: Sequence[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Connection]:
        """`project` filters exact project_id; global rows have project_id NULL."""
        stmt = select(Connection).order_by(Connection.kind, Connection.name)
        if kind is not None:
            stmt = stmt.where(Connection.kind == kind)
        if project is not None:
            stmt = stmt.where(Connection.project_id == project)
        if manageable_project_ids is not None:
            projects = tuple(dict.fromkeys(manageable_project_ids))
            if not projects:
                stmt = stmt.where(false())
            else:
                trusted_private = Connection.options["_apex_trusted_private_host"].as_boolean()
                auth_mode = func.lower(
                    func.coalesce(Connection.options["auth_mode"].as_string(), "")
                )
                ambient_kubernetes = and_(
                    Connection.kind == "cluster_inventory",
                    func.lower(Connection.provider) == "kubernetes",
                    auth_mode.in_(("in_cluster", "in-cluster", "incluster")),
                )
                stmt = stmt.where(
                    Connection.project_id.in_(projects),
                    Connection.secret_ref.is_(None),
                    Connection.kind != "secrets",
                    trusted_private.is_not(True),
                    not_(ambient_kubernetes),
                )
        return list((await self._session.scalars(stmt.limit(limit).offset(offset))).all())

    async def get(self, connection_id: str) -> Connection | None:
        return await self._session.get(Connection, connection_id)

    async def get_for_update(self, connection_id: str) -> Connection | None:
        """Lock a lifecycle row until the caller commits its mutation."""
        return await self._session.scalar(
            select(Connection)
            .where(Connection.id == connection_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

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
        normalized_options = {} if options is None else options
        _validate_persisted_connection_values(
            {
                "kind": kind,
                "provider": provider,
                "name": name,
                "project_id": project_id,
                "base_url": base_url,
                "options": normalized_options,
                "secret_ref": secret_ref,
            }
        )
        conn = Connection(
            kind=kind,
            provider=provider,
            name=name,
            project_id=project_id,
            base_url=base_url,
            options=dict(normalized_options),
            secret_ref=secret_ref,
        )
        self._session.add(conn)
        await self._commit_and_refresh(conn)
        return conn

    async def update(self, conn: Connection, changes: dict[str, Any]) -> Connection:
        _validate_persisted_connection_values(changes)
        for field, value in changes.items():
            setattr(conn, field, value)
        if _RUNTIME_CONFIGURATION_FIELDS.intersection(changes):
            conn.runtime_version = func.now()
        await self._commit_and_refresh(conn)
        return conn

    async def set_enabled(self, conn: Connection, enabled: bool) -> Connection:
        # Availability is part of the runtime generation. Durable-reference
        # guards prevent a normal disable from stranding active work; any
        # out-of-band toggle must invalidate cached/reserved adapters explicitly.
        if enabled:
            # Repository callers can also encounter rows created before the
            # secret-free persistence contract.  Never re-enable one without
            # first repairing its complete credential-bearing target.
            _validate_persisted_connection_values(
                {
                    "base_url": conn.base_url,
                    "options": conn.options,
                    "secret_ref": conn.secret_ref,
                }
            )
        await self._session.execute(
            update(Connection)
            .where(Connection.id == conn.id)
            .values(enabled=enabled, runtime_version=func.now())
        )
        await self._session.commit()
        await self._session.refresh(conn)
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
            pipeline_artifact_id = await self._session.scalar(
                select(ArtifactReference.id)
                .where(ArtifactReference.connection_id == conn.id)
                .limit(1)
            )
            if pipeline_artifact_id is not None:
                return "stored pipeline artifacts"
            pending_artifact_id = await self._session.scalar(
                select(ArtifactUploadIntent.id)
                .where(ArtifactUploadIntent.connection_id == conn.id)
                .limit(1)
            )
            if pending_artifact_id is not None:
                return "pending pipeline artifact uploads"
        if conn.kind == "execution_engine":
            active_run_id = await self._session.scalar(
                select(EngineRun.id)
                .where(
                    EngineRun.connection_id == conn.id,
                    EngineRun.status.not_in(_TERMINAL_ENGINE_STATUSES),
                )
                .limit(1)
            )
            if active_run_id is not None:
                return "active engine runs"
            # Compatibility fallback for rows created before connection_id was
            # projected as a foreign-key lease.
            legacy_active_run_id = await self._session.scalar(
                select(EngineRun.id)
                .where(
                    EngineRun.connection_id.is_(None),
                    EngineRun.status.not_in(_TERMINAL_ENGINE_STATUSES),
                    EngineRun.handle["connection_id"].as_string() == conn.id,
                )
                .limit(1)
            )
            if legacy_active_run_id is not None:
                return "active engine runs"
        if conn.kind == "work_tracking":
            pending_mutation_id = await self._session.scalar(
                select(WorkItemMutation.id)
                .where(
                    WorkItemMutation.connection_id == conn.id,
                    WorkItemMutation.status.in_(("pending", "running")),
                )
                .limit(1)
            )
            if pending_mutation_id is not None:
                return "pending work-item mutations"
            retained_mutation_id = await self._session.scalar(
                select(WorkItemMutation.id)
                .where(WorkItemMutation.connection_id == conn.id)
                .limit(1)
            )
            if retained_mutation_id is not None:
                return "retained work-item idempotency records"
        return None

    async def replace_host_mappings(
        self, conn: Connection, mappings: Sequence[dict[str, Any]]
    ) -> Connection:
        """PUT semantics: the provided list fully replaces the existing one."""
        for mapping in mappings:
            for field in ("pattern", "target"):
                value = mapping.get(field)
                if isinstance(value, str) and "\x00" in value:
                    raise ValueError(f"host mapping {field} must not contain U+0000")
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
