"""Connection resolution: (port kind, connection id) -> live adapter instance.

M2: resolution consults the `connections` table first (explicit connection_id >
project-scoped row > global row) and falls back to the static in-code
DEV_CONNECTIONS map — so `langgraph dev` keeps working without Postgres and DB
outages degrade gracefully (logged warning, stub adapters). Adapter instances
are cached keyed by (connection_id, runtime_version), so runtime-affecting edits
invalidate on the next resolve while metadata-only edits do not. The resolve()
surface is unchanged from M1 — graph nodes and
routers must depend on it, never on the AdapterRegistry directly.

Note: ConnectionConfig has no base_url field, so a row's base_url is merged into
options["base_url"] for the adapter factory.
"""

import asyncio
import hashlib
import json
import threading
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from inspect import isawaitable, iscoroutinefunction
from ipaddress import ip_address
from typing import Any, Protocol
from urllib.parse import urlsplit

import structlog
from sqlalchemy import case, or_, select
from sqlalchemy.exc import SQLAlchemyError

from apex.adapters.options import coerce_bool, normalize_host_port_endpoint
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.diagnostics import bounded_diagnostic
from apex.ports.secrets import SecretsPort
from apex.services.connection_credentials import (
    connection_options_require_repair,
    reject_raw_secret_options,
    validate_secret_ref,
)
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
NETWORK_TRUST_OPTION_KEYS = frozenset({"base_url", "endpoint", "secure", "verify_tls"})


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
    runtime_version: datetime | None


@dataclass(frozen=True)
class ResolvedAdapter:
    """Adapter plus immutable metadata from the resolution transaction.

    Runtime code must not depend on setting private attributes on third-party
    adapters: slot-based implementations can reject them. The persisted version is
    carried alongside the adapter so callers can atomically reserve its connection.
    """

    adapter: Any
    connection_id: str
    connection_version: datetime | None
    persisted: bool

    def __getattr__(self, name: str) -> Any:
        """Forward port calls while preserving compatibility with adapter consumers."""

        return getattr(self.adapter, name)


@dataclass(frozen=True)
class ResolvedConnectionMetadata:
    """Selected connection identity/configuration before secrets or adapter construction."""

    config: ConnectionConfig
    internal_project_id: str | None
    connection_version: datetime | None
    persisted: bool


class _ManagedAdapter:
    """One cached generation, closed exactly when its last port call releases."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.active_calls = 0
        self.retired = False
        self.closed = False
        self._closing = False

    def checkout(self) -> "_AdapterProxy":
        self.acquire()
        return _AdapterProxy(self, asyncio.get_running_loop())

    def acquire(self, *, allow_retired: bool = False) -> None:
        if self.closed or self._closing or (self.retired and not allow_retired):
            raise RuntimeError("adapter generation is already closed")
        self.active_calls += 1

    async def release(self) -> None:
        if self.active_calls <= 0:
            raise RuntimeError("adapter lease released without an active call")
        self.active_calls -= 1
        await self._close_if_idle()

    def release_later(self, loop: asyncio.AbstractEventLoop) -> None:
        """Release a GC-abandoned checkout without awaiting from ``__del__``."""

        if self.active_calls <= 0:
            return
        self.active_calls -= 1
        if not self.retired or self.active_calls or self.closed or self._closing:
            return
        try:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._close_if_idle()))
        except RuntimeError:
            # The owning loop is already gone; its network clients cannot be
            # closed safely from another loop during interpreter teardown.
            pass

    async def retire(self) -> None:
        self.retired = True
        await self._close_if_idle()

    async def invoke(
        self,
        method: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        allow_retired: bool,
    ) -> Any:
        self.acquire(allow_retired=allow_retired)
        try:
            return await method(*args, **kwargs)
        finally:
            await self.release()

    def await_result(self, result: Any, *, checkout: "_AdapterProxy") -> Any:
        async def wait() -> Any:
            checkout._ensure_open()
            self.acquire(allow_retired=True)
            try:
                return await result
            finally:
                await self.release()

        return wait()

    async def release_checkout(self) -> None:
        """Release one proxy's lifetime lease."""

        await self.release()

    async def _close_if_idle(self) -> None:
        if not self.retired or self.active_calls or self.closed or self._closing:
            return
        self._closing = True
        try:
            await close_adapter(self.adapter)
        except Exception as exc:
            logger.warning(
                "apex.connections.retired_adapter_close_failed",
                adapter_type=self.adapter.__class__.__name__,
                error=bounded_diagnostic(exc),
            )
        finally:
            self.closed = True
            self._closing = False


class _AdapterProxy:
    """Port-transparent proxy that leases a generation for each async call."""

    def __init__(self, managed: _ManagedAdapter, loop: asyncio.AbstractEventLoop) -> None:
        self._managed = managed
        self._loop = loop
        self._released = False

    @property
    def __class__(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
    ) -> type[Any]:
        """Preserve concrete and runtime-checkable protocol compatibility.

        Resolver callers historically received the adapter itself and some
        safety boundaries use ``isinstance`` against runtime-checkable port
        protocols.  Advertising the wrapped class keeps those checks truthful
        while calls still pass through the generation lease.
        """

        return self._managed.adapter.__class__

    def _ensure_open(self) -> None:
        if self._released:
            raise RuntimeError("adapter checkout has already been released")

    def __getattr__(self, name: str) -> Any:
        self._ensure_open()
        attribute = getattr(self._managed.adapter, name)
        if not callable(attribute):
            return attribute
        if iscoroutinefunction(attribute):

            async def invoke_async(*args: Any, **kwargs: Any) -> Any:
                self._ensure_open()
                return await self._managed.invoke(
                    attribute,
                    args,
                    kwargs,
                    allow_retired=True,
                )

            return invoke_async

        def invoke_sync(*args: Any, **kwargs: Any) -> Any:
            self._ensure_open()
            result = attribute(*args, **kwargs)
            if hasattr(result, "__aiter__"):
                return _LeasedAsyncIterator(self._managed, result, self._loop)
            if isawaitable(result):
                return self._managed.await_result(result, checkout=self)
            return result

        return invoke_sync

    async def aclose(self) -> None:
        """Explicit proxy close retires this generation without racing callers."""

        if self._released:
            return
        self._released = True
        await self._managed.release_checkout()
        await self._managed.retire()

    def __del__(self) -> None:
        if self.__dict__.get("_released", True):
            return
        self._released = True
        self._managed.release_later(self._loop)


class _LeasedAsyncIterator:
    """Hold one adapter lease from creation through exhaustion/explicit close."""

    def __init__(
        self,
        managed: _ManagedAdapter,
        iterator: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._managed = managed
        self._iterator = iterator.__aiter__()
        self._loop = loop
        self._closed = False
        # Acquire synchronously when iter_bytes returns so rotation cannot close
        # the generation in the gap before the first pull.
        self._managed.acquire(allow_retired=True)

    def __aiter__(self) -> "_LeasedAsyncIterator":
        return self

    async def __anext__(self) -> Any:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await anext(self._iterator)
        except StopAsyncIteration:
            await self.aclose()
            raise
        except BaseException:
            try:
                await self.aclose()
            except BaseException:
                pass
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._iterator, "aclose", None)
            if close is not None:
                result = close()
                if isawaitable(result):
                    await result
        finally:
            await self._managed.release()

    def __del__(self) -> None:
        if self.__dict__.get("_closed", True):
            return
        self._closed = True
        managed = self._managed
        iterator = self._iterator
        try:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(_close_abandoned_iterator(managed, iterator))
            )
        except RuntimeError:
            managed.release_later(self._loop)


async def _close_abandoned_iterator(managed: _ManagedAdapter, iterator: Any) -> None:
    try:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            result = close()
            if isawaitable(result):
                await result
    except BaseException:
        pass
    finally:
        await managed.release()


@dataclass
class _LoopCache:
    """Adapter instances and build locks owned by exactly one event loop."""

    instances: dict[str, tuple[datetime | None, _ManagedAdapter]]
    locks: dict[str, asyncio.Lock]


def connection_config_from_row(row: Any) -> ConnectionConfig:
    """Map a Connection ORM row (or anything row-shaped) to a ConnectionConfig.

    base_url is merged into options["base_url"] — ConnectionConfig has no
    base_url field by design (adapters read it from options).
    """
    raw_options = row.options
    if raw_options is None:
        options: dict[str, Any] = {}
    elif isinstance(raw_options, dict):
        options = dict(raw_options)
    else:
        raise ValueError("stored connection options require repair")
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
    """Reject unsafe adapter targets before credentials or provider I/O are used."""

    if raw_url is None:
        return
    if not isinstance(raw_url, str):
        raise ValueError("adapter URL must be a string")
    if not raw_url or raw_url != raw_url.strip() or len(raw_url) > 4_096:
        raise ValueError("adapter URL must be a non-empty http(s) URL")
    if "\\" in raw_url or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw_url):
        raise ValueError("adapter URL contains unsafe characters")

    parsed = urlsplit(raw_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("adapter URL must be an http(s) URL without embedded credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("adapter URL has an invalid port") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("adapter URL has an invalid port")
    settings = get_settings()
    if allow_private_hosts is None:
        allow_private_hosts = getattr(settings, "allow_private_adapter_hosts", False)
    if parsed.scheme == "http" and settings.is_locked_down and not allow_private_hosts:
        raise ValueError(
            "adapter URL must use https in locked environments unless explicitly "
            "approved as a trusted private target"
        )
    if allow_private_hosts:
        return

    normalized = parsed.hostname.rstrip(".").lower()
    if normalized in DENIED_ADAPTER_HOSTS or normalized.endswith(DENIED_ADAPTER_HOST_SUFFIXES):
        raise ValueError("private adapter hosts are disabled")

    # Hostname resolution is deliberately deferred to the connect-time pinned
    # transports. Save/build validation runs on ASGI/graph event loops and must
    # never block them on libc DNS. Numeric destinations can be rejected here.
    try:
        address = ip_address(normalized)
    except ValueError:
        if "." not in normalized:
            # Search-domain expansion makes single-label names both ambiguous and
            # capable of resolving into private infrastructure. Trusted private
            # connections return above and may deliberately use service names.
            raise ValueError("adapter URL host must be fully qualified") from None
        return
    if not address.is_global:
        raise ValueError("private adapter hosts are disabled")


def validate_adapter_transport_options(
    options: dict[str, Any], *, allow_private_hosts: bool | None = None
) -> None:
    """Keep stored adapter options from disabling TLS verification in production."""

    settings = get_settings()
    if allow_private_hosts is None:
        allow_private_hosts = getattr(settings, "allow_private_adapter_hosts", False)
    if (
        settings.is_locked_down
        and not allow_private_hosts
        and "verify_tls" in options
        and not coerce_bool(options.get("verify_tls"), default=True)
    ):
        raise ValueError(
            "adapter TLS verification cannot be disabled in locked environments "
            "unless explicitly approved for a trusted private target"
        )


def validate_connection_config(config: ConnectionConfig) -> None:
    # Treat database rows as untrusted input. Older deployments and direct SQL
    # writers can predate the HTTP/bootstrap validators, so reject raw option
    # credentials and unsupported reference strings before any secret/provider
    # resolution occurs.
    reject_raw_secret_options(config.options)
    validate_secret_ref(config.secret_ref)
    # `endpoint` is the S3/MinIO transport target. It is just as security
    # sensitive as the conventional adapter `base_url` and must not bypass the
    # SSRF policy.
    allow_private = config.options.get(TRUSTED_PRIVATE_HOST_OPTION) is True
    validate_adapter_transport_options(
        config.options,
        allow_private_hosts=allow_private or None,
    )
    validate_adapter_base_url(
        config.options.get("base_url"), allow_private_hosts=allow_private or None
    )
    endpoint = config.options.get("endpoint")
    if endpoint is not None:
        normalized_endpoint, endpoint_secure = normalize_host_port_endpoint(
            endpoint,
            secure=coerce_bool(config.options.get("secure"), default=False),
        )
        scheme = "https" if endpoint_secure else "http"
        validate_adapter_base_url(
            f"{scheme}://{normalized_endpoint}",
            allow_private_hosts=allow_private or None,
        )
    if connection_options_require_repair(config.options):
        raise ValueError("connection options contain unsafe credential-bearing configuration")


def stored_connection_from_row(row: Any) -> StoredConnection:
    return StoredConnection(
        config=connection_config_from_row(row),
        project_id=row.project_id,
        enabled=bool(row.enabled),
        runtime_version=row.runtime_version,
    )


class ConnectionStore(Protocol):
    """Lookup surface the resolver needs; DB-backed in prod, fake in tests."""

    async def get(self, connection_id: str) -> StoredConnection | None: ...

    async def find_default(
        self, kind: PortKind, project_id: str | None
    ) -> StoredConnection | None: ...


def _default_connection_stmt(kind: PortKind, project_id: str | None):  # noqa: ANN202
    """Return one SQL-side project/default selection; never materialize sibling rows."""

    from apex.persistence.models import Connection

    scope_predicate = Connection.project_id.is_(None)
    scope_precedence = None
    if project_id is not None:
        scope_predicate = or_(
            Connection.project_id == project_id,
            Connection.project_id.is_(None),
        )
        scope_precedence = case(
            (Connection.project_id == project_id, 0),
            else_=1,
        )
    stmt = select(Connection).where(
        Connection.kind == kind.value,
        Connection.enabled.is_(True),
        scope_predicate,
    )
    if scope_precedence is not None:
        stmt = stmt.order_by(scope_precedence)
    return stmt.order_by(Connection.runtime_version.desc(), Connection.id).limit(1)


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
        engine, session_factory = _throwaway_session_factory()
        try:
            async with session_factory() as session:
                row = await session.scalar(_default_connection_stmt(kind, project_id))
        finally:
            await engine.dispose()
        return stored_connection_from_row(row) if row is not None else None


class ConnectionResolver:
    """Resolves adapters from connection rows/configs, caching per event loop.

    Cache key per connection id is its semantic runtime_version (None for static
    configs). Runtime-affecting mutations advance it and rebuild the adapter;
    metadata-only edits leave active generations intact.
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
        resolved = await self.resolve_with_metadata(
            kind,
            connection_id=connection_id,
            project_id=project_id,
            expected_provider=expected_provider,
            options_overlay=options_overlay,
        )
        return resolved.adapter, resolved.connection_id

    async def resolve_with_metadata(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
        *,
        expected_provider: str | None = None,
        options_overlay: dict[str, Any] | None = None,
    ) -> ResolvedAdapter:
        """Resolve an adapter with the persisted identity/version used to build it."""

        metadata = await self.resolve_metadata(
            kind,
            connection_id=connection_id,
            project_id=project_id,
            expected_provider=expected_provider,
        )
        return await self.build_from_metadata(metadata, options_overlay=options_overlay)

    async def resolve_metadata(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
        *,
        expected_provider: str | None = None,
    ) -> ResolvedConnectionMetadata:
        """Select and authorize a connection without reading secrets or building an adapter."""

        stored = await self._select_stored(kind, connection_id, project_id)
        if stored is not None:
            conn = stored.config
            version = stored.runtime_version
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
        validate_connection_config(conn)
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
        return ResolvedConnectionMetadata(
            config=conn,
            internal_project_id=internal_project_id,
            connection_version=version,
            persisted=stored is not None,
        )

    async def build_from_metadata(
        self,
        metadata: ResolvedConnectionMetadata,
        *,
        options_overlay: dict[str, Any] | None = None,
    ) -> ResolvedAdapter:
        """Build the adapter generation for previously authorized metadata."""

        conn = metadata.config
        version = metadata.connection_version
        cache_key: str | None = None
        if options_overlay:
            forbidden_overlay_keys = sorted(
                key
                for key in options_overlay
                if key in NETWORK_TRUST_OPTION_KEYS or key.startswith("_apex_")
            )
            if forbidden_overlay_keys:
                raise ValueError(
                    "connection options overlay cannot change network target or trust policy: "
                    + ", ".join(forbidden_overlay_keys)
                )
            conn = conn.model_copy(update={"options": {**conn.options, **options_overlay}})
            validate_connection_config(conn)
            digest = hashlib.sha256(
                json.dumps(options_overlay, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            cache_key = f"{conn.id}:overlay:{digest}"
        adapter = await self._build_cached(conn, version, cache_key=cache_key)
        if conn.kind is PortKind.WORK_TRACKING:
            _expose_internal_connection_binding(adapter, metadata.internal_project_id)
        return ResolvedAdapter(
            adapter=adapter,
            connection_id=conn.id,
            connection_version=version,
            persisted=metadata.persisted,
        )

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
                error=bounded_diagnostic(f"{exc.__class__.__name__}: {bounded_diagnostic(exc)}"),
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
        # Validate before cache lookup as well as before construction. A legacy
        # or out-of-band row repair must never reuse a cached adapter generation
        # while its currently selected configuration is invalid.
        validate_connection_config(conn)
        key = cache_key or conn.id
        cache = self._cache_for_current_loop()
        lock = cache.locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = cache.instances.get(key)
            if (
                cached is not None
                and cached[0] == version
                and not cached[1].retired
                and not cached[1].closed
            ):
                return cached[1].checkout()
            secrets: SecretsPort | None = None
            if conn.secret_ref is not None and conn.kind is not PortKind.SECRETS:
                secrets = await self.resolve(PortKind.SECRETS)
            _ensure_builtin_adapters_registered()
            adapter = await AdapterRegistry.build(conn, secrets)
            managed = _ManagedAdapter(adapter)
            cache.instances[key] = (version, managed)
            if cached is not None:
                await cached[1].retire()
            return managed.checkout()

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
        for _, managed in instances:
            await managed.retire()

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
