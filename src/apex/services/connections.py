"""Connection resolution: (port kind, connection id) -> live adapter instance.

M2: resolution consults the `connections` table first (explicit connection_id >
project-scoped row > global row) and falls back to the static in-code
DEV_CONNECTIONS map — so `langgraph dev` keeps working without Postgres and DB
outages degrade gracefully (logged warning, stub adapters). Adapter instances
are cached keyed by (connection_id, updated_at), so admin edits invalidate on
the next resolve. The resolve() surface is unchanged from M1 — graph nodes and
routers must depend on it, never on the AdapterRegistry directly.

Note: ConnectionConfig has no base_url field, so a row's base_url is merged into
options["base_url"] for the adapter factory.
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from inspect import isawaitable
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.ports.secrets import SecretsPort

logger = structlog.get_logger(__name__)


def _throwaway_session_factory():  # noqa: ANN202 — (engine, sessionmaker) pair
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from apex.settings import get_settings

    engine = create_async_engine(get_settings().database.uri, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


DEV_CONNECTIONS: dict[PortKind, ConnectionConfig] = {
    PortKind.WORK_TRACKING: ConnectionConfig(
        id="dev-work-tracking-stub",
        kind=PortKind.WORK_TRACKING,
        provider="stub",
        name="Stub work tracking",
    ),
    PortKind.LOG_SEARCH: ConnectionConfig(
        id="dev-log-search-stub", kind=PortKind.LOG_SEARCH, provider="stub", name="Stub log search"
    ),
    PortKind.OBSERVABILITY: ConnectionConfig(
        id="dev-observability-stub",
        kind=PortKind.OBSERVABILITY,
        provider="stub",
        name="Stub observability",
    ),
    PortKind.DOCUMENTS: ConnectionConfig(
        id="dev-documents-stub", kind=PortKind.DOCUMENTS, provider="stub", name="Stub documents"
    ),
    PortKind.CLUSTER_INVENTORY: ConnectionConfig(
        id="dev-cluster-inventory-stub",
        kind=PortKind.CLUSTER_INVENTORY,
        provider="stub",
        name="Stub cluster inventory",
    ),
    PortKind.SOURCE_CONTROL: ConnectionConfig(
        id="dev-source-control-stub",
        kind=PortKind.SOURCE_CONTROL,
        provider="stub",
        name="Stub source control",
    ),
    PortKind.EXECUTION_ENGINE: ConnectionConfig(
        id="dev-engine-sim",
        kind=PortKind.EXECUTION_ENGINE,
        provider="sim",
        name="Simulated execution engine",
    ),
    PortKind.ARTIFACT_STORE: ConnectionConfig(
        id="dev-artifact-store-memory",
        kind=PortKind.ARTIFACT_STORE,
        provider="stub",
        name="In-memory artifact store",
    ),
    PortKind.SECRETS: ConnectionConfig(
        id="dev-secrets-env", kind=PortKind.SECRETS, provider="env", name="Env secrets"
    ),
}


@dataclass(frozen=True)
class StoredConnection:
    """A `connections` row projected for resolution (no ORM coupling)."""

    config: ConnectionConfig
    project_id: str | None
    enabled: bool
    updated_at: datetime | None


def connection_config_from_row(row: Any) -> ConnectionConfig:
    """Map a Connection ORM row (or anything row-shaped) to a ConnectionConfig.

    base_url is merged into options["base_url"] — ConnectionConfig has no
    base_url field by design (adapters read it from options).
    """
    options: dict[str, Any] = dict(row.options or {})
    if row.base_url:
        options.setdefault("base_url", row.base_url)
    return ConnectionConfig(
        id=row.id,
        kind=PortKind(row.kind),
        provider=row.provider,
        name=row.name,
        options=options,
        secret_ref=row.secret_ref,
    )


def stored_connection_from_row(row: Any) -> StoredConnection:
    return StoredConnection(
        config=connection_config_from_row(row),
        project_id=row.project_id,
        enabled=bool(row.enabled),
        updated_at=row.updated_at,
    )


class ConnectionStore(Protocol):
    """Lookup surface the resolver needs; DB-backed in prod, fake in tests."""

    async def get(self, connection_id: str) -> StoredConnection | None: ...

    async def find_default(
        self, kind: PortKind, project_id: str | None
    ) -> StoredConnection | None: ...


class DbConnectionStore:
    """Loads connection rows with a throwaway engine + session per call.

    Graph nodes run DB work inside short-lived asyncio.run loops on worker
    threads, so the module-cached engine in apex.persistence.db must NOT be
    used here — an asyncpg connection created in one event loop crashes when
    reused from another ("attached to a different loop"). A NullPool engine
    per call keeps every connection loop-local; routers pay a small per-call
    cost on these infrequent lookups.
    """

    async def get(self, connection_id: str) -> StoredConnection | None:
        from apex.persistence.models import Connection

        engine, session_factory = _throwaway_session_factory()
        try:
            async with session_factory() as session:
                row = await session.get(Connection, connection_id)
                return stored_connection_from_row(row) if row is not None else None
        finally:
            await engine.dispose()

    async def find_default(self, kind: PortKind, project_id: str | None) -> StoredConnection | None:
        from apex.persistence.models import Connection

        engine, session_factory = _throwaway_session_factory()
        try:
            async with session_factory() as session:
                stmt = (
                    select(Connection)
                    .where(Connection.kind == kind.value, Connection.enabled.is_(True))
                    .order_by(Connection.updated_at.desc(), Connection.id)
                )
                rows = list((await session.scalars(stmt)).all())
        finally:
            await engine.dispose()
        if project_id is not None:
            scoped = [r for r in rows if r.project_id == project_id]
            if scoped:
                return stored_connection_from_row(scoped[0])
        global_rows = [r for r in rows if r.project_id is None]
        return stored_connection_from_row(global_rows[0]) if global_rows else None


class ConnectionResolver:
    """Resolves adapters from connection rows/configs, caching built instances.

    Cache key per connection id is its updated_at (None for static configs):
    an admin PATCH bumps updated_at, so the next resolve rebuilds the adapter.
    """

    def __init__(
        self,
        connections: Iterable[ConnectionConfig] | None = None,
        store: ConnectionStore | None = None,
    ) -> None:
        conns = list(connections) if connections is not None else list(DEV_CONNECTIONS.values())
        self._by_id: dict[str, ConnectionConfig] = {c.id: c for c in conns}
        self._default_by_kind: dict[PortKind, ConnectionConfig] = {}
        for conn in conns:
            self._default_by_kind.setdefault(conn.kind, conn)
        self._store = store
        self._instances: dict[str, tuple[datetime | None, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def resolve(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> Any:
        """Precedence: explicit connection_id > project-scoped row > global row >
        static DEV_CONNECTIONS fallback."""
        stored = await self._select_stored(kind, connection_id, project_id)
        if stored is not None:
            return await self._build_cached(stored.config, stored.updated_at)
        conn = self._select_static(kind, connection_id)
        return await self._build_cached(conn, None)

    # ── selection ───────────────────────────────────────────────────────────

    async def _select_stored(
        self, kind: PortKind, connection_id: str | None, project_id: str | None
    ) -> StoredConnection | None:
        if self._store is None:
            return None
        try:
            if connection_id is not None:
                stored = await self._store.get(connection_id)
                if stored is None:
                    return None  # fall through to the static map (KeyError if unknown)
                self._check_usable(stored, kind, project_id)
                return stored
            stored = await self._store.find_default(kind, project_id)
            if stored is None:
                logger.debug(
                    "apex.connections.static_fallback", kind=kind.value, project_id=project_id
                )
            return stored
        except (SQLAlchemyError, OSError) as exc:
            logger.warning(
                "apex.connections.db_unavailable",
                kind=kind.value,
                connection_id=connection_id,
                error=f"{exc.__class__.__name__}: {exc}",
            )
            return None

    @staticmethod
    def _check_usable(stored: StoredConnection, kind: PortKind, project_id: str | None) -> None:
        conn = stored.config
        if conn.kind is not kind:
            raise ValueError(
                f"connection {conn.id!r} is kind={conn.kind.value!r}, not {kind.value!r}"
            )
        if not stored.enabled:
            raise ValueError(f"connection {conn.id!r} is disabled")
        if stored.project_id is not None and stored.project_id != project_id:
            raise ValueError(
                f"connection {conn.id!r} is scoped to project {stored.project_id!r}, "
                f"not {project_id!r}"
            )

    def _select_static(self, kind: PortKind, connection_id: str | None) -> ConnectionConfig:
        if connection_id is not None:
            try:
                conn = self._by_id[connection_id]
            except KeyError:
                raise KeyError(
                    f"unknown connection_id {connection_id!r}; known: {sorted(self._by_id)}"
                ) from None
            if conn.kind is not kind:
                raise ValueError(
                    f"connection {connection_id!r} is kind={conn.kind.value!r}, not {kind.value!r}"
                )
            return conn
        try:
            return self._default_by_kind[kind]
        except KeyError:
            raise KeyError(f"no default connection configured for kind {kind.value!r}") from None

    # ── building ────────────────────────────────────────────────────────────

    async def _build_cached(self, conn: ConnectionConfig, version: datetime | None) -> Any:
        lock = self._locks.setdefault(conn.id, asyncio.Lock())
        async with lock:
            cached = self._instances.get(conn.id)
            if cached is not None and cached[0] == version:
                return cached[1]
            secrets: SecretsPort | None = None
            if conn.secret_ref is not None and conn.kind is not PortKind.SECRETS:
                secrets = await self.resolve(PortKind.SECRETS)
            adapter = await AdapterRegistry.build(conn, secrets)
            self._instances[conn.id] = (version, adapter)
            if cached is not None:
                await _close_adapter(cached[1])
            return adapter

    async def close(self) -> None:
        """Close cached adapter clients before process shutdown or test teardown."""
        instances = list(self._instances.values())
        self._instances.clear()
        self._locks.clear()
        for _, adapter in instances:
            await _close_adapter(adapter)


async def _close_adapter(adapter: Any) -> None:
    close = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
    if close is None:
        return
    result = close()
    if isawaitable(result):
        await result


@lru_cache
def get_connection_resolver() -> ConnectionResolver:
    """Process-wide resolver: DB rows first, DEV_CONNECTIONS static fallback."""
    return ConnectionResolver(store=DbConnectionStore())


# Importing adapter packages registers their factories with the AdapterRegistry.
# Keep registration after ConnectionResolver/get_connection_resolver are defined:
# real work-tracking adapters import apex.services.work_tracking, whose router
# dependency providers import this module.
from apex.adapters import register_builtin_adapters  # noqa: E402

register_builtin_adapters()
