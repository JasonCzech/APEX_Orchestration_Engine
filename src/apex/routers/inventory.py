"""/inventory: latest cluster-inventory snapshots + on-demand environment rescans.

GET returns the latest persisted EnvironmentSnapshot row (snapshot=null when
the environment has never been scanned; stale=true when the scan is older than
7 days). POST .../rescan resolves the cluster-inventory adapter (optional
?connection_id= override), scans INLINE, persists one NEW snapshot row, and
returns the fresh payload; adapter/resolution failures surface as 502.

Scoping mirrors /catalog: environments inherit project_id through their
application; cross-project single-resource access 404s to avoid leaking
existence. Roles: GET = any authenticated; rescan = operator+.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from apex.app.dependencies import CurrentIdentity, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.persistence.models import Environment
from apex.persistence.repositories.snapshots import SnapshotsRepository
from apex.services.inventory import (
    AdapterResolver,
    InventoryService,
    InventoryView,
    get_inventory_adapter_resolver,
    get_snapshots_repository,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])

SnapshotsRepo = Annotated[SnapshotsRepository, Depends(get_snapshots_repository)]
ResolverDep = Annotated[AdapterResolver, Depends(get_inventory_adapter_resolver)]
OperatorIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))]


async def _load_visible_environment(
    repository: SnapshotsRepository, environment_id: str, identity: ConsumerIdentity
) -> Environment:
    env = await repository.get_environment(environment_id)
    if env is None or not identity.allows_project(env.application.project_id):
        raise HTTPException(status_code=404, detail=f"environment {environment_id!r} not found")
    return env


@router.get(
    "/environments/{environment_id}",
    operation_id="getEnvironmentInventory",
    response_model=InventoryView,
)
async def get_environment_inventory(
    environment_id: str, identity: CurrentIdentity, repository: SnapshotsRepo
) -> InventoryView:
    env = await _load_visible_environment(repository, environment_id, identity)
    return await InventoryService(repository).latest_inventory(env.id)


@router.post(
    "/environments/{environment_id}/rescan",
    operation_id="rescanEnvironment",
    response_model=InventoryView,
)
async def rescan_environment(
    environment_id: str,
    identity: OperatorIdentity,
    repository: SnapshotsRepo,
    resolve_adapter: ResolverDep,
    connection_id: str | None = None,
) -> InventoryView:
    env = await _load_visible_environment(repository, environment_id, identity)
    project_id = env.application.project_id
    try:
        adapter = await resolve_adapter(connection_id, project_id)
        return await InventoryService(repository).rescan(env, adapter)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail="environment rescan failed") from exc
