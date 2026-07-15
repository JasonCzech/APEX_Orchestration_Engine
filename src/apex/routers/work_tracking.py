"""/work-tracking: NL query translation, tracker passthrough, saved-query CRUD.

Adapter resolution follows the M2 pattern: get_connection_resolver().resolve(
WORK_TRACKING, connection_id=<body or ?connection_id>, project_id=<selected scoped
project or None>) — explicit connection > project row > global row > stub
fallback. Every passthrough endpoint accepts an optional connection_id query
parameter; scoped consumers with multiple projects must also select a project
with ?project=... so adapter resolution and provider query constraints match.
The two POST /query endpoints additionally accept connection_id in the body
(the body value wins when both are present).

Error contract (adapters raise, this router translates to problem details):
- KeyError (unknown item / unknown connection)  -> 404
- ValueError (bad query, rejected request)      -> 422 (409 for connection
  config conflicts at resolution time and saved-query name collisions)
- RuntimeError / httpx transport failures       -> 502 with the adapter message

Saved queries are project-scoped like M2 documents: global rows (project_id
NULL) are visible to everyone, scoped rows only to consumers with that project
scope. Mutations require operator+ and must not widen an app-only identity:
global rows require an unscoped admin, while project rows require project-wide
scope.
"""

import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
from apex.domain.input_limits import (
    MAX_DB_LIST_OFFSET,
    MAX_DESCRIPTION_CHARS,
    NoNulStr,
    RecordId,
    ResourceId,
    ScopeId,
)
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
from apex.persistence.repositories.work_item_mutations import (
    MutationClaimedError,
    MutationConnectionChangedError,
    MutationPayloadConflictError,
    MutationRetiredError,
)
from apex.services.connections import ConnectionResolver, internal_project_binding
from apex.services.work_item_mutations import (
    WorkItemMutationOutcomeAmbiguousError,
    WorkItemMutationService,
    get_work_item_mutation_service,
)
from apex.services.work_tracking import (
    get_saved_queries_repository,
    get_work_tracking_resolver,
    validate_provider_page,
)

router = APIRouter(prefix="/work-tracking", tags=["work-tracking"])

ResolverDep = Annotated[ConnectionResolver, Depends(get_work_tracking_resolver)]
RepositoryDep = Annotated[SavedQueriesRepository, Depends(get_saved_queries_repository)]
MutationServiceDep = Annotated[WorkItemMutationService, Depends(get_work_item_mutation_service)]
IdempotencyKeyHeader = Annotated[
    NoNulStr,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description=(
            "Caller-stable key for durable at-most-once provider dispatch and reconciliation; "
            "ambiguous outcomes return 409 and are never blindly redispatched"
        ),
    ),
]
ConnectionIdParam = Annotated[
    RecordId | None,
    Query(description="Explicit work-tracking connection id (default: resolved)"),
]
ProjectParam = Annotated[
    ScopeId | None,
    Query(description="Project used to resolve scoped work-tracking connections"),
]


# ── Schemas ──────────────────────────────────────────────────────────────────


class TranslateQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: NoNulStr = Field(min_length=1, max_length=20_000)
    connection_id: NoNulStr | None = Field(default=None, min_length=1, max_length=32)


class ExecuteQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: TranslatedQuery
    connection_id: NoNulStr | None = Field(default=None, min_length=1, max_length=32)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1_000)


class SavedQueryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NoNulStr = Field(min_length=1, max_length=255)
    provider: NoNulStr = Field(min_length=1, max_length=64)
    query: NoNulStr = Field(min_length=1, max_length=20_000)
    project_id: ScopeId | None = None
    description: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)


class SavedQueryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NoNulStr | None = Field(default=None, min_length=1, max_length=255)
    provider: NoNulStr | None = Field(default=None, min_length=1, max_length=64)
    query: NoNulStr | None = Field(default=None, min_length=1, max_length=20_000)
    description: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)


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


def _selected_project(identity: ConsumerIdentity, project: str | None) -> str | None:
    """Resolve the single project used for adapter selection.

    None means default connection. Scoped multi-project consumers must choose explicitly so
    the adapter cannot be resolved for one project while a provider query is
    constrained to another set.
    """
    if project is not None:
        if not identity.is_unscoped:
            ensure_scope(identity, project_id=project)
        return project
    if identity.is_unscoped:
        return None
    project_ids = identity.scoped_project_ids()
    if len(project_ids) == 1:
        return project_ids[0]
    if not project_ids:
        raise HTTPException(
            status_code=403,
            detail="consumer has no project scope for work-tracking connections",
        )
    raise HTTPException(
        status_code=403,
        detail="project query parameter is required for multi-project work-tracking consumers",
    )


def select_work_tracking_project(identity: ConsumerIdentity, project: str | None) -> str | None:
    """Public project-selection policy shared by direct and context routes."""

    return _selected_project(identity, project)


async def _resolve_adapter(
    resolver: ConnectionResolver,
    identity: ConsumerIdentity,
    connection_id: str | None,
    project: str | None = None,
) -> "ResolvedWorkTrackingAdapter":
    selected_project = _selected_project(identity, project)
    try:
        resolve_with_metadata = getattr(resolver, "resolve_with_metadata", None)
        if callable(resolve_with_metadata):
            metadata_resolver = cast(Callable[..., Awaitable[Any]], resolve_with_metadata)
            resolved = await metadata_resolver(
                PortKind.WORK_TRACKING,
                connection_id=connection_id,
                project_id=selected_project,
            )
            adapter = resolved.adapter
            resolved_connection_id = resolved.connection_id
            persisted = bool(resolved.persisted)
            connection_version = resolved.connection_version
        else:  # compatibility for injected test/extension resolvers
            adapter = await resolver.resolve(
                PortKind.WORK_TRACKING,
                connection_id=connection_id,
                project_id=selected_project,
            )
            resolved_connection_id = connection_id or "dev-work-tracking-fake"
            persisted = False
            connection_version = None
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work-tracking connection not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="work-tracking connection conflict") from exc
    provider = getattr(adapter, "provider", None)
    if not isinstance(provider, str) or not provider.strip():
        raise HTTPException(
            status_code=409,
            detail="resolved work-tracking adapter does not declare its provider",
        )
    return ResolvedWorkTrackingAdapter(
        adapter=adapter,
        provider=provider.casefold(),
        selected_project=selected_project,
        connection_id=resolved_connection_id,
        connection_persisted=persisted,
        connection_version=connection_version,
    )


@dataclass(frozen=True)
class ResolvedWorkTrackingAdapter:
    adapter: Any
    provider: str
    selected_project: str | None
    connection_id: str
    connection_persisted: bool
    connection_version: datetime | None


@dataclass(frozen=True)
class WorkTrackingMutationConnection:
    """Mutation connection selected before provider adapter construction."""

    provider: str
    selected_project: str | None
    connection_id: str
    connection_persisted: bool
    connection_version: datetime | None
    resolver_metadata: Any | None = None
    prebuilt: ResolvedWorkTrackingAdapter | None = None


async def _select_mutation_connection(
    resolver: ConnectionResolver,
    identity: ConsumerIdentity,
    connection_id: str | None,
    project: str | None,
) -> WorkTrackingMutationConnection:
    selected_project = _selected_project(identity, project)
    resolve_metadata = getattr(resolver, "resolve_metadata", None)
    build_from_metadata = getattr(resolver, "build_from_metadata", None)
    if callable(resolve_metadata) and callable(build_from_metadata):
        try:
            metadata = await cast(Callable[..., Awaitable[Any]], resolve_metadata)(
                PortKind.WORK_TRACKING,
                connection_id=connection_id,
                project_id=selected_project,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="work-tracking connection not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=409, detail="work-tracking connection conflict"
            ) from exc
        provider = metadata.config.provider
        if not isinstance(provider, str) or not provider.strip():
            raise HTTPException(
                status_code=409,
                detail="resolved work-tracking connection does not declare its provider",
            )
        return WorkTrackingMutationConnection(
            provider=provider.casefold(),
            selected_project=selected_project,
            connection_id=metadata.config.id,
            connection_persisted=bool(metadata.persisted),
            connection_version=metadata.connection_version,
            resolver_metadata=metadata,
        )

    # Compatibility for injected resolvers that expose only the legacy API.
    binding = await _resolve_adapter(resolver, identity, connection_id, selected_project)
    _require_project_bound(binding)
    return WorkTrackingMutationConnection(
        provider=binding.provider,
        selected_project=binding.selected_project,
        connection_id=binding.connection_id,
        connection_persisted=binding.connection_persisted,
        connection_version=binding.connection_version,
        prebuilt=binding,
    )


async def _build_mutation_adapter(
    resolver: ConnectionResolver,
    selection: WorkTrackingMutationConnection,
) -> ResolvedWorkTrackingAdapter:
    if selection.prebuilt is not None:
        return selection.prebuilt
    build = cast(Callable[..., Awaitable[Any]], resolver.build_from_metadata)
    resolved = await build(selection.resolver_metadata)
    adapter = resolved.adapter
    provider = getattr(adapter, "provider", None)
    if not isinstance(provider, str) or provider.casefold() != selection.provider:
        raise ValueError("resolved work-tracking adapter provider changed during construction")
    binding = ResolvedWorkTrackingAdapter(
        adapter=adapter,
        provider=selection.provider,
        selected_project=selection.selected_project,
        connection_id=resolved.connection_id,
        connection_persisted=bool(resolved.persisted),
        connection_version=resolved.connection_version,
    )
    _require_project_bound(binding)
    return binding


def _require_project_bound(binding: ResolvedWorkTrackingAdapter) -> None:
    """Fail closed for scoped real-provider adapters lacking an APEX binding."""

    if binding.selected_project is None or binding.provider in {"stub", "fake"}:
        return
    configured = internal_project_binding(binding.adapter)
    if (
        not isinstance(configured, str)
        or configured.casefold() != binding.selected_project.casefold()
    ):
        raise HTTPException(
            status_code=403,
            detail="resolved work-tracking connection is not bound to the selected project",
        )


def _external_query_project(binding: ResolvedWorkTrackingAdapter) -> str | None:
    """Provider project/key after internal APEX ownership has been proven."""

    if binding.selected_project is None:
        return None
    if binding.provider in {"stub", "fake"}:
        return binding.selected_project
    configured = getattr(binding.adapter, "project_id", None)
    if not isinstance(configured, str) or not configured.strip():
        raise HTTPException(
            status_code=409,
            detail="resolved work-tracking adapter has no external project configured",
        )
    return configured


def _require_matching_provider(
    binding: ResolvedWorkTrackingAdapter, query: TranslatedQuery
) -> None:
    if query.provider.casefold() != binding.provider:
        raise HTTPException(
            status_code=403,
            detail="work query provider does not match the resolved connection",
        )


async def resolve_scoped_work_tracking_adapter(
    resolver: ConnectionResolver,
    identity: ConsumerIdentity,
    connection_id: str | None = None,
    project: str | None = None,
) -> ResolvedWorkTrackingAdapter:
    """Resolve an adapter and prove its direct-item project boundary."""

    binding = await _resolve_adapter(resolver, identity, connection_id, project)
    _require_project_bound(binding)
    return binding


async def get_work_tracking_adapter(
    identity: CurrentIdentity,
    resolver: ResolverDep,
    connection_id: ConnectionIdParam = None,
    project: ProjectParam = None,
) -> ResolvedWorkTrackingAdapter:
    return await resolve_scoped_work_tracking_adapter(resolver, identity, connection_id, project)


AdapterDep = Annotated[ResolvedWorkTrackingAdapter, Depends(get_work_tracking_adapter)]


def _provider_page(offset: int, limit: int) -> Page:
    try:
        return validate_provider_page(Page(offset=offset, limit=limit))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid work-item page") from exc


@contextmanager
def adapter_errors() -> Iterator[None]:
    """Translate adapter exceptions into problem details (see module doc)."""
    try:
        yield
    except MutationPayloadConflictError as exc:
        raise HTTPException(status_code=409, detail="work-item mutation payload conflict") from exc
    except MutationClaimedError as exc:
        raise HTTPException(
            status_code=409, detail="work-item mutation is already claimed"
        ) from exc
    except MutationRetiredError as exc:
        raise HTTPException(status_code=409, detail="work-item mutation is retired") from exc
    except MutationConnectionChangedError as exc:
        raise HTTPException(
            status_code=409, detail="work-item mutation connection changed"
        ) from exc
    except WorkItemMutationOutcomeAmbiguousError as exc:
        raise HTTPException(
            status_code=409,
            detail="work-item mutation outcome is ambiguous",
            headers={"Retry-After": "5"},
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="work item not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="work tracker rejected the request") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="work tracker upstream failure") from exc
    except httpx.HTTPError as exc:  # defensive: adapters normally wrap transport errors
        raise HTTPException(status_code=502, detail="work tracker upstream failure") from exc


def _visible(identity: ConsumerIdentity, row: SavedQuery) -> bool:
    """Global rows (project_id NULL) are visible to everyone; scoped rows need scope."""
    return row.project_id is None or identity.allows_project(row.project_id)


def _ensure_saved_query_write(identity: ConsumerIdentity, project_id: str | None) -> None:
    """Require authority matching the full audience of the saved query."""

    allowed = (
        identity.is_unscoped
        if project_id is None
        else identity.contains_scope(ScopeRef(project_id=project_id))
    )
    if not allowed:
        audience = "global" if project_id is None else "project-wide"
        raise HTTPException(
            status_code=403,
            detail=f"{audience} saved-query mutations require matching administrative scope",
        )


def _ensure_work_item_write(identity: ConsumerIdentity, project_id: str | None) -> None:
    """Work items are project-wide provider resources, never app-local ones."""

    allowed = (
        identity.is_unscoped
        if project_id is None
        else identity.contains_scope(ScopeRef(project_id=project_id))
    )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="work-item mutations require project-wide administrative scope",
        )


_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)
_WIQL_WHERE = re.compile(r"\bwhere\b", re.IGNORECASE)
_JQL_PROJECT = re.compile(
    r"\bproject\s*(?:=|in)\s*(?P<value>\([^)]*\)|\"[^\"]+\"|'[^']+'|[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_WIQL_PROJECT = re.compile(
    r"\[System\.TeamProject\]\s*(?:=|in)\s*"
    r"(?P<value>\([^)]*\)|\"[^\"]+\"|'[^']+'|[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


def _constrain_translated_query(
    query: TranslatedQuery, allowed: tuple[str, ...] | None
) -> TranslatedQuery:
    if allowed is None:
        return query
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="consumer has no project scope for work queries",
        )

    provider = query.provider.lower()
    if provider == "jira":
        scoped = _constrain_jql(query.query, allowed)
    elif provider == "ado":
        scoped = _constrain_wiql(query.query, allowed)
    elif provider in {"stub", "fake"}:
        scoped = query.query
    else:
        raise HTTPException(
            status_code=403,
            detail="work-tracking provider does not support provable project scoping",
        )
    return query.model_copy(update={"query": scoped})


def _constrain_jql(raw: str, allowed: tuple[str, ...]) -> str:
    _validate_query_envelope(raw)
    _reject_conflicting_projects(_JQL_PROJECT.findall(raw), allowed)
    predicate, order = _split_order_by(raw)
    scope = f"project in ({', '.join(_jql_value(project) for project in allowed)})"
    if predicate:
        return f"{scope} AND ({predicate}){_prefixed(order)}"
    return f"{scope}{_prefixed(order)}"


def _constrain_wiql(raw: str, allowed: tuple[str, ...]) -> str:
    _validate_query_envelope(raw)
    _reject_conflicting_projects(_WIQL_PROJECT.findall(raw), allowed)
    scope = f"[System.TeamProject] IN ({', '.join(_wiql_value(project) for project in allowed)})"
    order_match = _top_level_match(raw, _ORDER_BY)
    head = raw[: order_match.start()].strip() if order_match else raw.strip()
    order = raw[order_match.start() :].strip() if order_match else ""
    where_match = _top_level_match(head, _WIQL_WHERE)
    if where_match:
        prefix = head[: where_match.end()].strip()
        predicate = head[where_match.end() :].strip()
        if predicate:
            return f"{prefix} {scope} AND ({predicate}){_prefixed(order)}"
        return f"{prefix} {scope}{_prefixed(order)}"
    return f"{head} WHERE {scope}{_prefixed(order)}"


def _split_order_by(raw: str) -> tuple[str, str]:
    match = _top_level_match(raw, _ORDER_BY)
    if match is None:
        return raw.strip(), ""
    return raw[: match.start()].strip(), raw[match.start() :].strip()


def _validate_query_envelope(raw: str) -> None:
    """Reject syntax that can escape the server-added project grouping.

    This is deliberately a small envelope validator, not a provider parser. It
    proves that quotes and parentheses are balanced and rejects statement/comment
    syntax before an opaque JQL/WIQL predicate is wrapped in an authorization
    clause. The provider remains responsible for validating the predicate itself.
    """

    depth = 0
    quote: str | None = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                # WIQL/SQL strings escape quotes by doubling them.
                if index + 1 < len(raw) and raw[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                _unsafe_query_envelope()
        elif char == ";" or raw.startswith(("--", "//", "/*", "*/"), index):
            _unsafe_query_envelope()
        index += 1
    if quote is not None or depth != 0:
        _unsafe_query_envelope()


def _unsafe_query_envelope() -> None:
    raise HTTPException(
        status_code=422,
        detail="work query contains unsafe grouping, comment, or statement syntax",
    )


def _top_level_match(raw: str, pattern: re.Pattern[str]) -> re.Match[str] | None:
    """Find a provider keyword outside quoted strings and nested predicates."""

    depth = 0
    quote: str | None = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                if index + 1 < len(raw) and raw[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0:
            match = pattern.match(raw, index)
            if match is not None:
                return match
        index += 1
    return None


def _prefixed(value: str) -> str:
    return f" {value}" if value else ""


def _reject_conflicting_projects(raw_values: list[str], allowed: tuple[str, ...]) -> None:
    allowed_set = set(allowed)
    requested = {
        project
        for raw in raw_values
        for project in _project_values(raw)
        if project not in allowed_set
    }
    if requested:
        raise HTTPException(
            status_code=403,
            detail="work query references a project outside scope",
        )


def _project_values(raw: str) -> tuple[str, ...]:
    value = raw.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    parts = [part.strip().strip("\"'") for part in value.split(",")]
    return tuple(part for part in parts if part)


def _jql_value(value: str) -> str:
    if value.replace("_", "").replace("-", "").isalnum():
        return value
    # The external project key is deployment-owned connection data, but scoped
    # administrators can manage their own connection. Treat it as data even
    # here: an unescaped quote could otherwise break out of the injected JQL
    # project predicate and widen a tenant-scoped search.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _wiql_value(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ── Query passthrough ────────────────────────────────────────────────────────


@router.post("/query/translate", operation_id="translateWorkQuery", response_model=TranslatedQuery)
async def translate_work_query(
    body: TranslateQueryRequest,
    identity: CurrentIdentity,
    resolver: ResolverDep,
    connection_id: ConnectionIdParam = None,
    project: ProjectParam = None,
) -> Any:
    selected_project = _selected_project(identity, project)
    binding = await _resolve_adapter(
        resolver, identity, body.connection_id or connection_id, selected_project
    )
    _require_project_bound(binding)
    external_project = _external_query_project(binding)
    context = QueryContext(
        project_id=selected_project,
        hints={"project": external_project} if external_project is not None else {},
    )
    with adapter_errors():
        translated = await binding.adapter.translate_query(body.text, context=context)
    if translated.provider.casefold() != binding.provider:
        raise HTTPException(
            status_code=502,
            detail="work-tracking adapter returned an inconsistent provider",
        )
    return translated


@router.post("/query/execute", operation_id="executeWorkQuery", response_model=WorkItemPage)
async def execute_work_query(
    body: ExecuteQueryRequest,
    identity: CurrentIdentity,
    resolver: ResolverDep,
    connection_id: ConnectionIdParam = None,
    project: ProjectParam = None,
) -> Any:
    page = _provider_page(body.offset, body.limit)
    selected_project = _selected_project(identity, project)
    binding = await _resolve_adapter(
        resolver, identity, body.connection_id or connection_id, selected_project
    )
    _require_project_bound(binding)
    _require_matching_provider(binding, body.query)
    external_project = _external_query_project(binding)
    allowed_projects = (external_project,) if external_project is not None else None
    scoped_query = _constrain_translated_query(body.query, allowed_projects)
    with adapter_errors():
        return await binding.adapter.execute_query(scoped_query, page=page)


# ── Item passthrough ─────────────────────────────────────────────────────────


@router.get("/items", operation_id="listWorkItems", response_model=WorkItemPage)
async def list_work_items(
    binding: AdapterDep,
    status: Annotated[NoNulStr | None, Query(max_length=255)] = None,
    kind: Annotated[NoNulStr | None, Query(max_length=64)] = None,
    q: Annotated[NoNulStr | None, Query(max_length=2_000)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=999)] = 0,
) -> Any:
    filters = WorkItemFilters(status=status, kind=kind, text=q)
    page = _provider_page(offset, limit)
    with adapter_errors():
        return await binding.adapter.list_items(filters, page=page)


@router.get("/items/{key}", operation_id="getWorkItem", response_model=WorkItem)
async def get_work_item(key: Annotated[ResourceId, Path()], binding: AdapterDep) -> Any:
    with adapter_errors():
        return await binding.adapter.get_item(key)


@router.post(
    "/items",
    operation_id="createWorkItem",
    status_code=201,
    response_model=WorkItem,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def create_work_item(
    draft: WorkItemDraft,
    identity: CurrentIdentity,
    resolver: ResolverDep,
    mutations: MutationServiceDep,
    idempotency_key: IdempotencyKeyHeader,
    connection_id: ConnectionIdParam = None,
    project: ProjectParam = None,
) -> Any:
    selected_project = _selected_project(identity, project)
    _ensure_work_item_write(identity, selected_project)
    selection = await _select_mutation_connection(
        resolver, identity, connection_id, selected_project
    )
    with adapter_errors():
        replay_create = getattr(mutations, "replay_create", None)
        replay = (
            await cast(Callable[..., Awaitable[Any]], replay_create)(
                draft=draft,
                identity=identity,
                project_id=selection.selected_project,
                connection_id=selection.connection_id,
                connection_persisted=selection.connection_persisted,
                connection_version=selection.connection_version,
                idempotency_key=idempotency_key,
            )
            if callable(replay_create)
            else None
        )
        if replay is not None:
            return replay
        binding = await _build_mutation_adapter(resolver, selection)
        return await mutations.create(
            adapter=binding.adapter,
            draft=draft,
            identity=identity,
            project_id=binding.selected_project,
            connection_id=binding.connection_id,
            connection_persisted=binding.connection_persisted,
            connection_version=binding.connection_version,
            idempotency_key=idempotency_key,
        )


@router.post(
    "/items/{key}/enrich",
    operation_id="enrichWorkItem",
    response_model=WorkItem,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def enrich_work_item(
    key: Annotated[ResourceId, Path()],
    enrichment: Enrichment,
    identity: CurrentIdentity,
    resolver: ResolverDep,
    mutations: MutationServiceDep,
    idempotency_key: IdempotencyKeyHeader,
    connection_id: ConnectionIdParam = None,
    project: ProjectParam = None,
) -> Any:
    selected_project = _selected_project(identity, project)
    _ensure_work_item_write(identity, selected_project)
    selection = await _select_mutation_connection(
        resolver, identity, connection_id, selected_project
    )
    with adapter_errors():
        replay_enrich = getattr(mutations, "replay_enrich", None)
        replay = (
            await cast(Callable[..., Awaitable[Any]], replay_enrich)(
                key=key,
                enrichment=enrichment,
                identity=identity,
                project_id=selection.selected_project,
                connection_id=selection.connection_id,
                connection_persisted=selection.connection_persisted,
                connection_version=selection.connection_version,
                idempotency_key=idempotency_key,
            )
            if callable(replay_enrich)
            else None
        )
        if replay is not None:
            return replay
        binding = await _build_mutation_adapter(resolver, selection)
        return await mutations.enrich(
            adapter=binding.adapter,
            key=key,
            enrichment=enrichment,
            identity=identity,
            project_id=binding.selected_project,
            connection_id=binding.connection_id,
            connection_persisted=binding.connection_persisted,
            connection_version=binding.connection_version,
            idempotency_key=idempotency_key,
        )


# ── Saved queries ────────────────────────────────────────────────────────────


@router.get(
    "/saved-queries", operation_id="listSavedQueries", response_model=SavedQueryListResponse
)
async def list_saved_queries(
    identity: CurrentIdentity,
    repository: RepositoryDep,
    project: Annotated[ScopeId | None, Query()] = None,
    provider: Annotated[NoNulStr | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> Any:
    ensure_scope(identity, project_id=project)
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
    _ensure_saved_query_write(identity, body.project_id)
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
        raise HTTPException(status_code=409, detail="saved query name already exists") from exc


@router.get("/saved-queries/{saved_query_id}", operation_id="getSavedQuery")
async def get_saved_query(
    saved_query_id: Annotated[RecordId, Path()],
    identity: CurrentIdentity,
    repository: RepositoryDep,
) -> SavedQueryOut:
    row = await repository.get(saved_query_id)
    if row is None or not _visible(identity, row):
        raise HTTPException(status_code=404, detail="saved query not found")
    return SavedQueryOut.model_validate(row)


@router.patch(
    "/saved-queries/{saved_query_id}",
    operation_id="updateSavedQuery",
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def update_saved_query(
    saved_query_id: Annotated[RecordId, Path()],
    body: SavedQueryUpdate,
    identity: CurrentIdentity,
    repository: RepositoryDep,
) -> SavedQueryOut:
    row = await repository.get_for_update(saved_query_id)
    if row is None or not _visible(identity, row):
        raise HTTPException(status_code=404, detail="saved query not found")
    _ensure_saved_query_write(identity, row.project_id)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return SavedQueryOut.model_validate(row)
    try:
        updated = await repository.update(row, changes)
    except ValueError as exc:  # unique (project_id, name) collision
        raise HTTPException(status_code=409, detail="saved query name already exists") from exc
    return SavedQueryOut.model_validate(updated)


@router.delete(
    "/saved-queries/{saved_query_id}",
    operation_id="deleteSavedQuery",
    status_code=204,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def delete_saved_query(
    saved_query_id: Annotated[RecordId, Path()],
    identity: CurrentIdentity,
    repository: RepositoryDep,
) -> None:
    row = await repository.get_for_update(saved_query_id)
    if row is None or not _visible(identity, row):
        raise HTTPException(status_code=404, detail="saved query not found")
    _ensure_saved_query_write(identity, row.project_id)
    await repository.delete(row)
