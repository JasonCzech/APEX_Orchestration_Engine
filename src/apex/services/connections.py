"""Connection resolution: (port kind, connection id) -> live adapter instance.

M1 static implementation: connections come from the in-code DEV_CONNECTIONS map
(one stub/sim/env connection per port kind), so the full pipeline runs offline.
M2 replaces the lookup with DB-backed rows from the `connections` table (admin
CRUD, project scoping, cache keyed by (connection_id, updated_at)) while keeping
this resolve() surface unchanged — graph nodes and routers must depend on it,
never on the AdapterRegistry directly.
"""

from collections.abc import Iterable
from functools import lru_cache
from typing import Any

# Importing the adapter packages registers their factories with the AdapterRegistry.
import apex.adapters.sim_engine  # noqa: F401
import apex.adapters.stubs  # noqa: F401
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.ports.secrets import SecretsPort

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


class ConnectionResolver:
    """Resolves adapters from connection configs, caching instances per connection id."""

    def __init__(self, connections: Iterable[ConnectionConfig] | None = None) -> None:
        conns = list(connections) if connections is not None else list(DEV_CONNECTIONS.values())
        self._by_id: dict[str, ConnectionConfig] = {c.id: c for c in conns}
        self._default_by_kind: dict[PortKind, ConnectionConfig] = {}
        for conn in conns:
            self._default_by_kind.setdefault(conn.kind, conn)
        self._instances: dict[str, Any] = {}

    async def resolve(self, kind: PortKind, connection_id: str | None = None) -> Any:
        conn = self._select(kind, connection_id)
        cached = self._instances.get(conn.id)
        if cached is not None:
            return cached
        secrets: SecretsPort | None = None
        if conn.secret_ref is not None and conn.kind is not PortKind.SECRETS:
            secrets = await self.resolve(PortKind.SECRETS)
        adapter = await AdapterRegistry.build(conn, secrets)
        self._instances[conn.id] = adapter
        return adapter

    def _select(self, kind: PortKind, connection_id: str | None) -> ConnectionConfig:
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


@lru_cache
def get_connection_resolver() -> ConnectionResolver:
    """Process-wide default resolver over DEV_CONNECTIONS."""
    return ConnectionResolver()
