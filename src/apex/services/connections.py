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
import hashlib
import json
import socket
import threading
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from inspect import isawaitable
from ipaddress import ip_address
from typing import Any, Protocol
from urllib.parse import urlsplit

import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.ports.secrets import SecretsPort
from apex.settings import get_settings

logger = structlog.get_logger(__name__)

DENIED_ADAPTER_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
}
DENIED_ADAPTER_HOST_SUFFIXES = (".metadata.google.internal",)
INTERNAL_PROJECT_BINDING_ATTR = "apex_project_id"
TRUSTED_PRIVATE_HOST_OPTION = "_apex_trusted_private_host"


def _throwaway_session_factory():  # noqa: ANN202 — (engine, sessionmaker) pair
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings

    database = get_settings().database
    engine = create_async_engine(
        database_asyncpg_uri(database.uri),
        poolclass=NullPool,
        connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
    )
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


@dataclass
class _LoopCache:
    """Adapter instances and build locks owned by exactly one event loop."""

    instances: dict[str, tuple[datetime | None, Any]]
    locks: dict[str, asyncio.Lock]


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


def validate_adapter_base_url(raw_url: Any, *, allow_private_hosts: bool | None = None) -> None:
    """Reject adapter targets that resolve to local, private, or metadata hosts."""

    if raw_url is None:
        return
    raw_url = str(raw_url).strip()
    if not raw_url:
        raise ValueError("adapter URL must be a non-empty http(s) URL")

    parsed = urlsplit(raw_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("adapter URL must be an http(s) URL without embedded credentials")
    if allow_private_hosts is None:
        allow_private_hosts = get_settings().allow_private_adapter_hosts
    if allow_private_hosts:
        return

    normalized = parsed.hostname.rstrip(".").lower()
    if normalized in DENIED_ADAPTER_HOSTS or normalized.endswith(DENIED_ADAPTER_HOST_SUFFIXES):
        raise ValueError("private adapter hosts are disabled")

    addresses = _resolve_adapter_host(normalized, parsed.port)
    for address in addresses:
        if not address.is_global:
            raise ValueError("private adapter hosts are disabled")


def validate_connection_config(config: ConnectionConfig) -> None:
    # `endpoint` is the S3/MinIO transport target. It is just as security
    # sensitive as the conventional adapter `base_url` and must not bypass the
    # SSRF policy.
    allow_private = config.options.get(TRUSTED_PRIVATE_HOST_OPTION) is True
    validate_adapter_base_url(
        config.options.get("base_url"), allow_private_hosts=allow_private or None
    )
    endpoint = config.options.get("endpoint")
    if endpoint is not None:
        raw_endpoint = str(endpoint).strip()
        if "://" not in raw_endpoint:
            scheme = "https" if config.options.get("secure") is True else "http"
            raw_endpoint = f"{scheme}://{raw_endpoint}"
        validate_adapter_base_url(raw_endpoint, allow_private_hosts=allow_private or None)


def _resolve_adapter_host(host: str, port: int | None) -> list[Any]:
    try:
        return [ip_address(host)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("adapter host could not be resolved") from exc
    addresses = {info[4][0] for info in infos if info and info[4]}
    return [ip_address(address) for address in addresses]


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
    """Resolves adapters from connection rows/configs, caching per event loop.

    Cache key per connection id is its updated_at (None for static configs):
    an admin PATCH bumps updated_at, so the next resolve rebuilds the adapter.
    Async clients and ``asyncio.Lock`` objects never cross loop boundaries.
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
        self._loop_caches: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopCache] = (
            weakref.WeakKeyDictionary()
        )
        # The process-global resolver can be reached by concurrent request and
        # graph loops on different threads. WeakKeyDictionary itself is not
        # thread-safe, so only registry access uses a regular lock; adapter
        # construction remains guarded by a loop-local asyncio.Lock.
        self._loop_caches_lock = threading.Lock()

    async def resolve(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> Any:
        """Precedence: explicit connection_id > project-scoped row > global row >
        static DEV_CONNECTIONS fallback."""
        adapter, _resolved_id = await self.resolve_with_connection_id(
            kind, connection_id=connection_id, project_id=project_id
        )
        return adapter

    async def resolve_with_connection_id(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
        *,
        expected_provider: str | None = None,
        options_overlay: dict[str, Any] | None = None,
    ) -> tuple[Any, str]:
        """Resolve an adapter and return the durable connection id actually used.

        ``options_overlay`` supports validated per-run knobs without replacing
        stored connection fields such as URL, project/domain, or secret_ref. The
        connection id remains the persisted base id so later abort and artifact
        reads can resolve the same resource. ``expected_provider`` prevents an
        engine selector from silently executing against a different default.
        """
        stored = await self._select_stored(kind, connection_id, project_id)
        if stored is not None:
            conn = stored.config
            version = stored.updated_at
            internal_project_id = stored.project_id
        else:
            if self._store is not None and get_settings().is_locked_down:
                raise RuntimeError(
                    f"no enabled persisted connection available for {kind.value!r}; "
                    "static development fallbacks are disabled"
                )
            conn = self._select_static(kind, connection_id)
            version = None
            internal_project_id = None
        if expected_provider is not None and conn.provider != expected_provider:
            raise ValueError(
                f"connection {conn.id!r} uses provider {conn.provider!r}, "
                f"not requested provider {expected_provider!r}"
            )
        _validate_internal_connection_binding(
            kind,
            provider=conn.provider,
            internal_project_id=internal_project_id,
            requested_project_id=project_id,
        )
        cache_key: str | None = None
        if options_overlay:
            forbidden_overlay_keys = sorted(
                key
                for key in options_overlay
                if key in {"base_url", "endpoint"} or key.startswith("_apex_")
            )
            if forbidden_overlay_keys:
                raise ValueError(
                    "connection options overlay cannot change network target or trust policy: "
                    + ", ".join(forbidden_overlay_keys)
                )
            conn = conn.model_copy(update={"options": {**conn.options, **options_overlay}})
            digest = hashlib.sha256(
                json.dumps(options_overlay, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:16]
            cache_key = f"{conn.id}:overlay:{digest}"
        adapter = await self._build_cached(conn, version, cache_key=cache_key)
        _expose_internal_connection_binding(adapter, internal_project_id)
        return adapter, conn.id

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
            if get_settings().is_locked_down:
                raise RuntimeError(
                    f"connection store unavailable while resolving {kind.value!r}"
                ) from exc
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

    async def _build_cached(
        self,
        conn: ConnectionConfig,
        version: datetime | None,
        *,
        cache_key: str | None = None,
    ) -> Any:
        key = cache_key or conn.id
        cache = self._cache_for_current_loop()
        lock = cache.locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = cache.instances.get(key)
            if cached is not None and cached[0] == version:
                return cached[1]
            secrets: SecretsPort | None = None
            if conn.secret_ref is not None and conn.kind is not PortKind.SECRETS:
                secrets = await self.resolve(PortKind.SECRETS)
            validate_connection_config(conn)
            _ensure_builtin_adapters_registered()
            adapter = await AdapterRegistry.build(conn, secrets)
            cache.instances[key] = (version, adapter)
            if cached is not None:
                await close_adapter(cached[1])
            return adapter

    async def close(self) -> None:
        """Close adapters owned by the calling loop.

        A client must be closed on the loop that owns its network streams.  A
        resolver used from multiple loops should therefore be closed once by
        each loop (the async-context-manager form makes that straightforward).
        """

        loop = asyncio.get_running_loop()
        with self._loop_caches_lock:
            cache = self._loop_caches.pop(loop, None)
        if cache is None:
            return
        instances = list(cache.instances.values())
        cache.instances.clear()
        cache.locks.clear()
        for _, adapter in instances:
            await close_adapter(adapter)

    async def __aenter__(self) -> "ConnectionResolver":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _cache_for_current_loop(self) -> _LoopCache:
        loop = asyncio.get_running_loop()
        with self._loop_caches_lock:
            cache = self._loop_caches.get(loop)
            if cache is None:
                cache = _LoopCache(instances={}, locks={})
                self._loop_caches[loop] = cache
            return cache


async def close_adapter(adapter: Any) -> None:
    """Close any adapter exposing an async or sync close method."""
    close = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
    if close is None:
        return
    result = close()
    if isawaitable(result):
        await result


def _validate_internal_connection_binding(
    kind: PortKind,
    *,
    provider: str,
    internal_project_id: str | None,
    requested_project_id: str | None,
) -> None:
    """Bind real tracker connections by APEX ownership, not external project name."""
    if kind is not PortKind.WORK_TRACKING or requested_project_id is None:
        return
    if provider in {"stub", "fake"}:
        return
    if (
        internal_project_id is None
        or internal_project_id.casefold() != requested_project_id.casefold()
    ):
        raise ValueError(
            "real work-tracking connection is not internally bound to the requested "
            f"APEX project: bound={internal_project_id!r}, requested={requested_project_id!r}"
        )


def _expose_internal_connection_binding(adapter: Any, internal_project_id: str | None) -> None:
    """Expose persisted APEX ownership for router defense-in-depth checks."""

    try:
        setattr(adapter, INTERNAL_PROJECT_BINDING_ATTR, internal_project_id)
    except (AttributeError, TypeError) as exc:
        raise TypeError("resolved adapter cannot expose its internal APEX project binding") from exc


def internal_project_binding(adapter: Any) -> str | None:
    value = getattr(adapter, INTERNAL_PROJECT_BINDING_ATTR, None)
    return str(value) if value is not None else None


def _ensure_builtin_adapters_registered() -> None:
    """Import adapter factories lazily to avoid services/adapters import cycles."""
    from apex.adapters import register_builtin_adapters

    register_builtin_adapters()


@lru_cache
def get_connection_resolver() -> ConnectionResolver:
    """Process-wide resolver: DB rows first, DEV_CONNECTIONS static fallback."""
    return ConnectionResolver(store=DbConnectionStore())
