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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Annotated, Any, Never, cast

import httpx
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
from apex.domain.input_limits import (
    MAX_DB_LIST_OFFSET,
    MAX_DESCRIPTION_CHARS,
    MAX_SCOPE_ID_CHARS,
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
from apex.persistence.repositories.saved_queries import (
    SavedQueriesRepository,
    SavedQueryNameConflictError,
)
from apex.persistence.repositories.work_item_mutations import (
    MutationClaimedError,
    MutationConnectionChangedError,
    MutationPayloadConflictError,
    MutationRetiredError,
)
from apex.services.connection_credentials import (
    reject_credential_text,
    sanitize_credential_text_for_output,
)
from apex.services.connections import (
    ConnectionResolver,
    close_adapter,
    internal_project_binding,
)
from apex.services.work_item_mutations import (
    WorkItemMutationOutcomeAmbiguousError,
    WorkItemMutationService,
    get_work_item_mutation_service,
)
from apex.services.work_items import (
    validated_provider_query,
    validated_provider_work_item,
    validated_provider_work_item_page,
)
from apex.services.work_tracking import (
    get_saved_queries_repository,
    get_work_tracking_resolver,
    validate_provider_page,
)

router = APIRouter(prefix="/work-tracking", tags=["work-tracking"])
logger = structlog.get_logger(__name__)

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


def _sanitize_saved_query_output_text(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    required: bool,
) -> str | None:
    """Quarantine malformed legacy text without reflecting or truncating it."""

    safe = sanitize_credential_text_for_output(value)
    if safe is None:
        return "[REDACTED]" if required else None
    if not minimum <= len(safe) <= maximum or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in safe
    ):
        return "[REDACTED]"
    return safe


def _work_tracking_resolution_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="work-tracking connection unavailable")


def _invalid_work_tracking_connection() -> HTTPException:
    return HTTPException(status_code=409, detail="invalid work-tracking connection")


def _normalized_provider(value: Any) -> str | None:
    """Validate resolver/provider identity without invoking scalar-subclass hooks."""

    if (
        type(value) is not str
        or not 1 <= len(value) <= 64
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        return None
    return value.casefold()


def _valid_connection_id(value: Any) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 32
        and value == value.strip()
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def _valid_connection_version(value: Any) -> bool:
    return value is None or type(value) is datetime


async def _close_work_tracking_adapter(adapter: Any) -> None:
    """Settle provider cleanup without replacing a durable result or stable error."""

    try:
        await close_adapter(adapter)
    except Exception:
        logger.warning("apex.work_tracking.adapter_close_failed")


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
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: NoNulStr = Field(min_length=1, max_length=255)
    provider: NoNulStr = Field(min_length=1, max_length=64)
    query: NoNulStr = Field(min_length=1, max_length=20_000)
    project_id: ScopeId | None = None
    description: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)

    @field_validator("name", "provider", "query", "description")
    @classmethod
    def reject_raw_credential_scalars(cls, value: str | None) -> str | None:
        return reject_credential_text(value, label="saved query text field")


class SavedQueryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: NoNulStr | None = Field(default=None, min_length=1, max_length=255)
    provider: NoNulStr | None = Field(default=None, min_length=1, max_length=64)
    query: NoNulStr | None = Field(default=None, min_length=1, max_length=20_000)
    description: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)

    @field_validator("name", "provider", "query")
    @classmethod
    def reject_null_required_fields(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @field_validator("name", "provider", "query", "description")
    @classmethod
    def reject_raw_credential_scalars(cls, value: str | None) -> str | None:
        return reject_credential_text(value, label="saved query text field")


class SavedQueryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, hide_input_in_errors=True)

    id: str
    name: str
    project_id: str | None = None
    provider: str
    query: str
    description: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("id", "name", "provider", "query", mode="before")
    @classmethod
    def sanitize_required_legacy_text(cls, value: Any, info: Any) -> str:
        bounds = {
            "id": (1, 32),
            "name": (1, 255),
            "provider": (1, 64),
            "query": (1, 20_000),
        }
        minimum, maximum = bounds[info.field_name]
        return (
            _sanitize_saved_query_output_text(
                value,
                minimum=minimum,
                maximum=maximum,
                required=True,
            )
            or "[REDACTED]"
        )

    @field_validator("project_id", "description", "created_by", mode="before")
    @classmethod
    def sanitize_optional_legacy_text(cls, value: Any, info: Any) -> str | None:
        bounds = {
            "project_id": (1, MAX_SCOPE_ID_CHARS),
            "description": (0, MAX_DESCRIPTION_CHARS),
            "created_by": (1, 255),
        }
        minimum, maximum = bounds[info.field_name]
        return _sanitize_saved_query_output_text(
            value,
            minimum=minimum,
            maximum=maximum,
            required=False,
        )


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
    resolution_error: HTTPException | None = None
    adapter: Any = None
    resolved_connection_id: str | None = None
    persisted = False
    connection_version: datetime | None = None
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
            if type(resolved.persisted) is not bool:
                raise ValueError("invalid persisted-connection marker")
            persisted = resolved.persisted
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
    except KeyError:
        resolution_error = HTTPException(
            status_code=404, detail="work-tracking connection not found"
        )
    except ValueError:
        resolution_error = HTTPException(
            status_code=409, detail="work-tracking connection conflict"
        )
    except Exception:
        resolution_error = _work_tracking_resolution_unavailable()
    if resolution_error is not None:
        if adapter is not None:
            await _close_work_tracking_adapter(adapter)
        raise resolution_error
    assert adapter is not None and resolved_connection_id is not None
    binding_error: HTTPException | None = None
    binding: ResolvedWorkTrackingAdapter | None = None
    try:
        provider = getattr(adapter, "provider", None)
        normalized_provider = _normalized_provider(provider)
        if (
            normalized_provider is None
            or not _valid_connection_id(resolved_connection_id)
            or not _valid_connection_version(connection_version)
        ):
            binding_error = _invalid_work_tracking_connection()
        else:
            binding = ResolvedWorkTrackingAdapter(
                adapter=adapter,
                provider=normalized_provider,
                selected_project=selected_project,
                connection_id=resolved_connection_id,
                connection_persisted=persisted,
                connection_version=connection_version,
            )
    except Exception:
        binding_error = _invalid_work_tracking_connection()
    if binding_error is not None:
        await _close_work_tracking_adapter(adapter)
        raise binding_error
    assert binding is not None
    return binding


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
    adapter_builder: Callable[[Any], Awaitable[Any]] | None = None
    prebuilt: ResolvedWorkTrackingAdapter | None = None


async def _select_mutation_connection(
    resolver: ConnectionResolver,
    identity: ConsumerIdentity,
    connection_id: str | None,
    project: str | None,
) -> WorkTrackingMutationConnection:
    selected_project = _selected_project(identity, project)
    resolver_surface_error = False
    resolve_metadata: Any = None
    build_from_metadata: Any = None
    try:
        resolve_metadata = getattr(resolver, "resolve_metadata", None)
        build_from_metadata = getattr(resolver, "build_from_metadata", None)
    except Exception:
        resolver_surface_error = True
    if resolver_surface_error:
        raise _work_tracking_resolution_unavailable()
    if callable(resolve_metadata) and callable(build_from_metadata):
        resolution_error: HTTPException | None = None
        metadata: Any = None
        try:
            metadata = await cast(Callable[..., Awaitable[Any]], resolve_metadata)(
                PortKind.WORK_TRACKING,
                connection_id=connection_id,
                project_id=selected_project,
            )
        except KeyError:
            resolution_error = HTTPException(
                status_code=404, detail="work-tracking connection not found"
            )
        except ValueError:
            resolution_error = HTTPException(
                status_code=409, detail="work-tracking connection conflict"
            )
        except Exception:
            resolution_error = _work_tracking_resolution_unavailable()
        if resolution_error is not None:
            raise resolution_error
        assert metadata is not None
        metadata_error: HTTPException | None = None
        provider: str | None = None
        metadata_connection_id: str | None = None
        metadata_persisted: bool | None = None
        metadata_version: datetime | None = None
        try:
            provider = _normalized_provider(metadata.config.provider)
            metadata_connection_id = metadata.config.id
            metadata_persisted = metadata.persisted
            metadata_version = metadata.connection_version
            if (
                provider is None
                or not _valid_connection_id(metadata_connection_id)
                or type(metadata_persisted) is not bool
                or not _valid_connection_version(metadata_version)
            ):
                metadata_error = _invalid_work_tracking_connection()
        except Exception:
            metadata_error = _invalid_work_tracking_connection()
        if metadata_error is not None:
            raise metadata_error
        assert provider is not None
        assert metadata_connection_id is not None
        assert metadata_persisted is not None
        return WorkTrackingMutationConnection(
            provider=provider,
            selected_project=selected_project,
            connection_id=metadata_connection_id,
            connection_persisted=metadata_persisted,
            connection_version=metadata_version,
            resolver_metadata=metadata,
            adapter_builder=cast(Callable[[Any], Awaitable[Any]], build_from_metadata),
        )

    # Compatibility for injected resolvers that expose only the legacy API.
    binding = await resolve_scoped_work_tracking_adapter(
        resolver,
        identity,
        connection_id,
        selected_project,
    )
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
    del resolver
    build = selection.adapter_builder
    if build is None:
        raise RuntimeError("work-tracking adapter builder is unavailable")
    resolved = await build(selection.resolver_metadata)
    adapter = resolved.adapter
    try:
        provider = _normalized_provider(getattr(adapter, "provider", None))
        if (
            provider != selection.provider
            or type(resolved.connection_id) is not str
            or resolved.connection_id != selection.connection_id
            or type(resolved.persisted) is not bool
            or resolved.persisted is not selection.connection_persisted
            or not _valid_connection_version(resolved.connection_version)
            or resolved.connection_version != selection.connection_version
        ):
            raise RuntimeError(
                "resolved work-tracking adapter identity changed during construction"
            )
        binding = ResolvedWorkTrackingAdapter(
            adapter=adapter,
            provider=selection.provider,
            selected_project=selection.selected_project,
            connection_id=selection.connection_id,
            connection_persisted=selection.connection_persisted,
            connection_version=selection.connection_version,
        )
        _require_project_bound(binding)
        return binding
    except BaseException:
        await _close_work_tracking_adapter(adapter)
        raise


def _require_project_bound(binding: ResolvedWorkTrackingAdapter) -> None:
    """Fail closed unless a scoped real provider has both ownership boundaries.

    The persisted APEX project binding prevents one tenant from selecting another
    tenant's connection.  The provider project/key separately prevents that
    connection's broader credential from reading or mutating sibling projects at
    the upstream tracker.  Every scoped route needs both checks; query translation
    alone is not a sufficient boundary for direct item and mutation operations.
    """

    if binding.selected_project is None or binding.provider in {"stub", "fake"}:
        return
    configured = internal_project_binding(binding.adapter)
    if type(configured) is not str or configured.casefold() != binding.selected_project.casefold():
        raise HTTPException(
            status_code=403,
            detail="resolved work-tracking connection is not bound to the selected project",
        )
    # Direct list/get/create/enrich routes do not pass through query-envelope
    # constraining.  Require the same external-project proof used by translated
    # queries before any of those routes can reach a provider credential.
    _external_query_project(binding)


def _external_query_project(binding: ResolvedWorkTrackingAdapter) -> str | None:
    """Provider project/key after internal APEX ownership has been proven."""

    if binding.selected_project is None:
        return None
    if binding.provider in {"stub", "fake"}:
        return binding.selected_project
    configured = getattr(binding.adapter, "project_id", None)
    if (
        type(configured) is not str
        or not 1 <= len(configured) <= MAX_SCOPE_ID_CHARS
        or configured != configured.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in configured)
    ):
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
    try:
        _require_project_bound(binding)
        return binding
    except BaseException:
        await _close_work_tracking_adapter(binding.adapter)
        raise


async def get_work_tracking_adapter(
    identity: CurrentIdentity,
    resolver: ResolverDep,
    connection_id: ConnectionIdParam = None,
    project: ProjectParam = None,
) -> AsyncIterator[ResolvedWorkTrackingAdapter]:
    binding = await resolve_scoped_work_tracking_adapter(resolver, identity, connection_id, project)
    try:
        yield binding
    finally:
        await _close_work_tracking_adapter(binding.adapter)


AdapterDep = Annotated[ResolvedWorkTrackingAdapter, Depends(get_work_tracking_adapter)]


def _provider_page(offset: int, limit: int) -> Page:
    page_error: HTTPException | None = None
    page: Page | None = None
    try:
        page = validate_provider_page(Page(offset=offset, limit=limit))
    except ValueError:
        page_error = HTTPException(status_code=422, detail="invalid work-item page")
    if page_error is not None:
        raise page_error
    assert page is not None
    return page


class _AdapterErrorBoundary:
    """Suppress a raw adapter error, then expose only its stable translation."""

    def __init__(self) -> None:
        self._translated: HTTPException | None = None

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        if error_type is None:
            return False
        if issubclass(error_type, MutationPayloadConflictError):
            self._translated = HTTPException(
                status_code=409, detail="work-item mutation payload conflict"
            )
        elif issubclass(error_type, MutationClaimedError):
            self._translated = HTTPException(
                status_code=409, detail="work-item mutation is already claimed"
            )
        elif issubclass(error_type, MutationRetiredError):
            self._translated = HTTPException(
                status_code=409, detail="work-item mutation is retired"
            )
        elif issubclass(error_type, MutationConnectionChangedError):
            self._translated = HTTPException(
                status_code=409, detail="work-item mutation connection changed"
            )
        elif issubclass(error_type, WorkItemMutationOutcomeAmbiguousError):
            self._translated = HTTPException(
                status_code=409,
                detail="work-item mutation outcome is ambiguous",
                headers={"Retry-After": "5"},
            )
        elif issubclass(error_type, KeyError):
            self._translated = HTTPException(status_code=404, detail="work item not found")
        elif issubclass(error_type, ValueError):
            self._translated = HTTPException(
                status_code=422, detail="work tracker rejected the request"
            )
        elif issubclass(error_type, (RuntimeError, httpx.HTTPError)):
            self._translated = HTTPException(
                status_code=502, detail="work tracker upstream failure"
            )
        elif issubclass(error_type, Exception):
            self._translated = HTTPException(
                status_code=502, detail="work tracker upstream failure"
            )
        else:
            return False
        return True

    def raise_if_error(self) -> Never:
        if self._translated is not None:
            raise self._translated
        raise RuntimeError("adapter error boundary has no translated error")


def adapter_errors() -> _AdapterErrorBoundary:
    """Build an adapter boundary whose translation is raised after ``with``."""

    return _AdapterErrorBoundary()


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
_WIQL_WHERE = re.compile(r"(?<![@.A-Za-z0-9_])where\b", re.IGNORECASE)
_WIQL_SUFFIX = re.compile(
    r"(?<![@.A-Za-z0-9_])(?:order\s+by|asof|mode)\b",
    re.IGNORECASE,
)
_WIQL_LINKS_FROM = re.compile(r"\bfrom\s+workitemlinks\b", re.IGNORECASE)
_JQL_PROJECT = re.compile(
    r"\bproject\s*(?P<operator>=|in)\s*",
    re.IGNORECASE,
)
_WIQL_PROJECT = re.compile(
    r"\[System\.TeamProject\]\s*(?P<operator>=|in)\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _QueryDialect:
    backslash_escapes_quotes: bool
    doubled_quotes_escape: bool
    bracketed_identifiers: bool = False


_JQL_DIALECT = _QueryDialect(
    backslash_escapes_quotes=True,
    doubled_quotes_escape=False,
)
_WIQL_DIALECT = _QueryDialect(
    backslash_escapes_quotes=False,
    doubled_quotes_escape=True,
    bracketed_identifiers=True,
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
    _validate_query_envelope(raw, dialect=_JQL_DIALECT)
    _reject_conflicting_projects(
        _project_references(raw, _JQL_PROJECT, dialect=_JQL_DIALECT),
        allowed,
    )
    predicate, order = _split_order_by(raw)
    scope = f"project in ({', '.join(_jql_value(project) for project in allowed)})"
    if predicate:
        return f"{scope} AND ({predicate}){_prefixed(order)}"
    return f"{scope}{_prefixed(order)}"


def _constrain_wiql(raw: str, allowed: tuple[str, ...]) -> str:
    _validate_query_envelope(raw, dialect=_WIQL_DIALECT)
    _reject_conflicting_projects(
        _project_references(raw, _WIQL_PROJECT, dialect=_WIQL_DIALECT),
        allowed,
    )
    values = ", ".join(_wiql_value(project) for project in allowed)
    suffix_match = _top_level_match(raw, _WIQL_SUFFIX, dialect=_WIQL_DIALECT)
    head = raw[: suffix_match.start()].strip() if suffix_match else raw.strip()
    suffix = raw[suffix_match.start() :].strip() if suffix_match else ""
    if _top_level_match(head, _WIQL_LINKS_FROM, dialect=_WIQL_DIALECT):
        # Link queries can return identifiers from both ends of a relation.
        # Qualify and constrain both sides; an unqualified TeamProject field is
        # not valid in a WorkItemLinks WHERE clause and would fail only at the
        # provider boundary.
        scope = (
            f"([Source].[System.TeamProject] IN ({values}) AND "
            f"[Target].[System.TeamProject] IN ({values}))"
        )
    else:
        scope = f"[System.TeamProject] IN ({values})"
    where_match = _top_level_match(head, _WIQL_WHERE, dialect=_WIQL_DIALECT)
    if where_match:
        prefix = head[: where_match.end()].strip()
        predicate = head[where_match.end() :].strip()
        if predicate:
            return f"{prefix} {scope} AND ({predicate}){_prefixed(suffix)}"
        return f"{prefix} {scope}{_prefixed(suffix)}"
    return f"{head} WHERE {scope}{_prefixed(suffix)}"


def _split_order_by(raw: str) -> tuple[str, str]:
    match = _top_level_match(raw, _ORDER_BY, dialect=_JQL_DIALECT)
    if match is None:
        return raw.strip(), ""
    return raw[: match.start()].strip(), raw[match.start() :].strip()


def _validate_query_envelope(raw: str, *, dialect: _QueryDialect) -> None:
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
            if dialect.backslash_escapes_quotes and char == "\\":
                index += 2
                continue
            if char == quote:
                if (
                    dialect.doubled_quotes_escape
                    and index + 1 < len(raw)
                    and raw[index + 1] == quote
                ):
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if dialect.bracketed_identifiers and char == "[":
            closing = raw.find("]", index + 1)
            if closing < 0:
                _unsafe_query_envelope()
            index = closing + 1
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


def _top_level_match(
    raw: str,
    pattern: re.Pattern[str],
    *,
    dialect: _QueryDialect,
) -> re.Match[str] | None:
    """Find a provider keyword outside quoted strings and nested predicates."""

    depth = 0
    quote: str | None = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is not None:
            if dialect.backslash_escapes_quotes and char == "\\":
                index += 2
                continue
            if char == quote:
                if (
                    dialect.doubled_quotes_escape
                    and index + 1 < len(raw)
                    and raw[index + 1] == quote
                ):
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if dialect.bracketed_identifiers and char == "[":
            closing = raw.find("]", index + 1)
            if closing < 0:
                return None
            index = closing + 1
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


def _project_references(
    raw: str,
    pattern: re.Pattern[str],
    *,
    dialect: _QueryDialect,
) -> tuple[str, ...]:
    """Extract only statically named projects from real field expressions."""

    projects: list[str] = []
    for match in _unquoted_pattern_matches(raw, pattern, dialect=dialect):
        operator = match.group("operator").casefold()
        if operator == "in":
            projects.extend(_static_project_list(raw, match.end(), dialect=dialect))
        else:
            project = _static_project_scalar(raw, match.end(), dialect=dialect)
            if project is not None:
                projects.append(project)
    return tuple(projects)


def _unquoted_pattern_matches(
    raw: str,
    pattern: re.Pattern[str],
    *,
    dialect: _QueryDialect,
) -> tuple[re.Match[str], ...]:
    """Match at every nesting depth while skipping literals and other WIQL fields."""

    matches: list[re.Match[str]] = []
    quote: str | None = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is not None:
            if dialect.backslash_escapes_quotes and char == "\\":
                index += 2
                continue
            if char == quote:
                if (
                    dialect.doubled_quotes_escape
                    and index + 1 < len(raw)
                    and raw[index + 1] == quote
                ):
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        match = pattern.match(raw, index)
        if match is not None:
            matches.append(match)
            index = match.end()
            continue
        if dialect.bracketed_identifiers and char == "[":
            closing = raw.find("]", index + 1)
            if closing < 0:
                break
            index = closing + 1
            continue
        if char in {"'", '"'}:
            quote = char
        index += 1
    return tuple(matches)


def _static_project_list(
    raw: str,
    start: int,
    *,
    dialect: _QueryDialect,
) -> tuple[str, ...]:
    index = start
    while index < len(raw) and raw[index].isspace():
        index += 1
    if index >= len(raw) or raw[index] != "(":
        # Dynamic JQL project functions are still bounded by the injected scope.
        return ()
    closing = _matching_query_parenthesis(raw, index, dialect=dialect)
    if closing is None:
        return ()
    values: list[str] = []
    for segment in _split_query_values(raw[index + 1 : closing], dialect=dialect):
        value = _decode_static_project_value(segment, dialect=dialect)
        if value is not None:
            values.append(value)
    return tuple(values)


def _static_project_scalar(
    raw: str,
    start: int,
    *,
    dialect: _QueryDialect,
) -> str | None:
    index = start
    while index < len(raw) and raw[index].isspace():
        index += 1
    if index >= len(raw):
        return None
    if raw[index] in {"'", '"'}:
        parsed = _decode_query_literal(raw, index, dialect=dialect)
        return parsed[0] if parsed is not None else None
    match = re.match(r"[A-Za-z0-9_-]+", raw[index:])
    return match.group(0) if match is not None else None


def _matching_query_parenthesis(
    raw: str,
    opening: int,
    *,
    dialect: _QueryDialect,
) -> int | None:
    depth = 1
    quote: str | None = None
    index = opening + 1
    while index < len(raw):
        char = raw[index]
        if quote is not None:
            if dialect.backslash_escapes_quotes and char == "\\":
                index += 2
                continue
            if char == quote:
                if (
                    dialect.doubled_quotes_escape
                    and index + 1 < len(raw)
                    and raw[index + 1] == quote
                ):
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
            if depth == 0:
                return index
        index += 1
    return None


def _split_query_values(raw: str, *, dialect: _QueryDialect) -> tuple[str, ...]:
    values: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is not None:
            if dialect.backslash_escapes_quotes and char == "\\":
                index += 2
                continue
            if char == quote:
                if (
                    dialect.doubled_quotes_escape
                    and index + 1 < len(raw)
                    and raw[index + 1] == quote
                ):
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
        elif char == "," and depth == 0:
            values.append(raw[start:index])
            start = index + 1
        index += 1
    values.append(raw[start:])
    return tuple(values)


def _decode_static_project_value(raw: str, *, dialect: _QueryDialect) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if value[0] in {"'", '"'}:
        parsed = _decode_query_literal(value, 0, dialect=dialect)
        if parsed is None or parsed[1] != len(value):
            return None
        return parsed[0]
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else None


def _decode_query_literal(
    raw: str,
    opening: int,
    *,
    dialect: _QueryDialect,
) -> tuple[str, int] | None:
    quote = raw[opening]
    decoded: list[str] = []
    index = opening + 1
    while index < len(raw):
        char = raw[index]
        if dialect.backslash_escapes_quotes and char == "\\":
            if index + 1 >= len(raw):
                return None
            decoded.append(raw[index + 1])
            index += 2
            continue
        if char == quote:
            if dialect.doubled_quotes_escape and index + 1 < len(raw) and raw[index + 1] == quote:
                decoded.append(quote)
                index += 2
                continue
            return "".join(decoded), index + 1
        decoded.append(char)
        index += 1
    return None


def _reject_conflicting_projects(
    requested_projects: tuple[str, ...], allowed: tuple[str, ...]
) -> None:
    allowed_set = set(allowed)
    requested = {project for project in requested_projects if project not in allowed_set}
    if requested:
        raise HTTPException(
            status_code=403,
            detail="work query references a project outside scope",
        )


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
    try:
        _require_project_bound(binding)
        external_project = _external_query_project(binding)
        context = QueryContext(
            project_id=selected_project,
            hints={"project": external_project} if external_project is not None else {},
        )
        boundary = adapter_errors()
        with boundary:
            translated = await binding.adapter.translate_query(body.text, context=context)
            return validated_provider_query(
                translated,
                expected_provider=binding.provider,
            )
        boundary.raise_if_error()
    finally:
        await _close_work_tracking_adapter(binding.adapter)


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
    try:
        _require_project_bound(binding)
        _require_matching_provider(binding, body.query)
        external_project = _external_query_project(binding)
        allowed_projects = (external_project,) if external_project is not None else None
        scoped_query = _constrain_translated_query(body.query, allowed_projects)
        boundary = adapter_errors()
        with boundary:
            return validated_provider_work_item_page(
                await binding.adapter.execute_query(scoped_query, page=page),
                requested_page=page,
            )
        boundary.raise_if_error()
    finally:
        await _close_work_tracking_adapter(binding.adapter)


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
    boundary = adapter_errors()
    with boundary:
        return validated_provider_work_item_page(
            await binding.adapter.list_items(filters, page=page),
            requested_page=page,
        )
    boundary.raise_if_error()


@router.get("/items/{key}", operation_id="getWorkItem", response_model=WorkItem)
async def get_work_item(key: Annotated[ResourceId, Path()], binding: AdapterDep) -> Any:
    boundary = adapter_errors()
    with boundary:
        return validated_provider_work_item(await binding.adapter.get_item(key))
    boundary.raise_if_error()


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
    binding = selection.prebuilt
    try:
        boundary = adapter_errors()
        with boundary:
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
        boundary.raise_if_error()
    finally:
        if binding is not None:
            await _close_work_tracking_adapter(binding.adapter)


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
    binding = selection.prebuilt
    try:
        boundary = adapter_errors()
        with boundary:
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
        boundary.raise_if_error()
    finally:
        if binding is not None:
            await _close_work_tracking_adapter(binding.adapter)


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
    write_error: HTTPException | None = None
    created: Any = None
    try:
        created = await repository.add(row)
    except SavedQueryNameConflictError:
        write_error = HTTPException(status_code=409, detail="saved query name already exists")
    except ValueError:
        write_error = HTTPException(status_code=422, detail="invalid saved query")
    if write_error is not None:
        raise write_error
    assert created is not None
    return created


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
    write_error: HTTPException | None = None
    updated: Any = None
    try:
        updated = await repository.update(row, changes)
    except SavedQueryNameConflictError:
        write_error = HTTPException(status_code=409, detail="saved query name already exists")
    except ValueError:
        write_error = HTTPException(status_code=422, detail="invalid saved query update")
    if write_error is not None:
        raise write_error
    assert updated is not None
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
