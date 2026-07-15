"""Admin connections CRUD (`/admin/connections`) — runtime adapter configuration.

Every route is admin-only (router-level require_role). Secret-bearing and
SecretsPort connections are platform-admin-only: a project-scoped admin could
otherwise point a project adapter at an attacker-controlled endpoint and make
the server resolve and transmit a platform-held secret during a probe/runtime
call. The `secret_ref` column stores references only, never raw secret values.

`POST /{id}/test` builds the adapter exactly as the runtime resolver would and
runs one cheap, read-only, stub-safe probe call per port kind; failures are
reported inline as {ok: false, detail} with HTTP 200 — never a 5xx.
"""

import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters import register_builtin_adapters
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.app.dependencies import require_role
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
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
from apex.services.connections import (
    TRUSTED_PRIVATE_HOST_OPTION,
    close_adapter,
    connection_config_from_row,
    get_connection_resolver,
    validate_adapter_base_url,
    validate_connection_config,
)

# This router validates provider names directly against the registry, so it
# must establish the same built-in registry state as the runtime resolver even
# when imported in isolation (tests, scripts, or OpenAPI generation).
register_builtin_adapters()

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
AdminIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.ADMIN))]


# ── schemas ──────────────────────────────────────────────────────────────────


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: PortKind
    provider: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    project_id: str | None = None  # null = global (any project may resolve it)
    base_url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None  # reference string only, e.g. "env:NAME"

    @field_validator("options")
    @classmethod
    def reject_raw_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_raw_secret_options(value)
        return value

    @field_validator("secret_ref")
    @classmethod
    def validate_secret_ref(cls, value: str | None) -> str | None:
        return _validate_secret_ref(value)


class ConnectionUpdate(BaseModel):
    """`kind` is immutable — create a new connection to change port kinds."""

    model_config = ConfigDict(extra="forbid")
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    project_id: str | None = None
    base_url: str | None = None
    options: dict[str, Any] | None = None
    secret_ref: str | None = None

    @field_validator("provider", "name")
    @classmethod
    def reject_null_required_fields(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @field_validator("options")
    @classmethod
    def reject_raw_secrets(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            raise ValueError("options cannot be null")
        _reject_raw_secret_options(value)
        return value

    @field_validator("secret_ref")
    @classmethod
    def validate_secret_ref(cls, value: str | None) -> str | None:
        return _validate_secret_ref(value)


def _validate_secret_ref(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 1024 or not re.fullmatch(r"(?:env|vault|file):[A-Za-z0-9_./:-]+", value):
        raise ValueError("secret_ref must use an approved env:, vault:, or file: reference")
    return value


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


def _reject_raw_secret_options(options: dict[str, Any]) -> None:
    secret_names = {
        "password",
        "token",
        "secret",
        "secretkey",
        "apikey",
        "clientsecret",
        "bearertoken",
        "privatekey",
        "credential",
    }

    def walk(value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
                if normalized in secret_names or any(
                    marker in normalized
                    for marker in (
                        "password",
                        "token",
                        "secret",
                        "apikey",
                        "credential",
                        "authorization",
                    )
                ):
                    return True
                if walk(nested):
                    return True
        elif isinstance(value, list):
            return any(walk(item) for item in value)
        return False

    if walk(options):
        raise ValueError("connection secrets must be supplied through secret_ref")


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
    key = f".apex-probes/{uuid4().hex}"
    artifact = await adapter.put(key, b"probe", content_type="text/plain")
    try:
        url = await adapter.get_url(artifact.key)
        return f"artifact round-trip ok: {url}"
    finally:
        await adapter.delete(artifact.key)


async def _probe_secrets(adapter: Any) -> str:
    # There is deliberately no universal probe secret. Resolving PATH violates
    # the locked-down integration prefix and probing an operator secret would
    # create an unnecessary access. Successful construction validates provider
    # configuration without reading secret material.
    return f"secrets adapter initialized: {adapter.__class__.__name__}"


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

_RUNTIME_IDENTITY_FIELDS = frozenset(
    {"provider", "project_id", "base_url", "options", "secret_ref"}
)


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


def _validate_connection_target(base_url: str | None, options: dict[str, Any] | None) -> None:
    connection_options = options or {}
    allow_private = connection_options.get(TRUSTED_PRIVATE_HOST_OPTION) is True
    for raw_url in (base_url, connection_options.get("base_url")):
        try:
            validate_adapter_base_url(raw_url, allow_private_hosts=allow_private or None)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    endpoint = connection_options.get("endpoint")
    if endpoint is not None:
        raw_endpoint = str(endpoint).strip()
        if "://" not in raw_endpoint:
            scheme = "https" if connection_options.get("secure") is True else "http"
            raw_endpoint = f"{scheme}://{raw_endpoint}"
        try:
            validate_adapter_base_url(
                raw_endpoint,
                allow_private_hosts=allow_private or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _get_or_404(repo: ConnectionsRepository, connection_id: str) -> Connection:
    conn = await repo.get(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"connection {connection_id!r} not found")
    return conn


def _can_manage_connection(
    identity: ConsumerIdentity,
    project_id: str | None,
    *,
    kind: PortKind | str | None = None,
    secret_ref: str | None = None,
    options: dict[str, Any] | None = None,
) -> bool:
    if identity.is_unscoped:
        return True
    if (
        secret_ref is not None
        or (kind is not None and PortKind(kind) is PortKind.SECRETS)
        or (options or {}).get(TRUSTED_PRIVATE_HOST_OPTION) is True
    ):
        return False
    return project_id is not None and identity.contains_scope(ScopeRef(project_id=project_id))


def _ensure_can_manage_connection(
    identity: ConsumerIdentity,
    project_id: str | None,
    *,
    kind: PortKind | str | None = None,
    secret_ref: str | None = None,
    options: dict[str, Any] | None = None,
) -> None:
    if not _can_manage_connection(
        identity,
        project_id,
        kind=kind,
        secret_ref=secret_ref,
        options=options,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Secret-bearing, secrets-port, global, and out-of-scope connections "
                "require an unscoped platform admin"
            ),
        )


def _ensure_can_manage_row(identity: ConsumerIdentity, conn: Connection) -> None:
    _ensure_can_manage_connection(
        identity,
        conn.project_id,
        kind=conn.kind,
        secret_ref=conn.secret_ref,
        options=conn.options,
    )


def _ensure_options_are_mutable_by(identity: ConsumerIdentity, options: dict[str, Any]) -> None:
    if not identity.is_unscoped and any(str(key).startswith("_apex_") for key in options):
        raise HTTPException(
            status_code=403,
            detail="Reserved _apex_ connection options require an unscoped platform admin",
        )


def _validate_probe_target(config: ConnectionConfig) -> None:
    """Block admin probes from reaching private hosts unless local dev opts in."""

    validate_connection_config(config)


def _protect_runtime_identity(conn: Connection, changes: dict[str, Any]) -> None:
    """Keep durable engine/artifact handles bound to one immutable endpoint."""

    if PortKind(conn.kind) is not PortKind.ARTIFACT_STORE:
        return
    changed = sorted(
        field
        for field in _RUNTIME_IDENTITY_FIELDS.intersection(changes)
        if changes[field] != getattr(conn, field)
    )
    if changed:
        raise HTTPException(
            status_code=409,
            detail=(
                "runtime connection identity fields are immutable once a connection id is "
                f"created ({', '.join(changed)}); create a new connection id instead"
            ),
        )


async def _protect_durable_references(repo: ConnectionsRepository, conn: Connection) -> None:
    checker = getattr(repo, "durable_reference_reason", None)
    if checker is None:
        return
    reason = await checker(conn)
    if reason is not None:
        raise HTTPException(
            status_code=409,
            detail=f"connection is still referenced by {reason}; migrate references first",
        )


def _probe_failure_detail(exc: Exception) -> str:
    if isinstance(exc, (KeyError, ValueError)):
        return str(exc.args[0]) if exc.args else exc.__class__.__name__
    return "connection probe failed; check server logs for details"


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("", operation_id="listConnections")
async def list_connections(
    identity: AdminIdentity,
    repo: ConnectionsRepo,
    kind: PortKind | None = None,
    project: str | None = None,
) -> list[ConnectionOut]:
    rows = await repo.list_connections(
        kind=kind.value if kind is not None else None, project=project
    )
    rows = [
        row
        for row in rows
        if _can_manage_connection(
            identity,
            row.project_id,
            kind=row.kind,
            secret_ref=row.secret_ref,
            options=row.options,
        )
    ]
    return [ConnectionOut.model_validate(row) for row in rows]


@router.post("", operation_id="createConnection", status_code=201)
async def create_connection(
    body: ConnectionCreate, identity: AdminIdentity, repo: ConnectionsRepo
) -> ConnectionOut:
    _ensure_can_manage_connection(
        identity,
        body.project_id,
        kind=body.kind,
        secret_ref=body.secret_ref,
        options=body.options,
    )
    _ensure_options_are_mutable_by(identity, body.options)
    _validate_provider(body.kind, body.provider)
    _validate_connection_target(body.base_url, body.options)
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
async def get_connection(
    connection_id: str, identity: AdminIdentity, repo: ConnectionsRepo
) -> ConnectionOut:
    conn = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    return ConnectionOut.model_validate(conn)


@router.patch("/{connection_id}", operation_id="updateConnection")
async def update_connection(
    connection_id: str, body: ConnectionUpdate, identity: AdminIdentity, repo: ConnectionsRepo
) -> ConnectionOut:
    conn = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    changes = body.model_dump(exclude_unset=True)
    if not identity.is_unscoped and changes.get("secret_ref") is not None:
        raise HTTPException(
            status_code=403,
            detail="Only an unscoped platform admin can attach a connection secret",
        )
    if "options" in changes:
        _ensure_options_are_mutable_by(identity, changes["options"] or {})
    if "project_id" in changes:
        _ensure_can_manage_connection(
            identity,
            changes["project_id"],
            kind=conn.kind,
            secret_ref=conn.secret_ref,
            options=conn.options,
        )
    if "provider" in changes:
        _validate_provider(PortKind(conn.kind), changes["provider"])
    _protect_runtime_identity(conn, changes)
    if PortKind(conn.kind) is PortKind.EXECUTION_ENGINE and any(
        field in changes and changes[field] != getattr(conn, field)
        for field in _RUNTIME_IDENTITY_FIELDS
    ):
        await _protect_durable_references(repo, conn)
    _validate_connection_target(
        changes.get("base_url", conn.base_url),
        changes.get("options", conn.options),
    )
    name = changes.get("name", conn.name)
    try:
        conn = await repo.update(conn, changes)
    except DuplicateConnectionNameError:
        raise HTTPException(
            status_code=409, detail=f"connection name {name!r} already exists"
        ) from None
    return ConnectionOut.model_validate(conn)


@router.delete("/{connection_id}", operation_id="deleteConnection", status_code=204)
async def delete_connection(
    connection_id: str, identity: AdminIdentity, repo: ConnectionsRepo
) -> None:
    conn = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    await _protect_durable_references(repo, conn)
    await repo.delete(conn)


@router.post("/{connection_id}/enable", operation_id="enableConnection")
async def enable_connection(
    connection_id: str, identity: AdminIdentity, repo: ConnectionsRepo
) -> ConnectionOut:
    conn = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    return ConnectionOut.model_validate(await repo.set_enabled(conn, True))


@router.post("/{connection_id}/disable", operation_id="disableConnection")
async def disable_connection(
    connection_id: str, identity: AdminIdentity, repo: ConnectionsRepo
) -> ConnectionOut:
    conn = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    await _protect_durable_references(repo, conn)
    return ConnectionOut.model_validate(await repo.set_enabled(conn, False))


@router.get("/{connection_id}/host-mappings", operation_id="getHostMappings")
async def get_host_mappings(
    connection_id: str, identity: AdminIdentity, repo: ConnectionsRepo
) -> list[HostMappingOut]:
    conn = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    return [HostMappingOut.model_validate(m) for m in conn.host_mappings]


@router.put("/{connection_id}/host-mappings", operation_id="putHostMappings")
async def put_host_mappings(
    connection_id: str, body: list[HostMappingIn], identity: AdminIdentity, repo: ConnectionsRepo
) -> list[HostMappingOut]:
    """Replaces the FULL mapping list (PUT semantics)."""
    conn = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    conn = await repo.replace_host_mappings(conn, [m.model_dump() for m in body])
    return [HostMappingOut.model_validate(m) for m in conn.host_mappings]


@router.post("/{connection_id}/test", operation_id="testConnection")
async def test_connection(
    connection_id: str, identity: AdminIdentity, repo: ConnectionsRepo
) -> ProbeResult:
    """Build the adapter exactly as the resolver would and run the kind's probe.

    Always 200: failures (bad secret_ref, unreachable backend, misconfigured
    options) come back inline as ok=false so the admin UI can show them.
    """
    row = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, row)
    started = time.perf_counter()
    adapter: Any | None = None
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
    finally:
        if adapter is not None:
            try:
                await close_adapter(adapter)
            except Exception as exc:
                detail = _probe_failure_detail(exc)
                ok = False
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return ProbeResult(ok=ok, latency_ms=latency_ms, detail=detail)
