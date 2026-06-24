"""/work-tracking: NL query translation, tracker passthrough, saved-query CRUD.

Adapter resolution follows the M2 pattern: get_connection_resolver().resolve(
WORK_TRACKING, connection_id=<body or ?connection_id>, project_id=<first scoped
project or None>) — explicit connection > project row > global row > stub
fallback. Every passthrough endpoint accepts an optional connection_id query
parameter; the two POST /query endpoints additionally accept it in the body
(the body value wins when both are present).

Error contract (adapters raise, this router translates to problem details):
- KeyError (unknown item / unknown connection)  -> 404
- ValueError (bad query, rejected request)      -> 422 (409 for connection
  config conflicts at resolution time and saved-query name collisions)
- RuntimeError / httpx transport failures       -> 502 with the adapter message

Saved queries are project-scoped like M2 documents: global rows (project_id
NULL) are visible to everyone, scoped rows only to consumers with that project
scope; mutations require operator+.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.domain.integrations import (
    Enrichment,
    Page,
    QueryContext,
    TranslatedQuery,
    WorkItem,
    WorkItemDraft,
    WorkItemFilters,
    WorkItemPage,
)
from apex.persistence.models import SavedQuery
from apex.persistence.repositories.saved_queries import SavedQueriesRepository
from apex.services.connections import ConnectionResolver
from apex.services.work_tracking import (
    get_saved_queries_repository,
    get_work_tracking_resolver,
)

router = APIRouter(prefix="/work-tracking", tags=["work-tracking"])

ResolverDep = Annotated[ConnectionResolver, Depends(get_work_tracking_resolver)]
RepositoryDep = Annotated[SavedQueriesRepository, Depends(get_saved_queries_repository)]
ConnectionIdParam = Annotated[
    str | None, Query(description="Explicit work-tracking connection id (default: resolved)")
]


# ── Schemas ──────────────────────────────────────────────────────────────────


class TranslateQueryRequest(BaseModel):
    text: str = Field(min_length=1)
    connection_id: str | None = None


class ExecuteQueryRequest(BaseModel):
    query: TranslatedQuery
    connection_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class SavedQueryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1)
    project_id: str | None = None
    description: str | None = None


class SavedQueryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    query: str | None = Field(default=None, min_length=1)
    description: str | None = None


class SavedQueryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    project_id: str | None = None
    provider: str
    query: str
    description: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SavedQueryListResponse(BaseModel):
    items: list[SavedQueryOut]
    limit: int
    offset: int


# ── Helpers ──────────────────────────────────────────────────────────────────


def _scoped_project(identity: ConsumerIdentity) -> str | None:
    """First scoped project id, or None for unscoped consumers."""
    project_ids = identity.scoped_project_ids()
    return project_ids[0] if project_ids else None


async def _resolve_adapter(
    resolver: ConnectionResolver, identity: ConsumerIdentity, connection_id: str | None
) -> Any:
    try:
        return await resolver.resolve(
            PortKind.WORK_TRACKING,
            connection_id=connection_id,
            project_id=_scoped_project(identity),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_exc_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def get_work_tracking_adapter(
    identity: CurrentIdentity, resolver: ResolverDep, connection_id: ConnectionIdParam = None
) -> Any:
    return await _resolve_adapter(resolver, identity, connection_id)


AdapterDep = Annotated[Any, Depends(get_work_tracking_adapter)]


def _exc_detail(exc: BaseException) -> str:
    return str(exc.args[0]) if exc.args else str(exc)


@contextmanager
def adapter_errors() -> Iterator[None]:
    """Translate adapter exceptions into problem details (see module doc)."""
    try:
        yield
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_exc_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="work tracker rejected the request") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="work tracker upstream failure") from exc
    except httpx.HTTPError as exc:  # defensive: adapters normally wrap transport errors
        raise HTTPException(status_code=502, detail="work tracker upstream failure") from exc


def _visible(identity: ConsumerIdentity, row: SavedQuery) -> bool:
    """Global rows (project_id NULL) are visible to everyone; scoped rows need scope."""
    return row.project_id is None or identity.allows_project(row.project_id)


# ── Query passthrough ────────────────────────────────────────────────────────


@router.post("/query/translate", operation_id="translateWorkQuery", response_model=TranslatedQuery)
async def translate_work_query(
    body: TranslateQueryRequest,
    identity: CurrentIdentity,
    resolver: ResolverDep,
    connection_id: ConnectionIdParam = None,
) -> Any:
    adapter = await _resolve_adapter(resolver, identity, body.connection_id or connection_id)
    context = QueryContext(project_id=_scoped_project(identity))
    with adapter_errors():
        return await adapter.translate_query(body.text, context=context)


@router.post("/query/execute", operation_id="executeWorkQuery", response_model=WorkItemPage)
async def execute_work_query(
    body: ExecuteQueryRequest,
    identity: CurrentIdentity,
    resolver: ResolverDep,
    connection_id: ConnectionIdParam = None,
) -> Any:
    adapter = await _resolve_adapter(resolver, identity, body.connection_id or connection_id)
    page = Page(offset=body.offset, limit=body.limit)
    with adapter_errors():
        return await adapter.execute_query(body.query, page=page)


# ── Item passthrough ─────────────────────────────────────────────────────────


@router.get("/items", operation_id="listWorkItems", response_model=WorkItemPage)
async def list_work_items(
    adapter: AdapterDep,
    status: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    filters = WorkItemFilters(status=status, kind=kind, text=q)
    with adapter_errors():
        return await adapter.list_items(filters, page=Page(offset=offset, limit=limit))


@router.get("/items/{key}", operation_id="getWorkItem", response_model=WorkItem)
async def get_work_item(key: str, adapter: AdapterDep) -> Any:
    with adapter_errors():
        return await adapter.get_item(key)


@router.post(
    "/items",
    operation_id="createWorkItem",
    status_code=201,
    response_model=WorkItem,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def create_work_item(draft: WorkItemDraft, adapter: AdapterDep) -> Any:
    with adapter_errors():
        return await adapter.create_item(draft)


@router.post(
    "/items/{key}/enrich",
    operation_id="enrichWorkItem",
    response_model=WorkItem,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def enrich_work_item(key: str, enrichment: Enrichment, adapter: AdapterDep) -> Any:
    with adapter_errors():
        return await adapter.enrich_item(key, enrichment)


# ── Saved queries ────────────────────────────────────────────────────────────


@router.get(
    "/saved-queries", operation_id="listSavedQueries", response_model=SavedQueryListResponse
)
async def list_saved_queries(
    identity: CurrentIdentity,
    repository: RepositoryDep,
    project: str | None = None,
    provider: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    allowed = None if identity.is_unscoped else identity.scoped_project_ids()
    rows = await repository.list(
        project=project,
        provider=provider,
        allowed_project_ids=allowed,
        limit=limit,
        offset=offset,
    )
    return {"items": rows, "limit": limit, "offset": offset}


@router.post(
    "/saved-queries",
    operation_id="createSavedQuery",
    status_code=201,
    response_model=SavedQueryOut,
)
async def create_saved_query(
    body: SavedQueryCreate,
    identity: Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))],
    repository: RepositoryDep,
) -> Any:
    if body.project_id is not None and not identity.allows_project(body.project_id):
        raise HTTPException(
            status_code=403, detail=f"consumer is not scoped to project {body.project_id!r}"
        )
    row = SavedQuery(
        name=body.name,
        project_id=body.project_id,
        provider=body.provider,
        query=body.query,
        description=body.description,
        created_by=identity.name,
    )
    try:
        return await repository.add(row)
    except ValueError as exc:  # unique (project_id, name) collision
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/saved-queries/{saved_query_id}", operation_id="getSavedQuery")
async def get_saved_query(
    saved_query_id: str, identity: CurrentIdentity, repository: RepositoryDep
) -> SavedQueryOut:
    row = await repository.get(saved_query_id)
    if row is None or not _visible(identity, row):
        raise HTTPException(status_code=404, detail=f"saved query {saved_query_id!r} not found")
    return SavedQueryOut.model_validate(row)


@router.patch(
    "/saved-queries/{saved_query_id}",
    operation_id="updateSavedQuery",
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def update_saved_query(
    saved_query_id: str,
    body: SavedQueryUpdate,
    identity: CurrentIdentity,
    repository: RepositoryDep,
) -> SavedQueryOut:
    row = await repository.get(saved_query_id)
    if row is None or not _visible(identity, row):
        raise HTTPException(status_code=404, detail=f"saved query {saved_query_id!r} not found")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return SavedQueryOut.model_validate(row)
    try:
        updated = await repository.update(row, changes)
    except ValueError as exc:  # unique (project_id, name) collision
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SavedQueryOut.model_validate(updated)


@router.delete(
    "/saved-queries/{saved_query_id}",
    operation_id="deleteSavedQuery",
    status_code=204,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def delete_saved_query(
    saved_query_id: str, identity: CurrentIdentity, repository: RepositoryDep
) -> None:
    row = await repository.get(saved_query_id)
    if row is None or not _visible(identity, row):
        raise HTTPException(status_code=404, detail=f"saved query {saved_query_id!r} not found")
    await repository.delete(row)
