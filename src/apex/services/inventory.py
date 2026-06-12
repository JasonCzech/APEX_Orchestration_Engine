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

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import PortKind
from apex.domain.integrations import EnvRef, ServiceInfo
from apex.persistence.db import get_session
from apex.persistence.models import Environment, EnvironmentSnapshot
from apex.persistence.repositories.snapshots import SnapshotsRepository
from apex.ports.cluster_inventory import ClusterInventoryPort
from apex.services.connections import get_connection_resolver

STALE_AFTER = timedelta(days=7)

# (connection_id, project_id) -> adapter; a dependency so tests can inject fakes.
AdapterResolver = Callable[[str | None, str | None], Awaitable[ClusterInventoryPort]]


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

    async def rescan(self, env: Environment, adapter: ClusterInventoryPort) -> InventoryView:
        """Scan now, persist one NEW snapshot row, return the fresh inventory."""
        snapshot = await adapter.scan_environment(EnvRef(id=env.id, name=env.name))
        row = await self._repository.add(
            env.id,
            data=snapshot.model_dump(),
            scanned_at=_parse_scanned_at(snapshot.scanned_at),
        )
        return _view(env.id, row)


def _parse_scanned_at(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _view(environment_id: str, row: EnvironmentSnapshot | None) -> InventoryView:
    if row is None:
        return InventoryView(environment_id=environment_id, snapshot=None)
    raw_services = (row.data or {}).get("services", [])
    services = [ServiceInfo.model_validate(s) for s in raw_services if isinstance(s, dict)]
    return InventoryView(
        environment_id=environment_id,
        snapshot=SnapshotView(
            scanned_at=row.scanned_at, services=services, stale=is_stale(row.scanned_at)
        ),
    )
