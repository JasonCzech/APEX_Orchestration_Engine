"""/inventory: latest cluster-inventory snapshots + on-demand environment rescans.

GET returns the latest persisted EnvironmentSnapshot row (snapshot=null when
the environment has never been scanned; stale=true when the scan is older than
7 days). POST .../rescan resolves the cluster-inventory adapter (optional
?connection_id= override), scans INLINE, persists bounded snapshot history, and
returns the fresh payload; adapter/resolution failures surface as 502.

Scoping mirrors /catalog: environments inherit project_id through their
application; cross-project single-resource access 404s to avoid leaking
existence. Roles: GET = any authenticated; rescan = operator+.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from apex.app.dependencies import CurrentIdentity, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.domain.input_limits import RecordId
from apex.domain.integrations import EnvRef
from apex.persistence.db import release_read_transactions
from apex.persistence.models import Environment
from apex.persistence.repositories.snapshots import SnapshotsRepository
from apex.services.inventory import (
    AdapterResolver,
    InventoryScanBusyError,
    InventoryService,
    InventoryView,
    get_inventory_adapter_resolver,
    get_snapshots_repository,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])

SnapshotsRepo = Annotated[SnapshotsRepository, Depends(get_snapshots_repository)]
ResolverDep = Annotated[AdapterResolver, Depends(get_inventory_adapter_resolver)]
OperatorIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))]
EnvironmentId = Annotated[RecordId, Path(description="Environment id")]
ConnectionIdParam = Annotated[
    RecordId | None,
    Query(description="Explicit cluster-inventory connection id"),
]


async def _load_visible_environment(
    repository: SnapshotsRepository, environment_id: str, identity: ConsumerIdentity
) -> Environment:
    env = await repository.get_environment(environment_id)
    if env is None or not identity.allows_scope(
        project_id=env.application.project_id, app_id=env.application_id
    ):
        raise HTTPException(status_code=404, detail="environment not found")
    return env


@router.get(
    "/environments/{environment_id}",
    operation_id="getEnvironmentInventory",
    response_model=InventoryView,
)
async def get_environment_inventory(
    environment_id: EnvironmentId, identity: CurrentIdentity, repository: SnapshotsRepo
) -> InventoryView:
    env = await _load_visible_environment(repository, environment_id, identity)
    return await InventoryService(repository).latest_inventory(env.id)


@router.post(
    "/environments/{environment_id}/rescan",
    operation_id="rescanEnvironment",
    response_model=InventoryView,
)
async def rescan_environment(
    environment_id: EnvironmentId,
    identity: OperatorIdentity,
    repository: SnapshotsRepo,
    resolve_adapter: ResolverDep,
    connection_id: ConnectionIdParam = None,
) -> InventoryView:
    env = await _load_visible_environment(repository, environment_id, identity)
    project_id = env.application.project_id
    environment = EnvRef(id=env.id, name=env.name)
    await release_read_transactions(repository)
    rescan_error: HTTPException | None = None
    result: InventoryView | None = None
    try:
        result = await InventoryService(repository).rescan(
            environment,
            lambda: resolve_adapter(connection_id, project_id),
        )
    except InventoryScanBusyError:
        rescan_error = HTTPException(
            status_code=429,
            detail="inventory scan capacity is exhausted; retry later",
            headers={"Retry-After": "1"},
        )
    except Exception:
        # Provider/config exceptions can retain credentials in traceback locals.
        rescan_error = HTTPException(status_code=502, detail="environment rescan failed")
    if rescan_error is not None:
        # Raise only after the provider handler is gone; ``from None`` hides an
        # exception chain but still leaves the raw exception in __context__.
        raise rescan_error
    assert result is not None
    return result
