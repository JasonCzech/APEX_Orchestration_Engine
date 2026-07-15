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

import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import PortKind
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
from apex.services.connections import get_connection_resolver

STALE_AFTER = timedelta(days=7)
MAX_CONCURRENT_INVENTORY_SCANS = 4
_INVENTORY_SCAN_ADMISSION_LOCK = threading.Lock()
_ACTIVE_INVENTORY_SCANS = 0

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
            resolved_adapter = await adapter() if callable(adapter) else adapter
            snapshot = await resolved_adapter.scan_environment(EnvRef(id=env.id, name=env.name))
            normalized = _validated_provider_snapshot(snapshot)
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


def _validated_provider_snapshot(snapshot: object) -> DomainEnvironmentSnapshot:
    if not isinstance(snapshot, DomainEnvironmentSnapshot):
        raise RuntimeError("cluster inventory adapter returned an invalid snapshot")
    try:
        # Re-serialize and re-validate to defeat model_construct(), unchecked
        # assignment, and shared mutable objects crossing the adapter boundary.
        raw = snapshot.model_dump(mode="json", warnings="error")
        normalized = DomainEnvironmentSnapshot.model_validate(raw)
        _parse_scanned_at(normalized.scanned_at)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RuntimeError("cluster inventory adapter returned an invalid snapshot") from exc
    return normalized


def _parse_scanned_at(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _view(environment_id: str, row: EnvironmentSnapshot | None) -> InventoryView:
    if row is None:
        return InventoryView(environment_id=environment_id, snapshot=None)
    data = row.data
    if not isinstance(data, dict):
        raise RuntimeError("persisted inventory snapshot data must be an object")
    raw_services = data.get("services", [])
    if not isinstance(raw_services, list):
        raise RuntimeError("persisted inventory snapshot services must be a list")
    if len(raw_services) > MAX_INVENTORY_SERVICES:
        raise RuntimeError(
            "persisted inventory snapshot exceeds the aggregate service limit of "
            f"{MAX_INVENTORY_SERVICES}"
        )
    if any(not isinstance(service, dict) for service in raw_services):
        raise RuntimeError("persisted inventory snapshot contains a non-object service")
    try:
        services = [ServiceInfo.model_validate(service) for service in raw_services]
    except ValidationError as exc:
        raise RuntimeError("persisted inventory snapshot contains invalid service data") from exc
    return InventoryView(
        environment_id=environment_id,
        snapshot=SnapshotView(
            scanned_at=row.scanned_at, services=services, stale=is_stale(row.scanned_at)
        ),
    )
