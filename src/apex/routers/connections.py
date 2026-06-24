"""Admin connections CRUD (`/admin/connections`) — runtime adapter configuration.

Every route is admin-only (router-level require_role). The `secret_ref` column
is a REFERENCE string ("env:NAME", "vault:path#key", ...) resolved through the
secrets port at adapter-build time — raw secret values never enter the table,
so returning secret_ref to admin clients is safe by design.

`POST /{id}/test` builds the adapter exactly as the runtime resolver would and
runs one cheap, read-only, stub-safe probe call per port kind; failures are
reported inline as {ok: false, detail} with HTTP 200 — never a 5xx.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from ipaddress import ip_address
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.app.dependencies import require_role
from apex.auth.identity import Role
from apex.domain.integrations import (
    DocScope,
    EnvRef,
    LoadTestSpec,
    LogQuery,
    Page,
    RepoRef,
    TimeWindow,
)
from apex.persistence.db import get_session
from apex.persistence.models import Connection
from apex.persistence.repositories.connections import (
    ConnectionsRepository,
    DuplicateConnectionNameError,
)
from apex.ports.secrets import SecretsPort
from apex.services.connections import connection_config_from_row, get_connection_resolver
from apex.settings import get_settings

router = APIRouter(
    prefix="/admin/connections",
    tags=["admin-connections"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


def get_connections_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionsRepository:
    return ConnectionsRepository(session)


ConnectionsRepo = Annotated[ConnectionsRepository, Depends(get_connections_repository)]


# ── schemas ──────────────────────────────────────────────────────────────────


class ConnectionCreate(BaseModel):
    kind: PortKind
    provider: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    project_id: str | None = None  # null = global (any project may resolve it)
    base_url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None  # reference string only, e.g. "env:NAME"


class ConnectionUpdate(BaseModel):
    """`kind` is immutable — create a new connection to change port kinds."""

    provider: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    project_id: str | None = None
    base_url: str | None = None
    options: dict[str, Any] | None = None
    secret_ref: str | None = None


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: PortKind
    provider: str
    name: str
    project_id: str | None
    base_url: str | None
    options: dict[str, Any]
    secret_ref: str | None  # reference string, never a raw secret
    enabled: bool
    created_at: datetime
    updated_at: datetime


class HostMappingIn(BaseModel):
    pattern: str = Field(min_length=1, max_length=1024)
    target: str = Field(min_length=1, max_length=1024)
    enabled: bool = True


class HostMappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    pattern: str
    target: str
    enabled: bool


class ProbeResult(BaseModel):
    ok: bool
    latency_ms: float
    detail: str


# ── probe calls: one cheap, read-only, stub-safe call per port kind ─────────


async def _probe_work_tracking(adapter: Any) -> str:
    item = await adapter.get_item("PHX-241")
    return f"fetched work item {item.key}"


async def _probe_log_search(adapter: Any) -> str:
    result = await adapter.search(LogQuery(query="*"), window=TimeWindow(), page=Page(limit=1))
    return f"log search returned {result.total} entries"


async def _probe_observability(adapter: Any) -> str:
    health = await adapter.get_service_health("checkout", window=TimeWindow())
    return f"service health: {health.status}"


async def _probe_documents(adapter: Any) -> str:
    hits = await adapter.search("checkout", scope=DocScope(), k=1)
    return f"document search returned {len(hits)} hits"


async def _probe_cluster_inventory(adapter: Any) -> str:
    snapshot = await adapter.scan_environment(EnvRef(id="connection-probe", name="probe"))
    return f"environment scan found {len(snapshot.services)} services"


async def _probe_source_control(adapter: Any) -> str:
    file = await adapter.get_file(RepoRef(name="connection-probe"), "README.md")
    return f"fetched {file.path} ({len(file.text)} chars)"


async def _probe_execution_engine(adapter: Any) -> str:
    report = await adapter.validate(
        LoadTestSpec(title="connection probe", vusers=1, ramp_s=0, duration_s=1)
    )
    return f"spec validation ok={report.ok}"


async def _probe_artifact_store(adapter: Any) -> str:
    artifact = await adapter.put("apex-connection-probe", b"probe", content_type="text/plain")
    url = await adapter.get_url(artifact.key)
    return f"artifact round-trip ok: {url}"


async def _probe_secrets(adapter: Any) -> str:
    await adapter.resolve("env:PATH")  # value is never returned or logged
    return "resolved secret_ref env:PATH"


PROBE_CALLS: dict[PortKind, Callable[[Any], Awaitable[str]]] = {
    PortKind.WORK_TRACKING: _probe_work_tracking,
    PortKind.LOG_SEARCH: _probe_log_search,
    PortKind.OBSERVABILITY: _probe_observability,
    PortKind.DOCUMENTS: _probe_documents,
    PortKind.CLUSTER_INVENTORY: _probe_cluster_inventory,
    PortKind.SOURCE_CONTROL: _probe_source_control,
    PortKind.EXECUTION_ENGINE: _probe_execution_engine,
    PortKind.ARTIFACT_STORE: _probe_artifact_store,
    PortKind.SECRETS: _probe_secrets,
}


# ── helpers ──────────────────────────────────────────────────────────────────


def _validate_provider(kind: PortKind, provider: str) -> None:
    registered = AdapterRegistry.providers_for(kind)
    if provider not in registered:
        hint = ", ".join(registered) if registered else "none"
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown provider {provider!r} for kind '{kind.value}'; "
                f"registered providers: {hint}"
            ),
        )


async def _get_or_404(repo: ConnectionsRepository, connection_id: str) -> Connection:
    conn = await repo.get(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"connection {connection_id!r} not found")
    return conn


def _validate_probe_target(config: ConnectionConfig) -> None:
    """Block admin probes from reaching private hosts unless local dev opts in."""

    if get_settings().allow_private_adapter_hosts:
        return
    raw_url = config.options.get("base_url")
    if raw_url is None:
        return
    parsed = urlsplit(str(raw_url))
    host = parsed.hostname
    if host is None:
        return
    normalized = host.lower()
    if normalized in {"localhost", "localhost.localdomain"}:
        raise ValueError("private adapter hosts are disabled")
    try:
        address = ip_address(normalized)
    except ValueError:
        return
    if address.is_private or address.is_loopback or address.is_link_local:
        raise ValueError("private adapter hosts are disabled")


def _probe_failure_detail(exc: Exception) -> str:
    if isinstance(exc, (KeyError, ValueError)):
        return str(exc.args[0]) if exc.args else exc.__class__.__name__
    return "connection probe failed; check server logs for details"


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("", operation_id="listConnections")
async def list_connections(
    repo: ConnectionsRepo, kind: PortKind | None = None, project: str | None = None
) -> list[ConnectionOut]:
    rows = await repo.list_connections(
        kind=kind.value if kind is not None else None, project=project
    )
    return [ConnectionOut.model_validate(row) for row in rows]


@router.post("", operation_id="createConnection", status_code=201)
async def create_connection(body: ConnectionCreate, repo: ConnectionsRepo) -> ConnectionOut:
    _validate_provider(body.kind, body.provider)
    try:
        conn = await repo.create(
            kind=body.kind.value,
            provider=body.provider,
            name=body.name,
            project_id=body.project_id,
            base_url=body.base_url,
            options=body.options,
            secret_ref=body.secret_ref,
        )
    except DuplicateConnectionNameError:
        raise HTTPException(
            status_code=409, detail=f"connection name {body.name!r} already exists"
        ) from None
    return ConnectionOut.model_validate(conn)


@router.get("/{connection_id}", operation_id="getConnection")
async def get_connection(connection_id: str, repo: ConnectionsRepo) -> ConnectionOut:
    return ConnectionOut.model_validate(await _get_or_404(repo, connection_id))


@router.patch("/{connection_id}", operation_id="updateConnection")
async def update_connection(
    connection_id: str, body: ConnectionUpdate, repo: ConnectionsRepo
) -> ConnectionOut:
    conn = await _get_or_404(repo, connection_id)
    changes = body.model_dump(exclude_unset=True)
    if "provider" in changes:
        _validate_provider(PortKind(conn.kind), changes["provider"])
    name = changes.get("name", conn.name)
    try:
        conn = await repo.update(conn, changes)
    except DuplicateConnectionNameError:
        raise HTTPException(
            status_code=409, detail=f"connection name {name!r} already exists"
        ) from None
    return ConnectionOut.model_validate(conn)


@router.delete("/{connection_id}", operation_id="deleteConnection", status_code=204)
async def delete_connection(connection_id: str, repo: ConnectionsRepo) -> None:
    await repo.delete(await _get_or_404(repo, connection_id))


@router.post("/{connection_id}/enable", operation_id="enableConnection")
async def enable_connection(connection_id: str, repo: ConnectionsRepo) -> ConnectionOut:
    conn = await _get_or_404(repo, connection_id)
    return ConnectionOut.model_validate(await repo.set_enabled(conn, True))


@router.post("/{connection_id}/disable", operation_id="disableConnection")
async def disable_connection(connection_id: str, repo: ConnectionsRepo) -> ConnectionOut:
    conn = await _get_or_404(repo, connection_id)
    return ConnectionOut.model_validate(await repo.set_enabled(conn, False))


@router.get("/{connection_id}/host-mappings", operation_id="getHostMappings")
async def get_host_mappings(connection_id: str, repo: ConnectionsRepo) -> list[HostMappingOut]:
    conn = await _get_or_404(repo, connection_id)
    return [HostMappingOut.model_validate(m) for m in conn.host_mappings]


@router.put("/{connection_id}/host-mappings", operation_id="putHostMappings")
async def put_host_mappings(
    connection_id: str, body: list[HostMappingIn], repo: ConnectionsRepo
) -> list[HostMappingOut]:
    """Replaces the FULL mapping list (PUT semantics)."""
    conn = await _get_or_404(repo, connection_id)
    conn = await repo.replace_host_mappings(conn, [m.model_dump() for m in body])
    return [HostMappingOut.model_validate(m) for m in conn.host_mappings]


@router.post("/{connection_id}/test", operation_id="testConnection")
async def test_connection(connection_id: str, repo: ConnectionsRepo) -> ProbeResult:
    """Build the adapter exactly as the resolver would and run the kind's probe.

    Always 200: failures (bad secret_ref, unreachable backend, misconfigured
    options) come back inline as ok=false so the admin UI can show them.
    """
    row = await _get_or_404(repo, connection_id)
    started = time.perf_counter()
    try:
        config = connection_config_from_row(row)
        _validate_probe_target(config)
        secrets: SecretsPort | None = None
        if config.secret_ref is not None and config.kind is not PortKind.SECRETS:
            secrets = await get_connection_resolver().resolve(PortKind.SECRETS)
        adapter = await AdapterRegistry.build(config, secrets)
        detail = await PROBE_CALLS[config.kind](adapter)
        ok = True
    except Exception as exc:  # probe must report failures inline, never raise
        detail = _probe_failure_detail(exc)
        ok = False
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return ProbeResult(ok=ok, latency_ms=latency_ms, detail=detail)
