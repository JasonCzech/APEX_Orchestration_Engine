"""Inventory orchestration: latest-snapshot reads + inline environment rescans.

Payload decision: both inventory endpoints return {environment_id, snapshot}
where snapshot is null until the environment has been scanned at least once;
snapshot carries scanned_at, services[], and stale (true when scanned_at is
strictly older than STALE_AFTER = 7 days).

Rescan runs INLINE — the adapter scan is awaited inside the request, because
Kubernetes inventory scans take seconds, not minutes. If scans ever grow
beyond request-friendly latency, the documented future option is 202 +
background job; the payload shape would not change.

Unlike the connection resolver's throwaway-engine pattern, the repository here
uses the request-scoped session from apex.persistence.db.get_session — this
module only ever runs in router scope.
"""

import re
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import PortKind
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.integrations import (
    MAX_INVENTORY_SERVICES,
    EnvRef,
    ServiceInfo,
)
from apex.domain.integrations import (
    EnvironmentSnapshot as DomainEnvironmentSnapshot,
)
from apex.persistence.db import get_session
from apex.persistence.models import EnvironmentSnapshot
from apex.persistence.repositories.snapshots import SnapshotsRepository
from apex.ports.cluster_inventory import ClusterInventoryPort
from apex.services.connections import close_adapter, get_connection_resolver

STALE_AFTER = timedelta(days=7)
MAX_CONCURRENT_INVENTORY_SCANS = 4
_INVENTORY_SCAN_ADMISSION_LOCK = threading.Lock()
_ACTIVE_INVENTORY_SCANS = 0
_SERVICE_NAME = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?\Z")
_IMAGE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")
_IMAGE_DIGEST = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._:/-]*@[A-Za-z][A-Za-z0-9_+.-]*:[A-Fa-f0-9]{32,512}\Z"
)

# (connection_id, project_id) -> adapter; a dependency so tests can inject fakes.
AdapterResolver = Callable[[str | None, str | None], Awaitable[ClusterInventoryPort]]
InventoryAdapterSource = ClusterInventoryPort | Callable[[], Awaitable[ClusterInventoryPort]]


class InventoryScanBusyError(RuntimeError):
    """Process scan capacity is exhausted; callers should retry later."""


@asynccontextmanager
async def inventory_scan_admission() -> AsyncIterator[None]:
    """Fail fast before adapter/provider I/O without creating queued waiters."""

    global _ACTIVE_INVENTORY_SCANS
    with _INVENTORY_SCAN_ADMISSION_LOCK:
        if _ACTIVE_INVENTORY_SCANS >= MAX_CONCURRENT_INVENTORY_SCANS:
            raise InventoryScanBusyError
        _ACTIVE_INVENTORY_SCANS += 1
    try:
        yield
    finally:
        with _INVENTORY_SCAN_ADMISSION_LOCK:
            _ACTIVE_INVENTORY_SCANS -= 1


class SnapshotView(BaseModel):
    scanned_at: datetime
    services: list[ServiceInfo]
    stale: bool


class InventoryView(BaseModel):
    environment_id: str
    snapshot: SnapshotView | None = None  # null = never scanned


def is_stale(scanned_at: datetime, *, now: datetime | None = None) -> bool:
    """Strictly older than STALE_AFTER. Naive timestamps are assumed UTC."""
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(UTC)
    return (reference - scanned_at) > STALE_AFTER


# ── FastAPI dependencies ─────────────────────────────────────────────────────


def get_snapshots_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SnapshotsRepository:
    return SnapshotsRepository(session)


def get_inventory_adapter_resolver() -> AdapterResolver:
    """Resolve a cluster-inventory adapter through the connection resolver
    (explicit connection_id > project-scoped row > global row > static stub)."""

    async def _resolve(connection_id: str | None, project_id: str | None) -> ClusterInventoryPort:
        return await get_connection_resolver().resolve(
            PortKind.CLUSTER_INVENTORY, connection_id=connection_id, project_id=project_id
        )

    return _resolve


# ── service ──────────────────────────────────────────────────────────────────


class InventoryService:
    def __init__(self, repository: SnapshotsRepository) -> None:
        self._repository = repository

    async def latest_inventory(self, environment_id: str) -> InventoryView:
        row = await self._repository.latest(environment_id)
        return _view(environment_id, row)

    async def rescan(self, env: EnvRef, adapter: InventoryAdapterSource) -> InventoryView:
        """Scan now, persist bounded history, and cap process-wide in-flight work.

        A lazy adapter factory lets HTTP callers acquire admission before secret,
        connection, or provider I/O. Direct service callers may still pass an
        already-resolved adapter and receive the same provider-work protection.
        """
        async with inventory_scan_admission():
            owns_adapter = callable(adapter)
            resolved_adapter = await adapter() if owns_adapter else adapter
            try:
                snapshot = await resolved_adapter.scan_environment(EnvRef(id=env.id, name=env.name))
                normalized = validated_provider_snapshot(snapshot)
                scanned_at = datetime.now(UTC)
                data = normalized.model_dump(mode="json")
                # Scan time is a server-owned fact. Validate the provider field for a
                # well-formed port result, then replace it so a provider cannot make a
                # snapshot permanently fresh (or prematurely stale).
                data["scanned_at"] = scanned_at.isoformat()
                row = await self._repository.add(
                    env.id,
                    data=data,
                    scanned_at=scanned_at,
                )
                return _view(env.id, row)
            finally:
                if owns_adapter:
                    await close_adapter(resolved_adapter)


def validated_provider_snapshot(snapshot: object) -> DomainEnvironmentSnapshot:
    validated: DomainEnvironmentSnapshot | None = None
    try:
        validated = _validated_provider_snapshot(snapshot)
    except Exception:
        pass
    if validated is None:
        # Raise after leaving the handler so provider strings are not retained
        # in an exception context traversed by logging or telemetry.
        raise RuntimeError("cluster inventory adapter returned an invalid snapshot")
    return validated


def _validated_provider_snapshot(snapshot: object) -> DomainEnvironmentSnapshot:
    if type(snapshot) is not DomainEnvironmentSnapshot or snapshot.__pydantic_extra__:
        raise ValueError("unexpected snapshot type")
    raw = _bounded_mapping(
        cast(dict[Any, Any], snapshot.__dict__),
        allowed={"scanned_at", "services"},
        required={"scanned_at", "services"},
    )
    raw_services = raw["services"]
    scanned_at = raw["scanned_at"]
    if (
        type(raw_services) is not list
        or len(raw_services) > MAX_INVENTORY_SERVICES
        or type(scanned_at) is not str
        or not 1 <= len(scanned_at) <= 64
        or "\x00" in scanned_at
    ):
        raise ValueError("unbounded snapshot fields")
    _parse_scanned_at(scanned_at)
    services = [_validated_service_info(service) for service in raw_services]
    return DomainEnvironmentSnapshot(services=services, scanned_at=scanned_at)


def _validated_service_info(service: object) -> ServiceInfo:
    if type(service) is ServiceInfo:
        if service.__pydantic_extra__:
            raise ValueError("unexpected service fields")
        source: object = service.__dict__
        required = {"image", "name", "replicas"}
    else:
        source = service
        required = {"name"}
    if type(source) is not dict:
        raise ValueError("inventory service is not an object")
    raw = _bounded_mapping(
        source,
        allowed={"image", "name", "replicas"},
        required=required,
    )
    name = raw["name"]
    image = raw.get("image", "")
    replicas = raw.get("replicas", 1)
    if (
        type(name) is not str
        or not 1 <= len(name) <= 253
        or type(image) is not str
        or len(image) > 2_048
        or type(replicas) is not int
        or not 0 <= replicas <= 10_000_000
    ):
        raise ValueError("unbounded inventory service fields")
    if (
        _SERVICE_NAME.fullmatch(name) is None
        or bounded_diagnostic(name, max_chars=len(name)) != name
    ):
        raise ValueError("invalid inventory service name")
    if image and (
        "://" in image
        or "//" in image
        or ("@" in image and _IMAGE_DIGEST.fullmatch(image) is None)
        or ("@" not in image and _IMAGE_NAME.fullmatch(image) is None)
        or bounded_diagnostic(image, max_chars=len(image)) != image
    ):
        raise ValueError("invalid inventory image reference")
    return ServiceInfo(name=name, replicas=replicas, image=image)


def _bounded_mapping(
    value: dict[Any, Any],
    *,
    allowed: set[str],
    required: set[str],
) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("inventory object must be a plain mapping")
    keys: list[str] = []
    iterator = iter(value)
    for _ in range(len(allowed) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            break
        if type(key) is not str:
            raise ValueError("inventory field names must be strings")
        keys.append(key)
    key_set = set(keys)
    if len(keys) > len(allowed) or not required <= key_set or not key_set <= allowed:
        raise ValueError("inventory fields do not match the expected schema")
    return {key: value[key] for key in keys}


def _parse_scanned_at(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _view(environment_id: str, row: EnvironmentSnapshot | None) -> InventoryView:
    if row is None:
        return InventoryView(environment_id=environment_id, snapshot=None)
    data = row.data
    if type(data) is not dict:
        raise RuntimeError("persisted inventory snapshot data must be an object")
    raw_services = data.get("services", [])
    if type(raw_services) is not list:
        raise RuntimeError("persisted inventory snapshot services must be a list")
    if len(raw_services) > MAX_INVENTORY_SERVICES:
        raise RuntimeError(
            "persisted inventory snapshot exceeds the aggregate service limit of "
            f"{MAX_INVENTORY_SERVICES}"
        )
    if any(
        type(service) is not dict and type(service) is not ServiceInfo for service in raw_services
    ):
        raise RuntimeError("persisted inventory snapshot contains a non-object service")
    services: list[ServiceInfo] | None = None
    try:
        services = [_validated_service_info(service) for service in raw_services]
    except Exception:
        pass
    if services is None:
        raise RuntimeError("persisted inventory snapshot contains invalid service data")
    return InventoryView(
        environment_id=environment_id,
        snapshot=SnapshotView(
            scanned_at=row.scanned_at, services=services, stale=is_stale(row.scanned_at)
        ),
    )
