"""Admin connections CRUD (`/admin/connections`) — runtime adapter configuration.

Every route is admin-only (router-level require_role). Secret-bearing and
ambient-identity/SecretsPort connections are platform-admin-only: a project-scoped admin could
otherwise point a project adapter at an attacker-controlled endpoint and make
the server resolve and transmit a platform-held secret during a probe/runtime
call. The `secret_ref` column stores references only, never raw secret values.

`POST /{id}/test` builds the adapter exactly as the runtime resolver would and
runs one cheap, read-only, stub-safe probe call per port kind; failures are
reported inline as {ok: false, detail} with HTTP 200 — never a 5xx.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters import register_builtin_adapters
from apex.adapters.options import coerce_bool, normalize_host_port_endpoint
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.app.dependencies import require_role
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.input_limits import (
    MAX_CHILD_ITEMS,
    MAX_DB_LIST_OFFSET,
    NoNulStr,
    RecordId,
    ScopeId,
)
from apex.domain.integrations import (
    DocHit,
    DocScope,
    EnvironmentSnapshot,
    EnvRef,
    FileContent,
    LoadTestSpec,
    LogQuery,
    LogSearchResult,
    Page,
    RepoRef,
    ServiceHealth,
    TimeWindow,
    ValidationReport,
    WorkItem,
)
from apex.persistence.db import get_session, release_read_transactions
from apex.persistence.models import Connection
from apex.persistence.repositories.connections import (
    ConnectionsRepository,
    DuplicateConnectionNameError,
)
from apex.ports.artifact_store import StoredArtifact
from apex.ports.secrets import SecretsPort
from apex.services.connection_credentials import (
    connection_options_require_repair,
    connection_url_requires_repair,
    reject_raw_secret_options,
    sanitize_connection_options_for_output,
    sanitize_connection_url_for_output,
    sanitize_secret_ref_for_output,
    validate_secret_ref,
)
from apex.services.connections import (
    TRUSTED_PRIVATE_HOST_OPTION,
    close_adapter,
    connection_config_from_row,
    get_connection_resolver,
    validate_adapter_base_url,
    validate_adapter_transport_options,
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
ConnectionId = Annotated[RecordId, Path(description="Connection id")]

_KUBERNETES_IN_CLUSTER_AUTH_MODES = frozenset({"in_cluster", "in-cluster", "incluster"})


# ── schemas ──────────────────────────────────────────────────────────────────


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: PortKind
    provider: NoNulStr = Field(min_length=1, max_length=64)
    name: NoNulStr = Field(min_length=1, max_length=255)
    project_id: ScopeId | None = None  # null = global (any project may resolve it)
    base_url: NoNulStr | None = Field(default=None, max_length=1024)
    options: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None  # reference string only, e.g. "env:NAME"

    @field_validator("options")
    @classmethod
    def reject_raw_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        return reject_raw_secret_options(value)

    @field_validator("secret_ref")
    @classmethod
    def validate_secret_ref(cls, value: str | None) -> str | None:
        return validate_secret_ref(value)


class ConnectionUpdate(BaseModel):
    """`kind` is immutable — create a new connection to change port kinds."""

    model_config = ConfigDict(extra="forbid")
    provider: NoNulStr | None = Field(default=None, min_length=1, max_length=64)
    name: NoNulStr | None = Field(default=None, min_length=1, max_length=255)
    project_id: ScopeId | None = None
    base_url: NoNulStr | None = Field(default=None, max_length=1024)
    options: dict[str, Any] = Field(default=None)  # type: ignore[assignment]
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
        return reject_raw_secret_options(value)

    @field_validator("secret_ref")
    @classmethod
    def validate_secret_ref(cls, value: str | None) -> str | None:
        return validate_secret_ref(value)


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, hide_input_in_errors=True)

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

    @field_validator("base_url", mode="before")
    @classmethod
    def sanitize_legacy_base_url(cls, value: Any) -> str | None:
        return sanitize_connection_url_for_output(value)

    @field_validator("options", mode="before")
    @classmethod
    def sanitize_legacy_options(cls, value: Any) -> dict[str, Any]:
        return sanitize_connection_options_for_output(value)

    @field_validator("secret_ref", mode="before")
    @classmethod
    def sanitize_legacy_secret_ref(cls, value: Any) -> str | None:
        return sanitize_secret_ref_for_output(value)


class HostMappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: NoNulStr = Field(min_length=1, max_length=1024)
    target: NoNulStr = Field(min_length=1, max_length=1024)
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
    detail: NoNulStr = Field(max_length=1_024)


# ── probe calls: one cheap, read-only, stub-safe call per port kind ─────────


async def _probe_work_tracking(adapter: Any) -> str:
    item = WorkItem.model_validate(_provider_payload(await adapter.get_item("PHX-241")))
    return f"fetched work item {item.key}"


async def _probe_log_search(adapter: Any) -> str:
    result = LogSearchResult.model_validate(
        _provider_payload(
            await adapter.search(
                LogQuery(query="*").model_copy(deep=True),
                window=TimeWindow().model_copy(deep=True),
                page=Page(limit=1).model_copy(deep=True),
            )
        )
    )
    return f"log search returned {result.total} entries"


async def _probe_observability(adapter: Any) -> str:
    health = ServiceHealth.model_validate(
        _provider_payload(
            await adapter.get_service_health(
                "checkout",
                window=TimeWindow().model_copy(deep=True),
            )
        )
    )
    return f"service health: {health.status}"


async def _probe_documents(adapter: Any) -> str:
    raw_hits = await adapter.search(
        "checkout",
        scope=DocScope().model_copy(deep=True),
        k=1,
    )
    if not isinstance(raw_hits, list) or len(raw_hits) > 1:
        raise RuntimeError("document probe returned an invalid result list")
    hits = [DocHit.model_validate(_provider_payload(hit)) for hit in raw_hits]
    return f"document search returned {len(hits)} hits"


async def _probe_cluster_inventory(adapter: Any) -> str:
    snapshot = EnvironmentSnapshot.model_validate(
        _provider_payload(
            await adapter.scan_environment(
                EnvRef(id="connection-probe", name="probe").model_copy(deep=True)
            )
        )
    )
    return f"environment scan found {len(snapshot.services)} services"


async def _probe_source_control(adapter: Any) -> str:
    file = FileContent.model_validate(
        _provider_payload(
            await adapter.get_file(
                RepoRef(name="connection-probe").model_copy(deep=True),
                "README.md",
            )
        )
    )
    return f"fetched {file.path} ({len(file.text)} chars)"


async def _probe_execution_engine(adapter: Any) -> str:
    spec = LoadTestSpec(title="connection probe", vusers=1, ramp_s=0, duration_s=1)
    report = ValidationReport.model_validate(
        _provider_payload(await adapter.validate(spec.model_copy(deep=True)))
    )
    return f"spec validation ok={report.ok}"


async def _delete_probe_artifact_definitively(
    delete: Callable[[str], Awaitable[None]], key: str
) -> None:
    """Finish cleanup before adapter closure, even when the request is cancelled."""

    async def cleanup() -> None:
        await delete(key)

    task = asyncio.create_task(cleanup(), name="artifact-connection-probe-cleanup")
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    break
                continue
            except BaseException:
                break
        if task.done():
            try:
                task.result()
            except BaseException:
                pass
        raise


async def _probe_artifact_store(adapter: Any) -> str:
    delete = getattr(adapter, "delete", None)
    if not callable(delete):
        raise ValueError("artifact store does not support safe probe cleanup")
    typed_delete = cast(Callable[[str], Awaitable[None]], delete)
    key = f".apex-probes/{uuid4().hex}"
    artifact = StoredArtifact.model_validate(
        _provider_payload(await adapter.put(key, b"probe", content_type="text/plain"))
    )
    try:
        if artifact.key != key or artifact.size != len(b"probe"):
            raise RuntimeError("artifact store probe returned inconsistent object metadata")
        payload = await adapter.get(key)
        if payload != b"probe":
            raise RuntimeError("artifact store probe read did not match the uploaded bytes")
        return "artifact round-trip succeeded"
    finally:
        await _delete_probe_artifact_definitively(typed_delete, key)


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


def _provider_payload(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="python") if callable(dump) else value


_RUNTIME_IDENTITY_FIELDS = frozenset(
    {"provider", "project_id", "base_url", "options", "secret_ref"}
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _validate_provider(kind: PortKind, provider: str) -> None:
    registered = AdapterRegistry.providers_for(kind)
    if provider not in registered:
        raise HTTPException(
            status_code=422,
            detail="unknown provider for connection kind",
        )


def _validate_connection_target(
    base_url: str | None,
    options: dict[str, Any] | None,
    secret_ref: str | None,
) -> None:
    connection_options = options or {}
    try:
        reject_raw_secret_options(connection_options)
        validate_secret_ref(secret_ref)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="invalid connection secret configuration"
        ) from exc
    allow_private = connection_options.get(TRUSTED_PRIVATE_HOST_OPTION) is True
    try:
        validate_adapter_transport_options(
            connection_options,
            allow_private_hosts=allow_private or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid connection transport options") from exc
    for raw_url in (base_url, connection_options.get("base_url")):
        try:
            validate_adapter_base_url(raw_url, allow_private_hosts=allow_private or None)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid connection target") from exc
    endpoint = connection_options.get("endpoint")
    if endpoint is not None:
        try:
            normalized_endpoint, endpoint_secure = normalize_host_port_endpoint(
                endpoint,
                secure=coerce_bool(connection_options.get("secure"), default=False),
            )
            scheme = "https" if endpoint_secure else "http"
            validate_adapter_base_url(
                f"{scheme}://{normalized_endpoint}",
                allow_private_hosts=allow_private or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid connection endpoint") from exc
    if connection_options_require_repair(connection_options):
        raise HTTPException(
            status_code=422,
            detail="connection options contain unsafe credential-bearing configuration",
        )


async def _get_or_404(repo: ConnectionsRepository, connection_id: str) -> Connection:
    conn = await repo.get(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="connection not found")
    return conn


async def _get_for_update_or_404(repo: ConnectionsRepository, connection_id: str) -> Connection:
    getter = getattr(repo, "get_for_update", repo.get)
    conn = await getter(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="connection not found")
    return conn


def _can_manage_connection(
    identity: ConsumerIdentity,
    project_id: str | None,
    *,
    kind: PortKind | str | None = None,
    provider: str | None = None,
    base_url: Any = None,
    secret_ref: str | None = None,
    options: dict[str, Any] | None = None,
) -> bool:
    if identity.is_unscoped:
        return True
    try:
        port_kind = PortKind(kind) if kind is not None else None
    except ValueError:
        return False
    connection_options = {} if options is None else options
    if (
        secret_ref is not None
        or connection_url_requires_repair(base_url)
        or connection_options_require_repair(connection_options)
        or port_kind is PortKind.SECRETS
        or connection_options.get(TRUSTED_PRIVATE_HOST_OPTION) is True
        or _uses_platform_ambient_identity(port_kind, provider, connection_options)
    ):
        return False
    return project_id is not None and identity.contains_scope(ScopeRef(project_id=project_id))


def _ensure_can_manage_connection(
    identity: ConsumerIdentity,
    project_id: str | None,
    *,
    kind: PortKind | str | None = None,
    provider: str | None = None,
    base_url: Any = None,
    secret_ref: str | None = None,
    options: dict[str, Any] | None = None,
) -> None:
    if not _can_manage_connection(
        identity,
        project_id,
        kind=kind,
        provider=provider,
        base_url=base_url,
        secret_ref=secret_ref,
        options=options,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Secret-bearing, repair-required, ambient-identity, secrets-port, global, and "
                "out-of-scope connections "
                "require an unscoped platform admin"
            ),
        )


def _ensure_can_manage_row(identity: ConsumerIdentity, conn: Connection) -> None:
    _ensure_can_manage_connection(
        identity,
        conn.project_id,
        kind=conn.kind,
        provider=conn.provider,
        base_url=conn.base_url,
        secret_ref=conn.secret_ref,
        options=conn.options,
    )


def _uses_platform_ambient_identity(
    kind: PortKind | str | None,
    provider: str | None,
    options: dict[str, Any] | None,
) -> bool:
    """Provider policy for modes that consume the APEX workload's identity."""

    if kind is None or PortKind(kind) is not PortKind.CLUSTER_INVENTORY:
        return False
    if (provider or "").strip().casefold() != "kubernetes":
        return False
    auth_mode = str((options or {}).get("auth_mode", "bearer")).strip().casefold()
    return auth_mode in _KUBERNETES_IN_CLUSTER_AUTH_MODES


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

    if PortKind(conn.kind) not in {PortKind.ARTIFACT_STORE, PortKind.EXECUTION_ENGINE}:
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
            detail="connection is still referenced; migrate references first",
        )


def _probe_failure_detail(exc: Exception) -> str:
    if isinstance(exc, (KeyError, ValueError)):
        return "connection probe configuration is invalid"
    return "connection probe failed; check server logs for details"


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("", operation_id="listConnections")
async def list_connections(
    identity: AdminIdentity,
    repo: ConnectionsRepo,
    kind: PortKind | None = None,
    project: Annotated[ScopeId | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> list[ConnectionOut]:
    manageable_projects = (
        None
        if identity.is_unscoped
        else tuple(scope.project_id for scope in identity.scopes if scope.app_id is None)
    )
    rows = await repo.list_connections(
        kind=kind.value if kind is not None else None,
        project=project,
        manageable_project_ids=manageable_projects,
        limit=limit,
        offset=offset,
    )
    rows = [
        row
        for row in rows
        if _can_manage_connection(
            identity,
            row.project_id,
            kind=row.kind,
            provider=row.provider,
            base_url=row.base_url,
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
        provider=body.provider,
        base_url=body.base_url,
        secret_ref=body.secret_ref,
        options=body.options,
    )
    _ensure_options_are_mutable_by(identity, body.options)
    _validate_provider(body.kind, body.provider)
    _validate_connection_target(body.base_url, body.options, body.secret_ref)
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
        raise HTTPException(status_code=409, detail="connection name already exists") from None
    return ConnectionOut.model_validate(conn)


@router.get("/{connection_id}", operation_id="getConnection")
async def get_connection(
    connection_id: ConnectionId, identity: AdminIdentity, repo: ConnectionsRepo
) -> ConnectionOut:
    conn = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    return ConnectionOut.model_validate(conn)


@router.patch("/{connection_id}", operation_id="updateConnection")
async def update_connection(
    connection_id: ConnectionId,
    body: ConnectionUpdate,
    identity: AdminIdentity,
    repo: ConnectionsRepo,
) -> ConnectionOut:
    conn = await _get_for_update_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    changes = body.model_dump(exclude_unset=True)
    if not identity.is_unscoped and changes.get("secret_ref") is not None:
        raise HTTPException(
            status_code=403,
            detail="Only an unscoped platform admin can attach a connection secret",
        )
    if "options" in changes:
        _ensure_options_are_mutable_by(identity, changes["options"] or {})
    if "provider" in changes:
        _validate_provider(PortKind(conn.kind), changes["provider"])
    # Authorize the complete effective row, not just the pre-PATCH row. This
    # prevents a scoped admin from switching a benign connection to a mode that
    # consumes the platform pod identity.
    _ensure_can_manage_connection(
        identity,
        changes.get("project_id", conn.project_id),
        kind=conn.kind,
        provider=changes.get("provider", conn.provider),
        base_url=changes.get("base_url", conn.base_url),
        secret_ref=changes.get("secret_ref", conn.secret_ref),
        options=changes.get("options", conn.options),
    )
    _protect_runtime_identity(conn, changes)
    runtime_identity_changed = any(
        field in changes and changes[field] != getattr(conn, field)
        for field in _RUNTIME_IDENTITY_FIELDS
    )
    any_field_changed = any(value != getattr(conn, field) for field, value in changes.items())
    if (PortKind(conn.kind) is PortKind.EXECUTION_ENGINE and runtime_identity_changed) or (
        PortKind(conn.kind) is PortKind.WORK_TRACKING and any_field_changed
    ):
        await _protect_durable_references(repo, conn)
    _validate_connection_target(
        changes.get("base_url", conn.base_url),
        changes.get("options", conn.options),
        changes.get("secret_ref", conn.secret_ref),
    )
    try:
        conn = await repo.update(conn, changes)
    except DuplicateConnectionNameError:
        raise HTTPException(status_code=409, detail="connection name already exists") from None
    return ConnectionOut.model_validate(conn)


@router.delete("/{connection_id}", operation_id="deleteConnection", status_code=204)
async def delete_connection(
    connection_id: ConnectionId, identity: AdminIdentity, repo: ConnectionsRepo
) -> None:
    conn = await _get_for_update_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    await _protect_durable_references(repo, conn)
    await repo.delete(conn)


@router.post("/{connection_id}/enable", operation_id="enableConnection")
async def enable_connection(
    connection_id: ConnectionId, identity: AdminIdentity, repo: ConnectionsRepo
) -> ConnectionOut:
    conn = await _get_for_update_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    # Revalidate the effective persisted row, not merely its original request.
    # Legacy/direct-SQL records can predate the current write validators and
    # must be repaired before they can be made available to runtime resolvers.
    _validate_connection_target(conn.base_url, conn.options, conn.secret_ref)
    if conn.enabled:
        return ConnectionOut.model_validate(conn)
    return ConnectionOut.model_validate(await repo.set_enabled(conn, True))


@router.post("/{connection_id}/disable", operation_id="disableConnection")
async def disable_connection(
    connection_id: ConnectionId, identity: AdminIdentity, repo: ConnectionsRepo
) -> ConnectionOut:
    conn = await _get_for_update_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    if not conn.enabled:
        return ConnectionOut.model_validate(conn)
    await _protect_durable_references(repo, conn)
    return ConnectionOut.model_validate(await repo.set_enabled(conn, False))


@router.get("/{connection_id}/host-mappings", operation_id="getHostMappings")
async def get_host_mappings(
    connection_id: ConnectionId, identity: AdminIdentity, repo: ConnectionsRepo
) -> list[HostMappingOut]:
    conn = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    return [HostMappingOut.model_validate(m) for m in conn.host_mappings]


@router.put("/{connection_id}/host-mappings", operation_id="putHostMappings")
async def put_host_mappings(
    connection_id: ConnectionId,
    body: Annotated[list[HostMappingIn], Body(max_length=MAX_CHILD_ITEMS)],
    identity: AdminIdentity,
    repo: ConnectionsRepo,
) -> list[HostMappingOut]:
    """Replaces the FULL mapping list (PUT semantics)."""
    # Serialize full replacements with every other lifecycle mutation. Without
    # the parent-row lock, concurrent PUTs can each delete a stale child set and
    # then commit the union of both replacement lists.
    conn = await _get_for_update_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, conn)
    conn = await repo.replace_host_mappings(conn, [m.model_dump() for m in body])
    return [HostMappingOut.model_validate(m) for m in conn.host_mappings]


@router.post("/{connection_id}/test", operation_id="testConnection")
async def test_connection(
    connection_id: ConnectionId, identity: AdminIdentity, repo: ConnectionsRepo
) -> ProbeResult:
    """Build the adapter exactly as the resolver would and run the kind's probe.

    Always 200: failures (bad secret_ref, unreachable backend, misconfigured
    options) come back inline as ok=false so the admin UI can show them.
    """
    row = await _get_or_404(repo, connection_id)
    _ensure_can_manage_row(identity, row)
    config = connection_config_from_row(row)
    await release_read_transactions(repo)
    started = time.perf_counter()
    adapter: Any | None = None
    try:
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
    return ProbeResult(
        ok=ok,
        latency_ms=latency_ms,
        detail=bounded_diagnostic(detail, max_chars=1_024),
    )
