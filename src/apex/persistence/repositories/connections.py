"""Async repository for connections + host mappings (runtime adapter config).

Rows hold only configuration and supported ``env:NAME`` reference strings in
``secret_ref`` — raw secret values never enter this table.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, false, func, not_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import ConnectionConfig, PortKind
from apex.persistence.models import (
    ArtifactReference,
    ArtifactUploadIntent,
    Connection,
    Document,
    EngineRun,
    HostMapping,
    WorkItemMutation,
)
from apex.persistence.repositories._conflicts import (
    bounded_driver_message,
    driver_constraint_name,
)
from apex.services.connection_credentials import (
    connection_options_require_repair,
    connection_url_requires_repair,
    reject_credential_text,
    reject_raw_secret_options,
    validate_secret_ref,
)
from apex.services.connections import validate_scoped_work_tracking_config

_TERMINAL_ENGINE_STATUSES = ("completed", "failed", "aborted")
_CONNECTION_TEXT_FIELDS = frozenset(
    {"kind", "provider", "name", "project_id", "base_url", "secret_ref"}
)
_NULLABLE_CONNECTION_TEXT_FIELDS = frozenset({"project_id", "base_url", "secret_ref"})
_CONNECTION_TEXT_BOUNDS = {
    "kind": (1, 64),
    "provider": (1, 64),
    "name": (1, 255),
    "project_id": (1, 255),
    "base_url": (1, 1_024),
    "secret_ref": (1, 259),
}
_CONNECTION_MUTABLE_FIELDS = frozenset(
    {"provider", "name", "project_id", "base_url", "options", "secret_ref", "enabled"}
)
_RUNTIME_CONFIGURATION_FIELDS = frozenset(
    {"kind", "provider", "project_id", "base_url", "options", "secret_ref", "enabled"}
)


def _validate_persisted_connection_values(values: dict[str, Any]) -> None:
    """Defend non-HTTP writers from invalid or secret-bearing values."""

    for field in _CONNECTION_TEXT_FIELDS.intersection(values):
        value = values[field]
        if value is None and field in _NULLABLE_CONNECTION_TEXT_FIELDS:
            continue
        if type(value) is not str:
            raise ValueError(f"connection {field} must be a string")
        minimum, maximum = _CONNECTION_TEXT_BOUNDS[field]
        if not minimum <= len(value) <= maximum or "\x00" in value:
            raise ValueError(
                f"connection {field} must be a {minimum}-{maximum} character string without U+0000"
            )
        reject_credential_text(value, label=f"connection {field}")
    if "options" in values:
        options = values["options"]
        if type(options) is not dict:
            raise ValueError("connection options must be a JSON object")
        reject_raw_secret_options(options)
        if connection_options_require_repair(options):
            raise ValueError(
                "connection options contain unsafe credential-bearing transport configuration"
            )

    if "enabled" in values and type(values["enabled"]) is not bool:
        raise ValueError("connection enabled must be a boolean")

    if "base_url" in values and connection_url_requires_repair(values["base_url"]):
        raise ValueError("connection base_url contains unsafe credential-bearing configuration")

    if "secret_ref" in values:
        secret_ref = values["secret_ref"]
        if secret_ref is not None and not isinstance(secret_ref, str):
            raise ValueError("connection secret_ref must be a string or null")
        validate_secret_ref(secret_ref)


def _validate_scoped_work_tracking_values(values: dict[str, Any]) -> None:
    """Apply the provider project boundary to one complete effective row."""

    kind: PortKind | None = None
    try:
        kind = PortKind(values["kind"])
    except ValueError:
        pass
    if kind is None:
        raise ValueError("connection kind is unsupported")
    options = dict(values["options"])
    base_url = values["base_url"]
    if base_url is not None:
        options.setdefault("base_url", base_url)
    validate_scoped_work_tracking_config(
        ConnectionConfig(
            id="repository-validation",
            kind=kind,
            provider=values["provider"],
            name=values["name"],
            options=options,
            secret_ref=values["secret_ref"],
        ),
        internal_project_id=values["project_id"],
    )


def _effective_connection_values(
    conn: Connection, changes: dict[str, Any] | None = None
) -> dict[str, Any]:
    changes = {} if changes is None else changes
    return {
        field: changes[field] if field in changes else getattr(conn, field)
        for field in (
            "kind",
            "provider",
            "name",
            "project_id",
            "base_url",
            "options",
            "secret_ref",
            "enabled",
        )
    }


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
        values = {
            "kind": kind,
            "provider": provider,
            "name": name,
            "project_id": project_id,
            "base_url": base_url,
            "options": normalized_options,
            "secret_ref": secret_ref,
            "enabled": True,
        }
        _validate_persisted_connection_values(values)
        _validate_scoped_work_tracking_values(values)
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
        if type(changes) is not dict or len(changes) > len(_CONNECTION_MUTABLE_FIELDS):
            raise ValueError("unsupported connection fields")
        if any(
            type(field) is not str or field not in _CONNECTION_MUTABLE_FIELDS
            for field in dict.keys(changes)
        ):
            raise ValueError("unsupported connection fields")
        # Reject an invalid supplied field before consulting the existing row.
        # Besides keeping errors deterministic, this avoids touching row-shaped
        # objects at all when a direct writer's patch is already unsafe.
        _validate_persisted_connection_values(changes)
        effective_values = _effective_connection_values(conn, changes)
        _validate_persisted_connection_values(effective_values)
        _validate_scoped_work_tracking_values(effective_values)
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
        if type(enabled) is not bool:
            raise ValueError("connection enabled must be a boolean")
        if enabled:
            # Repository callers can also encounter rows created before the
            # secret-free persistence contract.  Never re-enable one without
            # first repairing its complete credential-bearing target.
            effective_values = _effective_connection_values(conn, {"enabled": True})
            _validate_persisted_connection_values(effective_values)
            _validate_scoped_work_tracking_values(effective_values)
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
        if type(mappings) is not list or len(mappings) > 256:
            raise ValueError("host mappings must be a list of at most 256 items")
        for mapping in mappings:
            if (
                type(mapping) is not dict
                or len(mapping) not in {2, 3}
                or any(
                    type(field) is not str or field not in {"pattern", "target", "enabled"}
                    for field in dict.keys(mapping)
                )
                or "pattern" not in mapping
                or "target" not in mapping
            ):
                raise ValueError("host mapping must contain only pattern, target, and enabled")
            for field in ("pattern", "target"):
                value = mapping.get(field)
                if type(value) is not str:
                    raise ValueError(f"host mapping {field} must be a string")
                if not 1 <= len(value) <= 1_024 or "\x00" in value:
                    raise ValueError(
                        f"host mapping {field} must be a 1-1024 character string without U+0000"
                    )
                reject_credential_text(value, label=f"host mapping {field}")
            if "enabled" in mapping and type(mapping["enabled"]) is not bool:
                raise ValueError("host mapping enabled must be a boolean")
        conn.host_mappings = [
            HostMapping(pattern=m["pattern"], target=m["target"], enabled=m.get("enabled", True))
            for m in mappings
        ]
        await self._commit_and_refresh(conn)
        return conn

    async def _commit_and_refresh(self, conn: Connection) -> None:
        duplicate_name = False
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_duplicate_connection_name(exc):
                duplicate_name = True
            else:
                raise
        if duplicate_name:
            raise DuplicateConnectionNameError("connection name already exists")
        await self._session.refresh(conn)


def _is_duplicate_connection_name(exc: IntegrityError) -> bool:
    constraint_name = driver_constraint_name(exc.orig)
    message = bounded_driver_message(exc.orig)
    return constraint_name == "uq_connections_name" or (
        "uq_connections_name" in message
        or ("unique constraint failed" in message and "connections.name" in message)
    )
