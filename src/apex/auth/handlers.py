"""LangGraph Server custom auth (wired via langgraph.json -> auth.path -> `auth`).

Decorated handlers are thin wiring; authorization logic lives in pure helpers
(`scope_filter`, `ensure_thread_scope`, ...) so it stays unit-testable without
the server runtime.
"""

import asyncio
import math
from collections.abc import Mapping, Sequence
from typing import Any, Never, cast

from langgraph_sdk import Auth
from langgraph_sdk.auth import is_studio_user
from pydantic import ValidationError

from apex.app.security import mark_stream_request_authenticated
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import AuthStoreUnavailableError, extract_api_key, get_default_resolver
from apex.domain.diagnostics import bounded_diagnostic, contains_credential_material
from apex.domain.input_limits import validate_json_object, validation_error_summary
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.services.audit import append_audit_event_best_effort, event_from_identity
from apex.services.langgraph_client import (
    LAUNCH_ROOT_FINGERPRINT_METADATA_KEY,
    RERUN_CLAIM_METADATA_KEY,
    RERUN_FINGERPRINT_METADATA_KEY,
    TRUSTED_LOOPBACK_CLAIM,
    is_trusted_loopback,
)
from apex.services.run_validation import (
    validate_context_run_input,
    validate_gate_payload,
    validate_playground_run_input,
    validate_public_run_input,
)
from apex.settings import get_settings

auth = Auth()

# `langgraph dev` Studio requests bypass key auth with a StudioUser; treat as local admin.
_STUDIO_IDENTITY = ConsumerIdentity(
    consumer_id="studio",
    name="studio",
    consumer_type=ConsumerType.INTERNAL,
    role=Role.ADMIN,
)

_PENDING_AUTH_AUDIT: set[asyncio.Task[None]] = set()
_MAX_PENDING_AUTH_AUDIT = 1024
_PROJECT_NAMESPACE_PREFIX = ("apex", "project")
_MAX_LANGGRAPH_JSON_BYTES = 5_000_000
_MAX_LANGGRAPH_JSON_NODES = 20_000
_MAX_LANGGRAPH_READ_PAGE_SIZE = 100
_MAX_LANGGRAPH_READ_OFFSET = 10_000
_MAX_LANGGRAPH_IDENTIFIER_CHARS = 255
_THREAD_SEARCH_SUMMARY_SELECT = [
    "thread_id",
    "created_at",
    "updated_at",
    "status",
]
_MAX_STORE_NAMESPACE_LABELS = 32
_MAX_STORE_NAMESPACE_LABEL_CHARS = 255
_MAX_STORE_KEY_CHARS = 255
_MAX_STORE_QUERY_CHARS = 20_000
_MAX_STORE_FILTER_BYTES = 100_000
_MAX_STORE_FILTER_NODES = 2_000
_MAX_STORE_INDEX_PATHS = 32
_MAX_STORE_MAX_DEPTH = 32
_MAX_STORE_TTL_MINUTES = 60 * 24 * 365 * 10

# These fields are inserted by LangGraph after its public request validator has
# stripped caller-supplied reserved keys and before the Runs.create auth hook is
# invoked. Keep the list explicit: treating every unknown configurable as
# runtime-owned would also admit caller-controlled checkpoint/replay controls.
_LANGGRAPH_RUN_RUNTIME_KEYS = frozenset(
    {
        "langgraph_auth_user",
        "langgraph_auth_user_id",
        "langgraph_auth_permissions",
        "langgraph_request_id",
        "__langsmith_project__",
        "__langsmith_example_id__",
        "__request_start_time_ms__",
        "__after_seconds__",
        "__otel_traceparent__",
        "__otel_tracestate__",
        "__dd_trace_headers__",
        "__pregel_node_finished",
    }
)
# LangGraph always forwards these tracing headers/baggage values when a
# langsmith-trace header is present. Unlike the reserved fields above, they are
# caller-controlled, so they receive their own strict shape and size checks.
_LANGGRAPH_TRACE_CONFIG_KEYS = frozenset(
    {
        "langsmith-trace",
        "langsmith-metadata",
        "langsmith-tags",
        "langsmith-project",
    }
)
_LANGGRAPH_CREDENTIAL_SCAN_EXEMPT_KEYS = frozenset(
    {
        # These values are injected from authenticated/server objects rather
        # than caller tracing headers. Their field names intentionally contain
        # ``auth`` and would otherwise trip the generic credential classifier.
        "langgraph_auth_user",
        "langgraph_auth_user_id",
        "langgraph_auth_permissions",
        "__request_start_time_ms__",
        "__after_seconds__",
        "__pregel_node_finished",
    }
)
_MAX_RUNTIME_TEXT_CHARS = 4_096
_MAX_RUNTIME_PERMISSIONS = 128
_MAX_RUNTIME_TRACE_HEADERS = 32
_LAUNCH_IDEMPOTENCY_METADATA_KEYS = frozenset(
    {
        "launch_idempotency_key",
        "launch_idempotency_fingerprint",
        "launch_principal_id",
    }
)
_SERVER_OWNED_LAUNCH_METADATA_KEYS = _LAUNCH_IDEMPOTENCY_METADATA_KEYS | {
    LAUNCH_ROOT_FINGERPRINT_METADATA_KEY,
    RERUN_CLAIM_METADATA_KEY,
    RERUN_FINGERPRINT_METADATA_KEY,
}
_DIRECT_RUNS_ALLOWED_METADATA_KEY = "apex_direct_runs_allowed"
_IMMUTABLE_THREAD_METADATA_KEYS = frozenset(
    {
        "project_id",
        "app_id",
        "created_by",
        "graph_id",
        "assistant_id",
        _DIRECT_RUNS_ALLOWED_METADATA_KEY,
        *_SERVER_OWNED_LAUNCH_METADATA_KEYS,
    }
)


def user_payload(identity: ConsumerIdentity, *, trusted_loopback: bool = False) -> dict[str, Any]:
    """Authenticate return shape; surfaces in `configurable.langgraph_auth_user`."""
    return {
        "identity": identity.consumer_id,
        "display_name": identity.name,
        "name": identity.name,
        "role": identity.role.value,
        "consumer_type": identity.consumer_type.value,
        "scopes": [scope.model_dump(mode="json") for scope in identity.scopes],
        TRUSTED_LOOPBACK_CLAIM: trusted_loopback,
    }


def _is_trusted_loopback_user(user: Any) -> bool:
    getter = getattr(user, "get", None)
    if callable(getter):
        return getter(TRUSTED_LOOPBACK_CLAIM, False) is True
    return getattr(user, TRUSTED_LOOPBACK_CLAIM, False) is True


def identity_from_user(user: Any) -> ConsumerIdentity:
    """Rebuild a ConsumerIdentity from the server's normalized user object.

    The runtime wraps our authenticate dict in a proxy exposing both `.get` and
    attribute access; support either so helpers also work with plain dicts/objects.
    """
    if is_studio_user(user):
        # `langgraph dev` Studio bypasses key auth; only honor it (as local admin) in
        # dev/test. In a locked-down environment a Studio request must never be admin.
        if get_settings().is_locked_down:
            raise Auth.exceptions.HTTPException(
                status_code=401, detail="Studio access is not permitted in this environment"
            )
        return _STUDIO_IDENTITY

    def field(key: str) -> Any:
        getter = getattr(user, "get", None)
        if callable(getter):
            value = getter(key)
            if value is not None:
                return value
        return getattr(user, key, None)

    identity = field("identity")
    role = field("role")
    consumer_type = field("consumer_type")
    if not identity or not role or not consumer_type:
        raise Auth.exceptions.HTTPException(status_code=401, detail="Malformed auth identity")

    scopes = field("scopes") or []
    parsed_identity: ConsumerIdentity | None = None
    try:
        parsed_identity = ConsumerIdentity(
            consumer_id=str(identity),
            name=str(field("name") or field("display_name") or identity),
            consumer_type=ConsumerType(consumer_type),
            role=Role(role),
            scopes=[ScopeRef.model_validate(dict(scope)) for scope in scopes],
        )
    except Exception:
        pass
    if parsed_identity is None:
        raise Auth.exceptions.HTTPException(status_code=401, detail="Malformed auth identity")
    return parsed_identity


def _authz_action(ctx: Auth.types.AuthContext) -> str:
    return f"{ctx.resource}.{ctx.action}"


def _deny_authz(identity: ConsumerIdentity, *, action: str, detail: str) -> None:
    _schedule_authz_denial(identity, action=action, reason=detail)
    raise Auth.exceptions.HTTPException(status_code=403, detail=detail)


def _schedule_authz_denial(identity: ConsumerIdentity, *, action: str, reason: str) -> None:
    _schedule_auth_decision(
        identity,
        action=action,
        decision="denied",
        status_code=403,
        reason=reason,
    )


def _schedule_auth_decision(
    identity: ConsumerIdentity | None,
    *,
    action: str,
    decision: str,
    status_code: int,
    reason: str,
) -> None:
    try:
        if len(_PENDING_AUTH_AUDIT) >= _MAX_PENDING_AUTH_AUDIT:
            return
        task = asyncio.get_running_loop().create_task(
            append_audit_event_best_effort(
                event_from_identity(
                    identity=identity,
                    category="authz_decision",
                    action=action,
                    decision=decision,
                    reason=reason,
                    resource_type="langgraph",
                    status_code=status_code,
                )
            )
        )
        _PENDING_AUTH_AUDIT.add(task)
        task.add_done_callback(_PENDING_AUTH_AUDIT.discard)
    except RuntimeError:
        return


async def _ensure_catalog_app_scope(identity: ConsumerIdentity, value: Mapping[str, Any]) -> None:
    """Bind explicit LangGraph app IDs to the authoritative catalog project."""
    config = value.get("config") if isinstance(value.get("config"), Mapping) else {}
    kwargs_value = value.get("kwargs")
    kwargs: Mapping[str, Any] = kwargs_value if isinstance(kwargs_value, Mapping) else {}
    nested_config = kwargs.get("config") if isinstance(kwargs, Mapping) else {}
    if not isinstance(nested_config, Mapping):
        nested_config = {}
    configurable = config.get("configurable") if isinstance(config, Mapping) else {}
    nested_configurable = (
        nested_config.get("configurable") if isinstance(nested_config, Mapping) else {}
    )
    if not isinstance(configurable, Mapping):
        configurable = {}
    if not isinstance(nested_configurable, Mapping):
        nested_configurable = {}
    metadata = value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    input_value = value.get("input")
    input_payload: Mapping[str, Any] = input_value if isinstance(input_value, Mapping) else {}
    nested_input_value = kwargs.get("input")
    nested_input: Mapping[str, Any] = (
        nested_input_value if isinstance(nested_input_value, Mapping) else {}
    )
    app_id = (
        input_payload.get("app_id")
        or nested_input.get("app_id")
        or configurable.get("app_id")
        or nested_configurable.get("app_id")
        or metadata.get("app_id")
    )
    project_id = (
        input_payload.get("project_id")
        or nested_input.get("project_id")
        or configurable.get("project_id")
        or nested_configurable.get("project_id")
        or metadata.get("project_id")
    )
    if not app_id or not project_id:
        return
    app = await _load_catalog_application(str(app_id))
    if app is None or app.archived_at is not None or app.project_id != project_id:
        _deny_authz(
            identity,
            action="threads.scope",
            detail="app_id is not authorized for project_id",
        )


async def _load_catalog_application(app_id: str) -> Any:
    from apex.persistence.db import get_sessionmaker
    from apex.persistence.repositories.catalog import CatalogRepository

    async with get_sessionmaker()() as session:
        return await CatalogRepository(session).get_application(app_id)


def ensure_role(
    identity: ConsumerIdentity, minimum: Role, *, action: str = "langgraph.authz"
) -> None:
    if not identity.role.at_least(minimum):
        _deny_authz(
            identity,
            action=action,
            detail=f"Requires role '{minimum.value}' or higher",
        )


def ensure_thread_scope(
    identity: ConsumerIdentity, metadata: dict[str, Any], *, action: str = "threads.scope"
) -> None:
    """Stamp/verify LangGraph project/app metadata against consumer scopes (mutates).

    Unscoped admins pass untouched. Scoped consumers must be pinned to an allowed
    project. App-narrowed scopes are also pinned to `app_id`; project-wide scopes
    may omit it.
    """
    project_id = _project_id(metadata.get("project_id"))
    app_id = _project_id(metadata.get("app_id"))
    if identity.is_unscoped:
        # Platform admins may select any scope, but values still cross a durable
        # identifier boundary and must never be coerced from arbitrary JSON.
        return
    projects = identity.scoped_project_ids()
    if project_id is None:
        if len(projects) == 1:
            project_id = projects[0]
            metadata["project_id"] = project_id
        else:
            _deny_authz(
                identity,
                action=action,
                detail="project_id metadata is required for scoped consumers",
            )
    else:
        metadata["project_id"] = project_id
    if project_id is None:
        raise AssertionError("project_id must be resolved for scoped LangGraph metadata")
    if project_id not in projects:
        _deny_authz(
            identity,
            action=action,
            detail=f"Project '{project_id}' is outside this consumer's scopes",
        )

    if app_id is not None:
        metadata["app_id"] = app_id
        if not _allows_langgraph_scope(identity, project_id=project_id, app_id=app_id):
            _deny_authz(
                identity,
                action=action,
                detail=(
                    f"App '{app_id}' in project '{project_id}' is outside this consumer's scopes"
                ),
            )
        return

    metadata.pop("app_id", None)
    if _has_project_wide_scope(identity, project_id):
        return
    app_ids = _app_ids_for_project(identity, project_id)
    if len(app_ids) == 1:
        metadata["app_id"] = app_ids[0]
        return
    _deny_authz(
        identity,
        action=action,
        detail="app_id metadata is required for app-scoped consumers",
    )


def _has_project_wide_scope(identity: ConsumerIdentity, project_id: str) -> bool:
    return any(scope.project_id == project_id and scope.app_id is None for scope in identity.scopes)


def _app_ids_for_project(identity: ConsumerIdentity, project_id: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for scope in identity.scopes:
        if scope.project_id == project_id and scope.app_id is not None:
            seen.setdefault(scope.app_id)
    return tuple(seen)


def _allows_langgraph_scope(
    identity: ConsumerIdentity, *, project_id: str, app_id: str | None
) -> bool:
    if identity.is_unscoped:
        return True
    if app_id is None:
        return _has_project_wide_scope(identity, project_id)
    return any(
        scope.project_id == project_id and (scope.app_id is None or scope.app_id == app_id)
        for scope in identity.scopes
    )


def _require_langgraph_scope(
    identity: ConsumerIdentity,
    *,
    project_id: str,
    app_id: str | None,
    action: str,
) -> str | None:
    if app_id is not None:
        if not _allows_langgraph_scope(identity, project_id=project_id, app_id=app_id):
            _deny_authz(
                identity,
                action=action,
                detail=(
                    f"App '{app_id}' in project '{project_id}' is outside this consumer's scopes"
                ),
            )
        return app_id
    if _has_project_wide_scope(identity, project_id):
        return None
    app_ids = _app_ids_for_project(identity, project_id)
    if len(app_ids) == 1:
        return app_ids[0]
    _deny_authz(
        identity,
        action=action,
        detail="app_id is required for app-scoped consumers",
    )


def _stamp_run_scope(
    payload: dict[str, Any],
    run_args: dict[str, Any],
    input_payload: dict[str, Any],
    config_payload: dict[str, Any],
    configurable: dict[str, Any],
    metadata: dict[str, Any],
    config_metadata: dict[str, Any],
    *,
    project_id: str,
    app_id: str | None,
) -> None:
    input_payload["project_id"] = project_id
    configurable["project_id"] = project_id
    metadata["project_id"] = project_id
    config_metadata["project_id"] = project_id
    if app_id is not None:
        input_payload["app_id"] = app_id
        configurable["app_id"] = app_id
        metadata["app_id"] = app_id
        config_metadata["app_id"] = app_id
    else:
        input_payload.pop("app_id", None)
        configurable.pop("app_id", None)
        metadata.pop("app_id", None)
        config_metadata.pop("app_id", None)
    run_args["input"] = input_payload
    config_payload["configurable"] = configurable
    config_payload["metadata"] = config_metadata
    run_args["config"] = config_payload
    payload["metadata"] = metadata


def _metadata_filter(project_id: str, app_id: str | None = None) -> dict[str, Any]:
    condition: dict[str, Any] = {"project_id": {"$eq": project_id}}
    if app_id is not None:
        condition["app_id"] = {"$eq": app_id}
    return condition


def _scope_filters(identity: ConsumerIdentity) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    project_wide: set[str] = set()
    seen_apps: set[tuple[str, str]] = set()
    for scope in identity.scopes:
        if scope.app_id is None and scope.project_id not in project_wide:
            project_wide.add(scope.project_id)
            filters.append(_metadata_filter(scope.project_id))
    for scope in identity.scopes:
        if scope.app_id is None or scope.project_id in project_wide:
            continue
        key = (scope.project_id, scope.app_id)
        if key not in seen_apps:
            seen_apps.add(key)
            filters.append(_metadata_filter(scope.project_id, scope.app_id))
    return filters


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _reject_invalid_persisted_input(
    identity: ConsumerIdentity,
    *,
    action: str,
    detail: str,
) -> Never:
    _schedule_auth_decision(
        identity,
        action=action,
        decision="denied",
        status_code=422,
        reason="persisted request input rejected",
    )
    raise Auth.exceptions.HTTPException(
        status_code=422,
        detail=f"Invalid persisted input: {detail}",
    )


def _reject_invalid_request_input(
    identity: ConsumerIdentity,
    *,
    action: str,
    detail: str,
) -> Never:
    _schedule_auth_decision(
        identity,
        action=action,
        decision="denied",
        status_code=422,
        reason="request resource or shape limits rejected the request",
    )
    raise Auth.exceptions.HTTPException(
        status_code=422,
        detail=f"Invalid request input: {detail}",
    )


def _validate_bounded_page(
    identity: ConsumerIdentity,
    payload: Mapping[str, Any],
    *,
    action: str,
    default_limit: int,
    max_limit: int = _MAX_LANGGRAPH_READ_PAGE_SIZE,
    allow_count_sentinel: bool = False,
) -> None:
    limit = payload.get("limit", default_limit)
    offset = payload.get("offset", 0)
    count_sentinel = allow_count_sentinel and limit == 0 and offset == 0
    if not count_sentinel and (type(limit) is not int or not 1 <= limit <= max_limit):
        _reject_invalid_request_input(
            identity,
            action=action,
            detail=f"limit must be an integer between 1 and {max_limit}",
        )
    if type(offset) is not int or not 0 <= offset <= _MAX_LANGGRAPH_READ_OFFSET:
        _reject_invalid_request_input(
            identity,
            action=action,
            detail=f"offset must be an integer between 0 and {_MAX_LANGGRAPH_READ_OFFSET}",
        )


def _validate_store_namespace_field(
    identity: ConsumerIdentity,
    payload: Mapping[str, Any],
    field: str,
    *,
    action: str,
    label: str,
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if isinstance(value, str | bytes) or not isinstance(value, (list, tuple)):
        _reject_invalid_request_input(
            identity,
            action=action,
            detail=f"{label} must be a string sequence",
        )
    if len(value) > _MAX_STORE_NAMESPACE_LABELS:
        _reject_invalid_request_input(
            identity,
            action=action,
            detail=f"{label} must contain at most {_MAX_STORE_NAMESPACE_LABELS} labels",
        )
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > _MAX_STORE_NAMESPACE_LABEL_CHARS
            or "." in item
            or "\x00" in item
        ):
            _reject_invalid_request_input(
                identity,
                action=action,
                detail=(
                    f"{label} labels must be 1-{_MAX_STORE_NAMESPACE_LABEL_CHARS} "
                    "characters without periods or U+0000"
                ),
            )


def _validate_store_key(
    identity: ConsumerIdentity,
    payload: Mapping[str, Any],
    *,
    action: str,
) -> None:
    key = payload.get("key")
    if key is None:
        return
    if not isinstance(key, str) or not key or len(key) > _MAX_STORE_KEY_CHARS or "\x00" in key:
        _reject_invalid_request_input(
            identity,
            action=action,
            detail=f"store key must be 1-{_MAX_STORE_KEY_CHARS} characters without U+0000",
        )


def _validate_store_index(
    identity: ConsumerIdentity,
    payload: Mapping[str, Any],
    *,
    action: str,
) -> None:
    value = payload.get("index")
    if value is None or value is False:
        return
    if isinstance(value, str | bytes) or not isinstance(value, (list, tuple)):
        _reject_invalid_request_input(
            identity,
            action=action,
            detail="store index must be false or a string sequence",
        )
    if len(value) > _MAX_STORE_INDEX_PATHS or any(
        not isinstance(item, str)
        or not item
        or len(item) > _MAX_STORE_NAMESPACE_LABEL_CHARS
        or "\x00" in item
        for item in value
    ):
        _reject_invalid_request_input(
            identity,
            action=action,
            detail=(
                f"store index must contain at most {_MAX_STORE_INDEX_PATHS} non-empty "
                f"paths of at most {_MAX_STORE_NAMESPACE_LABEL_CHARS} characters without U+0000"
            ),
        )


def _validate_store_ttl(
    identity: ConsumerIdentity,
    payload: Mapping[str, Any],
    *,
    action: str,
) -> None:
    ttl = payload.get("ttl")
    if ttl is None:
        return
    if (
        isinstance(ttl, bool)
        or not isinstance(ttl, int | float)
        or not math.isfinite(ttl)
        or not 0 < ttl <= _MAX_STORE_TTL_MINUTES
    ):
        _reject_invalid_request_input(
            identity,
            action=action,
            detail=(
                "store ttl must be a finite positive number no greater than "
                f"{_MAX_STORE_TTL_MINUTES} minutes"
            ),
        )


def _validate_store_read_request(
    identity: ConsumerIdentity,
    payload: Mapping[str, Any],
    *,
    action: str,
    store_action: str,
) -> None:
    _validate_store_namespace_field(
        identity,
        payload,
        "namespace",
        action=action,
        label="store namespace",
    )
    if store_action == "get":
        _validate_store_key(identity, payload, action=action)
        return
    if store_action == "search":
        _validate_bounded_page(identity, payload, action=action, default_limit=10)
        _validate_persisted_json_field(
            identity,
            payload,
            "filter",
            action=action,
            label="store search filter",
            max_bytes=_MAX_STORE_FILTER_BYTES,
            max_nodes=_MAX_STORE_FILTER_NODES,
        )
        query = payload.get("query")
        if query is not None and (
            not isinstance(query, str) or len(query) > _MAX_STORE_QUERY_CHARS or "\x00" in query
        ):
            _reject_invalid_request_input(
                identity,
                action=action,
                detail=(
                    "store search query must be a string of at most "
                    f"{_MAX_STORE_QUERY_CHARS} characters without U+0000"
                ),
            )
        return
    if store_action in {"list", "list_namespaces"}:
        _validate_store_namespace_field(
            identity,
            payload,
            "suffix",
            action=action,
            label="store namespace suffix",
        )
        _validate_bounded_page(identity, payload, action=action, default_limit=100)
        max_depth = payload.get("max_depth")
        if max_depth is not None and (
            type(max_depth) is not int or not 1 <= max_depth <= _MAX_STORE_MAX_DEPTH
        ):
            _reject_invalid_request_input(
                identity,
                action=action,
                detail=(f"store max_depth must be an integer between 1 and {_MAX_STORE_MAX_DEPTH}"),
            )


def _validate_persisted_json_field(
    identity: ConsumerIdentity,
    container: Mapping[str, Any],
    field: str,
    *,
    action: str,
    label: str,
    max_bytes: int = _MAX_LANGGRAPH_JSON_BYTES,
    max_nodes: int = _MAX_LANGGRAPH_JSON_NODES,
) -> None:
    if field not in container or container[field] is None:
        return
    value = container[field]
    if not isinstance(value, Mapping):
        _reject_invalid_persisted_input(
            identity,
            action=action,
            detail=f"{label} must be a JSON object",
        )
    invalid_detail: str | None = None
    try:
        validate_json_object(
            dict(value),
            label=label,
            max_bytes=max_bytes,
            max_nodes=max_nodes,
        )
    except ValueError as exc:
        invalid_detail = bounded_diagnostic(exc, max_chars=1_024)
    if invalid_detail is not None:
        _reject_invalid_persisted_input(
            identity,
            action=action,
            detail=invalid_detail,
        )


def _validate_persisted_text_field(
    identity: ConsumerIdentity,
    container: Mapping[str, Any],
    field: str,
    *,
    action: str,
    label: str,
    max_chars: int | None = None,
) -> None:
    if field not in container or container[field] is None:
        return
    value = container[field]
    if not isinstance(value, str):
        _reject_invalid_persisted_input(
            identity,
            action=action,
            detail=f"{label} must be a string",
        )
    if "\x00" in value:
        _reject_invalid_persisted_input(
            identity,
            action=action,
            detail=f"{label} must not contain U+0000",
        )
    if max_chars is not None and len(value) > max_chars:
        _reject_invalid_persisted_input(
            identity,
            action=action,
            detail=f"{label} must contain at most {max_chars} characters",
        )


def _reject_persisted_credential_material(
    identity: ConsumerIdentity,
    value: Any,
    *,
    action: str,
    label: str,
) -> None:
    """Reject credential-shaped data before a native durable JSON write."""

    if contains_credential_material(value):
        _reject_invalid_persisted_input(
            identity,
            action=action,
            detail=f"{label} must not contain credential material",
        )


def _validate_persisted_text_sequence_field(
    identity: ConsumerIdentity,
    container: Mapping[str, Any],
    field: str,
    *,
    action: str,
    label: str,
) -> None:
    if field not in container or container[field] is None:
        return
    value = container[field]
    if isinstance(value, str | bytes) or not isinstance(value, (list, tuple)):
        _reject_invalid_persisted_input(
            identity,
            action=action,
            detail=f"{label} must be a string sequence",
        )
    if any(not isinstance(item, str) or "\x00" in item for item in value):
        _reject_invalid_persisted_input(
            identity,
            action=action,
            detail=f"{label} entries must be strings without U+0000",
        )


def _validate_run_persisted_inputs(
    identity: ConsumerIdentity,
    value: Auth.types.RunsCreate,
    *,
    action: str,
    trusted_loopback: bool = False,
) -> None:
    """Reject PostgreSQL-incompatible JSON before scope/catalog database reads."""

    payload = _mapping(value)
    run_args = _run_arguments(payload)
    _validate_persisted_json_field(
        identity,
        payload,
        "metadata",
        action=action,
        label="run metadata",
    )
    if payload.get("metadata") is not None:
        _reject_persisted_credential_material(
            identity,
            payload["metadata"],
            action=action,
            label="run metadata",
        )
    _validate_persisted_json_field(
        identity,
        run_args,
        "input",
        action=action,
        label="run input",
    )
    if run_args.get("input") is not None:
        _reject_persisted_credential_material(
            identity,
            run_args["input"],
            action=action,
            label="run input",
        )
    if "config" in run_args and run_args["config"] is not None:
        config = run_args["config"]
        if not isinstance(config, Mapping):
            _reject_invalid_persisted_input(
                identity,
                action=action,
                detail="run config must be a JSON object",
            )
        config_for_validation = dict(config)
        configurable = config_for_validation.get("configurable")
        if isinstance(configurable, Mapping):
            configurable_for_validation = dict(configurable)
            runtime_user = configurable_for_validation.get("langgraph_auth_user")
            if runtime_user is not None:
                # LangGraph's ProxyUser implements mapping-style methods without
                # registering as a Mapping, so the generic JSON walker cannot inspect
                # it. This value is injected from our own authenticated user response;
                # use that bounded JSON representation in the validation copy, then
                # verify the live object against ``identity`` in ensure_run_controls.
                configurable_for_validation["langgraph_auth_user"] = user_payload(identity)
            if "__pregel_node_finished" in configurable_for_validation:
                # This server-owned callback is never persisted as JSON. Preserve a
                # scalar placeholder only in the validation copy so the surrounding
                # attacker-controlled config still receives the normal budget walk.
                configurable_for_validation["__pregel_node_finished"] = "runtime-callback"
            config_for_validation["configurable"] = configurable_for_validation
        invalid_detail = None
        try:
            validate_json_object(
                config_for_validation,
                label="run config",
                max_bytes=_MAX_LANGGRAPH_JSON_BYTES,
                max_nodes=_MAX_LANGGRAPH_JSON_NODES,
            )
        except ValueError as exc:
            invalid_detail = bounded_diagnostic(exc, max_chars=1_024)
        if invalid_detail is not None:
            _reject_invalid_persisted_input(
                identity,
                action=action,
                detail=invalid_detail,
            )
        configurable_for_scan = _mapping(config_for_validation.get("configurable"))
        for runtime_key in _LANGGRAPH_CREDENTIAL_SCAN_EXEMPT_KEYS:
            marker = object()
            runtime_value = configurable_for_scan.pop(runtime_key, marker)
            if (
                runtime_value is not marker
                and runtime_key not in {"langgraph_auth_user", "__pregel_node_finished"}
                and contains_credential_material(runtime_value)
            ):
                _reject_invalid_persisted_input(
                    identity,
                    action=action,
                    detail="run config must not contain credential material",
                )
        config_for_validation["configurable"] = configurable_for_scan
        _reject_persisted_credential_material(
            identity,
            config_for_validation,
            action=action,
            label="run config",
        )
    # Commands, webhooks, checkpoint selectors, and other run controls live
    # alongside input/config in kwargs. Scan them as well; only the verified
    # runtime auth/callback fields removed above are exempt.
    run_controls = {
        key: nested for key, nested in run_args.items() if key not in {"config", "input"}
    }
    top_level_controls = {
        key: nested
        for key, nested in payload.items()
        if key not in {"config", "input", "kwargs", "metadata"}
    }
    if contains_credential_material(
        {"run_controls": run_controls, "top_level_controls": top_level_controls}
    ):
        _reject_invalid_persisted_input(
            identity,
            action=action,
            detail="run controls must not contain credential material",
        )


def _reject_untrusted_launch_metadata(
    identity: ConsumerIdentity,
    metadata: Mapping[str, Any],
    *,
    action: str,
    trusted_loopback: bool,
) -> None:
    forged = _SERVER_OWNED_LAUNCH_METADATA_KEYS.intersection(metadata)
    if forged and not trusted_loopback:
        _deny_authz(
            identity,
            action=action,
            detail=(
                "Launch idempotency metadata is server-owned and may only be set by "
                f"the validated pipeline facade: {', '.join(sorted(forged))}"
            ),
        )


def _nested_mapping(root: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = root
    for key in keys:
        current = _mapping(current).get(key)
    return _mapping(current)


def _run_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    """Return RunsCreate.kwargs, with a top-level fallback for older SDK tests.

    The server runtime captures kwargs.config before invoking auth, so callers of
    this helper must mutate the returned nested mappings in place.
    """

    if "kwargs" in payload:
        return _mapping(payload.get("kwargs"))
    return payload


def _project_id(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_LANGGRAPH_IDENTIFIER_CHARS
        or "\x00" in value
    ):
        raise Auth.exceptions.HTTPException(
            status_code=422,
            detail=(
                "Invalid scope identifier: expected a non-empty, NUL-free string of at most "
                f"{_MAX_LANGGRAPH_IDENTIFIER_CHARS} characters"
            ),
        )
    return value


def ensure_run_scope(
    identity: ConsumerIdentity,
    value: Auth.types.RunsCreate,
    *,
    action: str = "runs.create",
) -> Auth.types.HandlerResult:
    """Validate project/app-bearing run input/config before the graph can execute.

    Every scoped run, including one targeting an existing thread, receives an
    effective project/app in its input, configurable, and run metadata. The
    returned exact metadata filter simultaneously constrains the target thread,
    preventing a run configured for one tenant from executing on another
    tenant's thread.
    """
    payload = _mapping(value)
    run_args = _run_arguments(payload)
    raw_input = run_args.get("input")
    if raw_input is not None and not isinstance(raw_input, Mapping):
        raise Auth.exceptions.HTTPException(
            status_code=422, detail="Invalid run controls: run input must be a JSON object"
        )
    input_payload = _mapping(run_args.get("input"))
    config_payload = _mapping(run_args.get("config"))
    configurable = _mapping(config_payload.get("configurable"))
    metadata = _mapping(payload.get("metadata"))
    config_metadata = _mapping(config_payload.get("metadata"))

    explicit = [
        _project_id(input_payload.get("project_id")),
        _project_id(configurable.get("project_id")),
        _project_id(metadata.get("project_id")),
        _project_id(config_metadata.get("project_id")),
    ]
    explicit_apps = [
        _project_id(input_payload.get("app_id")),
        _project_id(configurable.get("app_id")),
        _project_id(metadata.get("app_id")),
        _project_id(config_metadata.get("app_id")),
    ]
    explicit_projects = [project for project in explicit if project is not None]
    unique_projects = sorted(set(explicit_projects))
    if len(unique_projects) > 1:
        if identity.is_unscoped:
            _reject_invalid_request_input(
                identity,
                action=action,
                detail="conflicting project_id values are not allowed for runs",
            )
        _deny_authz(
            identity,
            action=action,
            detail="Conflicting project_id values are not allowed for scoped runs",
        )

    effective_project = unique_projects[0] if unique_projects else None
    unique_apps = sorted({app_id for app_id in explicit_apps if app_id is not None})
    if len(unique_apps) > 1:
        if identity.is_unscoped:
            _reject_invalid_request_input(
                identity,
                action=action,
                detail="conflicting app_id values are not allowed for runs",
            )
        _deny_authz(
            identity,
            action=action,
            detail="Conflicting app_id values are not allowed for scoped runs",
        )
    effective_app = unique_apps[0] if unique_apps else None

    if identity.is_unscoped:
        if effective_project is None:
            if effective_app is not None:
                _reject_invalid_request_input(
                    identity,
                    action=action,
                    detail="project_id is required when app_id is provided for runs",
                )
            # A platform operation that deliberately has no tenant identifiers
            # remains global. Once any project is supplied, however, every
            # durable surface and the existing-thread filter must agree.
            return None
        _stamp_run_scope(
            payload,
            run_args,
            input_payload,
            config_payload,
            configurable,
            metadata,
            config_metadata,
            project_id=effective_project,
            app_id=effective_app,
        )
        return cast("Auth.types.FilterType", _metadata_filter(effective_project, effective_app))

    for project_id in explicit_projects:
        if not identity.allows_project(project_id):
            _deny_authz(
                identity,
                action=action,
                detail=f"Project '{project_id}' is outside this consumer's scopes",
            )

    if effective_project is None:
        projects = identity.scoped_project_ids()
        if effective_app is not None and len(projects) != 1:
            _deny_authz(
                identity,
                action=action,
                detail="project_id is required when app_id is provided for scoped runs",
            )
        if len(projects) != 1:
            _deny_authz(
                identity,
                action=action,
                detail="project_id is required for scoped runs",
            )
        effective_project = projects[0]

    effective_app = _require_langgraph_scope(
        identity,
        project_id=effective_project,
        app_id=effective_app,
        action=action,
    )
    _stamp_run_scope(
        payload,
        run_args,
        input_payload,
        config_payload,
        configurable,
        metadata,
        config_metadata,
        project_id=effective_project,
        app_id=effective_app,
    )
    return cast("Auth.types.FilterType", _metadata_filter(effective_project, effective_app))


async def _load_run_environment_target(
    environment_id: str, project_id: str | None, app_id: str | None
) -> tuple[str, int, str]:
    """Resolve through the catalog at the authorization boundary (injectable in tests)."""

    from apex.persistence.db import get_sessionmaker
    from apex.persistence.repositories.catalog import CatalogRepository
    from apex.services.environments import resolve_environment_target

    async with get_sessionmaker()() as session:
        target = await resolve_environment_target(
            CatalogRepository(session),
            environment_id,
            project_id=project_id,
            app_id=app_id,
        )
    return target.base_url, target.version, target.app_id


async def ensure_run_environment(
    identity: ConsumerIdentity,
    value: Auth.types.RunsCreate,
    *,
    action: str = "runs.create",
) -> Auth.types.HandlerResult:
    """Replace caller target data with an authorized, catalog-resolved immutable target."""

    payload = _mapping(value)
    run_args = _run_arguments(payload)
    input_payload = _mapping(run_args.get("input"))
    config_payload = _mapping(run_args.get("config"))
    configurable = _mapping(config_payload.get("configurable"))
    metadata = _mapping(payload.get("metadata"))
    config_metadata = _mapping(config_payload.get("metadata"))
    load_test = _mapping(configurable.get("load_test"))
    if "target_environment" in load_test:
        _deny_authz(
            identity,
            action=action,
            detail=(
                "load_test.target_environment cannot be supplied directly; select environment_id"
            ),
        )
    script_refs = load_test.get("script_refs")
    if isinstance(script_refs, list) and any(
        isinstance(ref, str) and ref.lstrip().startswith("{") for ref in script_refs
    ):
        _deny_authz(
            identity,
            action=action,
            detail="inline load_test.script_refs are not allowed; select an approved environment",
        )

    # This field is server-owned. Always discard a caller value before resolving.
    configurable.pop("environment_target", None)
    configurable.pop("environment_target_version", None)
    environment_id = _project_id(configurable.get("environment_id"))
    if environment_id is not None:
        project_id = _project_id(configurable.get("project_id"))
        app_id = _project_id(configurable.get("app_id"))
        environment_lookup_failed = False
        try:
            target, version, target_app_id = await _load_run_environment_target(
                environment_id, project_id, app_id
            )
            authoritative_app_id = _project_id(target_app_id)
            if project_id is None:
                # The catalog resolver already rejects this case. Keep the
                # stamping boundary explicit so a future resolver cannot turn
                # an environment into an ownership-less run.
                raise LookupError("environment target has no project ownership")
            if authoritative_app_id is None or (
                app_id is not None and app_id != authoritative_app_id
            ):
                raise LookupError("environment target has conflicting application ownership")
            _require_langgraph_scope(
                identity,
                project_id=project_id,
                app_id=authoritative_app_id,
                action=action,
            )
            configurable["environment_target"] = target
            configurable["environment_target_version"] = version
            # Environment ownership is authoritative. A project-wide caller
            # may omit app_id, but persisting that run as project-level would
            # let sibling-app identities read its state and artifacts. Stamp
            # the owning app into every LangGraph scope-bearing surface before
            # either thread authorization or persistence.
            _stamp_run_scope(
                payload,
                run_args,
                input_payload,
                config_payload,
                configurable,
                metadata,
                config_metadata,
                project_id=project_id,
                app_id=authoritative_app_id,
            )
        except LookupError:
            environment_lookup_failed = True
        if environment_lookup_failed:
            raise Auth.exceptions.HTTPException(
                status_code=404,
                detail="Environment target not found",
            )

    config_payload["configurable"] = configurable
    run_args["config"] = config_payload
    if environment_id is None:
        return None
    project_id = _project_id(configurable.get("project_id"))
    target_app_id = _project_id(configurable.get("app_id"))
    assert project_id is not None and target_app_id is not None
    return cast("Auth.types.FilterType", _metadata_filter(project_id, target_app_id))


def _validate_runtime_text(
    value: Any,
    *,
    label: str,
    max_chars: int = _MAX_RUNTIME_TEXT_CHARS,
    allow_none: bool = True,
) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if len(value) > max_chars:
        raise ValueError(f"{label} must contain at most {max_chars} characters")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain U+0000")


def _validate_runtime_text_sequence(
    value: Any,
    *,
    label: str,
    max_items: int,
    max_chars: int = _MAX_LANGGRAPH_IDENTIFIER_CHARS,
) -> None:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a list of strings")
    if len(value) > max_items:
        raise ValueError(f"{label} must contain at most {max_items} entries")
    for item in value:
        _validate_runtime_text(
            item,
            label=f"{label} entries",
            max_chars=max_chars,
            allow_none=False,
        )


def _runtime_auth_user_payload(value: Any) -> dict[str, Any]:
    """Materialize the server-injected auth user so extra fields cannot hide.

    Production receives LangGraph's ``ProxyUser`` while tests use a small
    mapping-style proxy. Both are enumerable; anything opaque fails closed.
    """

    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("model_dump", "dict"):
        inspection_failed = False
        try:
            method = getattr(value, method_name, None)
        except Exception:  # noqa: BLE001 - auth values must be inspectable
            inspection_failed = True
            method = None
        if inspection_failed:
            raise ValueError("langgraph_auth_user is not inspectable")
        if not callable(method):
            continue
        rendering_failed = False
        try:
            try:
                rendered = method(mode="json")
            except TypeError:
                rendered = method()
        except Exception:  # noqa: BLE001 - auth values must be inspectable
            rendering_failed = True
            rendered = None
        if rendering_failed:
            raise ValueError("langgraph_auth_user is not inspectable")
        if isinstance(rendered, Mapping):
            return dict(rendered)
        raise ValueError("langgraph_auth_user must render as an object")
    enumeration_failed = False
    try:
        keys = list(iter(value))
    except Exception:  # noqa: BLE001 - auth values must be enumerable
        enumeration_failed = True
        keys = []
    if enumeration_failed:
        raise ValueError("langgraph_auth_user is not enumerable")
    if len(keys) > 32 or any(not isinstance(key, str) for key in keys):
        raise ValueError("langgraph_auth_user contains invalid fields")
    materialization_failed = False
    try:
        rendered_user = {key: value[key] for key in keys}
    except Exception:  # noqa: BLE001 - auth values must be enumerable
        materialization_failed = True
        rendered_user = {}
    if materialization_failed:
        raise ValueError("langgraph_auth_user is not enumerable")
    return rendered_user


def _validate_runtime_run_configurable(
    identity: ConsumerIdentity,
    runtime: Mapping[str, Any],
    *,
    trusted_loopback: bool,
) -> None:
    """Validate only the metadata LangGraph itself adds around app config.

    Public reserved keys are stripped by LangGraph before these values are
    injected. Header-derived LangSmith values are deliberately validated as
    untrusted input. Application configuration is validated separately through
    ``PipelineConfigurable`` so runtime metadata cannot weaken its extra-forbid
    contract.
    """

    allowed = _LANGGRAPH_RUN_RUNTIME_KEYS | _LANGGRAPH_TRACE_CONFIG_KEYS
    unknown_count = sum(key not in allowed for key in runtime)
    if unknown_count:
        raise ValueError(f"config.configurable contains {unknown_count} unsupported field(s)")

    if "langgraph_auth_user" in runtime:
        runtime_user_payload = _runtime_auth_user_payload(runtime["langgraph_auth_user"])
        expected_user = user_payload(identity, trusted_loopback=trusted_loopback)
        expected_proxy_user = {**expected_user, "is_authenticated": True}
        if runtime_user_payload != expected_user and runtime_user_payload != expected_proxy_user:
            raise ValueError(
                "langgraph_auth_user does not match the authenticated consumer "
                "or contains non-canonical fields"
            )
        malformed_user = False
        try:
            runtime_identity = identity_from_user(runtime["langgraph_auth_user"])
        except Auth.exceptions.HTTPException:
            malformed_user = True
            runtime_identity = None
        if malformed_user:
            raise ValueError("langgraph_auth_user is malformed")
        if runtime_identity != identity:
            raise ValueError("langgraph_auth_user does not match the authenticated consumer")

    if "langgraph_auth_user_id" in runtime:
        auth_user_id = runtime["langgraph_auth_user_id"]
        _validate_runtime_text(
            auth_user_id,
            label="langgraph_auth_user_id",
            max_chars=_MAX_LANGGRAPH_IDENTIFIER_CHARS,
            allow_none=False,
        )
        if auth_user_id != identity.consumer_id:
            raise ValueError("langgraph_auth_user_id does not match the authenticated consumer")

    if "langgraph_auth_permissions" in runtime:
        _validate_runtime_text_sequence(
            runtime["langgraph_auth_permissions"],
            label="langgraph_auth_permissions",
            max_items=_MAX_RUNTIME_PERMISSIONS,
        )

    for key, max_chars in (
        ("langgraph_request_id", _MAX_LANGGRAPH_IDENTIFIER_CHARS),
        ("__langsmith_project__", _MAX_LANGGRAPH_IDENTIFIER_CHARS),
        ("__langsmith_example_id__", _MAX_LANGGRAPH_IDENTIFIER_CHARS),
        ("__otel_traceparent__", 512),
        ("__otel_tracestate__", _MAX_RUNTIME_TEXT_CHARS),
        ("langsmith-trace", _MAX_RUNTIME_TEXT_CHARS),
        ("langsmith-project", _MAX_LANGGRAPH_IDENTIFIER_CHARS),
    ):
        if key in runtime:
            _validate_runtime_text(runtime[key], label=key, max_chars=max_chars)

    if "__request_start_time_ms__" in runtime:
        request_start = runtime["__request_start_time_ms__"]
        if (
            isinstance(request_start, bool)
            or not isinstance(request_start, int | float)
            or not math.isfinite(request_start)
            or request_start < 0
        ):
            raise ValueError("__request_start_time_ms__ must be a finite non-negative number")

    if "__after_seconds__" in runtime:
        after_seconds = runtime["__after_seconds__"]
        if type(after_seconds) is not int:
            raise ValueError("__after_seconds__ must be an integer")
        max_after_seconds = 86_400 if trusted_loopback else 0
        if not 0 <= after_seconds <= max_after_seconds:
            if trusted_loopback:
                raise ValueError("__after_seconds__ must be between 0 and 86400")
            raise ValueError("__after_seconds__ must be 0 for direct run creation")

    if "__dd_trace_headers__" in runtime:
        headers = runtime["__dd_trace_headers__"]
        if not isinstance(headers, Mapping):
            raise ValueError("__dd_trace_headers__ must be an object")
        if len(headers) > _MAX_RUNTIME_TRACE_HEADERS:
            raise ValueError(
                f"__dd_trace_headers__ must contain at most {_MAX_RUNTIME_TRACE_HEADERS} entries"
            )
        normalized_headers: dict[str, str] = {}
        for key, value in headers.items():
            _validate_runtime_text(
                key,
                label="__dd_trace_headers__ names",
                max_chars=128,
                allow_none=False,
            )
            _validate_runtime_text(
                value,
                label="__dd_trace_headers__ values",
                max_chars=_MAX_RUNTIME_TEXT_CHARS,
                allow_none=False,
            )
            normalized_headers[key] = value
        validate_json_object(
            normalized_headers,
            label="__dd_trace_headers__",
            max_bytes=20_000,
            max_nodes=128,
        )

    if "__pregel_node_finished" in runtime and not callable(runtime["__pregel_node_finished"]):
        raise ValueError("__pregel_node_finished must be a runtime callback")

    if "langsmith-metadata" in runtime:
        metadata = runtime["langsmith-metadata"]
        if not isinstance(metadata, dict):
            raise ValueError("langsmith-metadata must be a JSON object")
        validate_json_object(
            metadata,
            label="langsmith-metadata",
            max_bytes=100_000,
            max_nodes=2_000,
        )

    if "langsmith-tags" in runtime:
        _validate_runtime_text_sequence(
            runtime["langsmith-tags"],
            label="langsmith-tags",
            max_items=128,
        )


def ensure_run_controls(
    identity: ConsumerIdentity,
    value: Auth.types.RunsCreate,
    *,
    action: str = "runs.create",
    trusted_loopback: bool = False,
) -> None:
    """Enforce cost and payload budgets for direct LangGraph SDK run creation."""

    payload = _mapping(value)
    run_args = _run_arguments(payload)
    raw_input = run_args.get("input")
    input_payload = _mapping(run_args.get("input"))
    config_payload = _mapping(run_args.get("config"))
    configurable = _mapping(config_payload.get("configurable"))
    invalid_controls = False
    invalid_controls_detail = ""
    try:
        requested_action = payload.get("action") or run_args.get("action")
        multitask_strategy = payload.get("multitask_strategy") or run_args.get("multitask_strategy")
        webhook = payload.get("webhook") if "webhook" in payload else run_args.get("webhook")
        if not trusted_loopback and webhook is not None:
            raise ValueError("run webhooks are disabled by outbound-network policy")
        if not trusted_loopback and requested_action in {"interrupt", "rollback"}:
            raise ValueError(
                "run action cannot interrupt or roll back an existing run; use "
                "/v1/pipelines/{thread_id}/abort"
            )
        if not trusted_loopback and multitask_strategy not in {None, "reject"}:
            raise ValueError(
                "multitask_strategy must be 'reject' for direct run creation; "
                "use the validated /v1 pipeline APIs for lifecycle transitions"
            )
        if not trusted_loopback:
            if (
                "kwargs" in payload
                and "thread_id" in payload
                and payload.get("thread_id") is None
                and run_args.get("temporary") is not True
            ):
                raise ValueError(
                    "stateless direct runs must be temporary; on_completion='keep' is disabled"
                )
            # The runtime accepts arbitrary JSON numbers here and only coerces
            # them while building the backend request.  Disable delayed direct
            # runs so fractional/negative values cannot silently change meaning
            # and oversized integers cannot become backend/protobuf failures.
            control_containers = (payload,) if run_args is payload else (payload, run_args)
            for controls in control_containers:
                if "after_seconds" in controls:
                    after_seconds = controls["after_seconds"]
                    if type(after_seconds) is not int or after_seconds != 0:
                        raise ValueError(
                            "after_seconds must be the integer 0 for direct run creation"
                        )
                if "feedback_keys" in controls:
                    feedback_keys = controls["feedback_keys"]
                    if feedback_keys is not None and (
                        isinstance(feedback_keys, str | bytes)
                        or not isinstance(feedback_keys, (list, tuple))
                        or len(feedback_keys) != 0
                    ):
                        raise ValueError("feedback_keys are disabled for direct run creation")
                if controls.get("checkpoint_during") is False:
                    raise ValueError("checkpoint_during cannot be disabled for direct run creation")
                for interrupt_field in ("interrupt_before", "interrupt_after"):
                    if controls.get(interrupt_field) is not None:
                        raise ValueError(
                            f"{interrupt_field} is server-owned for direct run creation"
                        )
                if "durability" in controls:
                    # Native LangGraph defaults to async durability. APEX must
                    # checkpoint the external-engine abort handle before work can
                    # proceed, so normalize every public run to the same write-ahead
                    # contract used by the validated facade.
                    controls["durability"] = "sync"
                if "checkpoint_during" in controls:
                    controls["checkpoint_during"] = True
            run_args["durability"] = "sync"
            run_args["checkpoint_during"] = True
            load_test = _mapping(configurable.get("load_test"))
            forbidden_selectors = {
                "script_refs",
                "test_id",
                "test_instance_id",
            }.intersection(load_test)
            if forbidden_selectors:
                raise ValueError(
                    "provider workload selectors are connection/catalog-owned and cannot be "
                    f"overridden per run: {', '.join(sorted(forbidden_selectors))}"
                )
        assistant_id = str(payload.get("assistant_id") or run_args.get("assistant_id") or "")
        if assistant_id == "pipeline":
            # Trusted facade calls (notably gate resumes) need the same
            # write-ahead durability guarantee as initial pipeline launches.
            run_args["durability"] = "sync"
            run_args["checkpoint_during"] = True
        if raw_input is not None and not isinstance(raw_input, Mapping):
            raise ValueError("run input must be a JSON object")
        requested_thread_id = _project_id(payload.get("thread_id"))
        configured_thread_id = _project_id(configurable.get("thread_id"))
        if configured_thread_id is not None and configured_thread_id != requested_thread_id:
            raise ValueError(
                "config.configurable.thread_id is server-owned and must match the target thread"
            )
        application_config = {
            key: value
            for key, value in configurable.items()
            if key in PipelineConfigurable.model_fields
        }
        runtime_config = {
            key: value
            for key, value in configurable.items()
            if key not in PipelineConfigurable.model_fields
        }
        _validate_runtime_run_configurable(
            identity,
            runtime_config,
            trusted_loopback=trusted_loopback,
        )
        PipelineConfigurable.model_validate(application_config)
        validate_public_run_input(input_payload)
        context_keys = {"subject", "work_item_keys", "document_packets"}
        playground_keys = {"prompt", "sample_input"}
        if assistant_id == "context" or context_keys.intersection(input_payload):
            validate_context_run_input(input_payload)
        elif assistant_id == "playground" or playground_keys.intersection(input_payload):
            validate_playground_run_input(input_payload)
        command = run_args.get("command")
        if command is not None:
            if not trusted_loopback:
                raise ValueError(
                    "native run commands are server-owned; use the gate resume endpoint"
                )
            validate_gate_payload(command)
    except (ValidationError, ValueError) as exc:
        invalid_controls = True
        _schedule_auth_decision(
            identity,
            action=action,
            decision="denied",
            status_code=422,
            reason="run resource or payload limits rejected the request",
        )
        if isinstance(exc, ValidationError):
            detail = validation_error_summary(exc, max_errors=1, max_chars=1_024)
        else:
            # ValueError messages in this validation pipeline are code-owned,
            # but still apply the shared response cap and credential redaction
            # instead of reflecting an exception verbatim.
            detail = bounded_diagnostic(exc, max_chars=1_024)
        invalid_controls_detail = detail
    if invalid_controls:
        raise Auth.exceptions.HTTPException(
            status_code=422,
            detail=f"Invalid run controls: {invalid_controls_detail}",
        )


def _scoped_store_prefix(project_id: str, app_id: str | None) -> tuple[str, ...]:
    prefix: tuple[str, ...] = (*_PROJECT_NAMESPACE_PREFIX, project_id)
    return (*prefix, "app", app_id) if app_id is not None else prefix


def ensure_store_namespace_scope(
    identity: ConsumerIdentity,
    value: Any,
    *,
    action: str = "store.scope",
) -> None:
    """Project/app-prefix LangGraph store namespaces for scoped consumers.

    The SDK documents store namespace mutation as the intended auth mechanism.
    A consumer with one unambiguous scope may use an unprefixed namespace; the
    handler transparently adds ``apex/project/<project>`` and, for app-only
    grants, ``app/<app>``. Ambiguous identities must select a canonical prefix.
    """

    if identity.is_unscoped:
        return
    projects = identity.scoped_project_ids()
    if not projects:
        _deny_authz(
            identity,
            action=action,
            detail="store namespace requires at least one project scope",
        )
    if not isinstance(value, dict):
        _deny_authz(identity, action=action, detail="store operation payload is malformed")

    raw_namespace = value.get("namespace")
    namespace = tuple(raw_namespace or ())
    if tuple(namespace[:2]) == _PROJECT_NAMESPACE_PREFIX:
        selected_project = _project_id(namespace[2]) if len(namespace) >= 3 else None
        if selected_project is None:
            _deny_authz(
                identity,
                action=action,
                detail="project-prefixed store namespace must include a project_id",
            )
        assert selected_project is not None
        if selected_project not in projects:
            _deny_authz(
                identity,
                action=action,
                detail=(
                    f"Store namespace project '{selected_project}' is outside "
                    "this consumer's scopes"
                ),
            )
        selected_app: str | None = None
        if len(namespace) > 3 and namespace[3] == "app":
            if len(namespace) < 5 or (selected_app := _project_id(namespace[4])) is None:
                _deny_authz(
                    identity,
                    action=action,
                    detail="app-prefixed store namespace must include an app_id",
                )
        if selected_app is None and not _has_project_wide_scope(identity, selected_project):
            _deny_authz(
                identity,
                action=action,
                detail="project-wide store namespace requires project-wide scope",
            )
        if selected_app is not None and not _allows_langgraph_scope(
            identity, project_id=selected_project, app_id=selected_app
        ):
            _deny_authz(
                identity,
                action=action,
                detail=(
                    f"Store namespace app '{selected_app}' in project "
                    f"'{selected_project}' is outside this consumer's scopes"
                ),
            )
        value["namespace"] = namespace
        return

    if len(projects) != 1:
        _deny_authz(
            identity,
            action=action,
            detail="project-prefixed store namespace is required for multi-project consumers",
        )

    selected_project = projects[0]
    selected_app = _require_langgraph_scope(
        identity,
        project_id=selected_project,
        app_id=None,
        action=action,
    )
    value["namespace"] = (*_scoped_store_prefix(selected_project, selected_app), *namespace)


def ensure_metadata_scope(
    identity: ConsumerIdentity,
    value: Any,
    *,
    action: str,
    required: bool = False,
) -> None:
    """Stamp/verify `metadata.project_id` on assistant-like resources."""

    if not isinstance(value, dict):
        _deny_authz(identity, action=action, detail="resource payload is malformed")
    metadata = _mapping(value.get("metadata"))
    # Validate durable ownership selectors even for an unscoped platform admin.
    _project_id(metadata.get("project_id"))
    _project_id(metadata.get("app_id"))
    if identity.is_unscoped:
        return
    if required and not metadata:
        _deny_authz(
            identity,
            action=action,
            detail="project_id metadata is required for scoped consumers",
        )
    ensure_thread_scope(identity, metadata, action=action)
    value["metadata"] = metadata


def ensure_unscoped_admin(identity: ConsumerIdentity, *, action: str, resource: str) -> None:
    if identity.is_unscoped:
        return
    _deny_authz(
        identity,
        action=action,
        detail=f"{resource} requires an unscoped admin until project ownership can be verified",
    )


def scope_filter(identity: ConsumerIdentity) -> Auth.types.FilterType | bool | None:
    """Metadata filter limiting reads/searches to scoped projects/apps.

    None = unscoped (no filter); False = no usable scope (deny).
    """
    if identity.is_unscoped:
        return None
    filters = _scope_filters(identity)
    if not filters:
        return False
    if len(filters) == 1:
        return cast("Auth.types.FilterType", filters[0])
    # $or is supported by the installed runtime (langgraph_runtime_inmem ops) even
    # though the SDK's FilterType alias does not model it yet.
    return cast(
        "Auth.types.FilterType",
        {"$or": filters},
    )


def _require_direct_runs_allowed(
    base_filter: Auth.types.HandlerResult,
) -> Auth.types.HandlerResult:
    """Constrain an untrusted run creation to explicitly public threads.

    Exact-match filtering intentionally fails closed for legacy threads that do
    not carry the server-owned classification marker.
    """
    if base_filter is False:
        return False
    direct_run_filter = {
        _DIRECT_RUNS_ALLOWED_METADATA_KEY: {"$eq": True},
    }
    if base_filter is None:
        return cast("Auth.types.FilterType", direct_run_filter)
    return cast(
        "Auth.types.FilterType",
        {**cast("dict[str, Any]", base_filter), **direct_run_filter},
    )


@auth.authenticate
async def authenticate(
    headers: dict[bytes, bytes],
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store_unavailable = False
    try:
        identity = await get_default_resolver().resolve(extract_api_key(headers))
    except AuthStoreUnavailableError:
        store_unavailable = True
        identity = None
    if store_unavailable:
        # Leave the catch before translating the failure. Otherwise the opaque
        # HTTP exception retains the resolver's driver cause for tracing code.
        raise Auth.exceptions.HTTPException(status_code=503, detail="API key store is unavailable")
    if identity is None:
        detail = "Invalid or missing API key"
        _schedule_auth_decision(
            None,
            action="authenticate",
            decision="unauthenticated",
            status_code=401,
            reason=detail,
        )
        raise Auth.exceptions.HTTPException(status_code=401, detail=detail)
    if scope is not None:
        await mark_stream_request_authenticated(scope)
    return user_payload(identity, trusted_loopback=is_trusted_loopback(headers))


@auth.on.threads.create
async def on_threads_create(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsCreate
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    payload = _mapping(value)
    _validate_persisted_json_field(
        identity,
        payload,
        "metadata",
        action=action,
        label="thread metadata",
    )
    metadata = _mapping(payload.get("metadata"))
    trusted_loopback = _is_trusted_loopback_user(ctx.user)
    if payload.get("metadata") is not None:
        _reject_persisted_credential_material(
            identity,
            metadata,
            action=action,
            label="thread metadata",
        )
    _reject_untrusted_launch_metadata(
        identity,
        metadata,
        action=action,
        trusted_loopback=trusted_loopback,
    )
    # This classification is derived rather than accepted from callers. Facade
    # launch threads must remain replay-stable: a public native run appended to
    # one could otherwise become the newest run adopted by an idempotent retry.
    metadata[_DIRECT_RUNS_ALLOWED_METADATA_KEY] = not bool(
        _LAUNCH_IDEMPOTENCY_METADATA_KEYS.intersection(metadata)
    )
    # Attribution is server-owned; never preserve a caller-supplied identity.
    metadata["created_by"] = identity.consumer_id
    ensure_thread_scope(identity, metadata, action=action)
    value["metadata"] = metadata
    await _ensure_catalog_app_scope(identity, value)
    return scope_filter(identity)


@auth.on.threads.create_run
async def on_threads_create_run(
    ctx: Auth.types.AuthContext, value: Auth.types.RunsCreate
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    payload = _mapping(value)
    metadata = _mapping(payload.get("metadata"))
    trusted_loopback = _is_trusted_loopback_user(ctx.user)
    _validate_run_persisted_inputs(
        identity,
        value,
        action=action,
        trusted_loopback=trusted_loopback,
    )
    _reject_untrusted_launch_metadata(
        identity,
        metadata,
        action=action,
        trusted_loopback=trusted_loopback,
    )
    # Also classify threads created implicitly by Runs.create. Existing thread
    # metadata is not mutated by this run metadata; the returned filter below
    # authorizes public runs only when the thread itself was classified public.
    metadata[_DIRECT_RUNS_ALLOWED_METADATA_KEY] = not bool(
        _LAUNCH_IDEMPOTENCY_METADATA_KEYS.intersection(metadata)
    )
    metadata["created_by"] = identity.consumer_id
    payload["metadata"] = metadata
    # Perform all cheap model/shape/resource checks before any catalog or
    # environment query can receive attacker-sized identifiers.
    ensure_run_controls(
        identity,
        value,
        action=action,
        trusted_loopback=trusted_loopback,
    )
    result = ensure_run_scope(identity, value, action=action)
    await _ensure_catalog_app_scope(identity, value)
    environment_filter = await ensure_run_environment(identity, value, action=action)
    if environment_filter is not None:
        result = environment_filter
    # Environment resolution stamps deployment-owned target fields; validate
    # the final effective configurable before the runtime persists the run.
    ensure_run_controls(
        identity,
        value,
        action=action,
        trusted_loopback=trusted_loopback,
    )
    if trusted_loopback:
        return result
    return _require_direct_runs_allowed(result)


@auth.on.threads.read
async def on_threads_read(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsRead
) -> Auth.types.HandlerResult:
    return scope_filter(identity_from_user(ctx.user))


@auth.on.threads.search
async def on_threads_search(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsSearch
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    payload = _mapping(value)
    if payload.get("values") and not _is_trusted_loopback_user(ctx.user):
        _deny_authz(
            identity,
            action=action,
            detail="Native thread value filters are disabled on the public summary surface",
        )
    _validate_bounded_page(
        identity,
        payload,
        action=action,
        default_limit=10,
        allow_count_sentinel=True,
    )
    for field, label in (
        ("metadata", "thread metadata filter"),
        ("values", "thread values filter"),
    ):
        _validate_persisted_json_field(
            identity,
            payload,
            field,
            action=action,
            label=label,
            max_bytes=_MAX_STORE_FILTER_BYTES,
            max_nodes=_MAX_STORE_FILTER_NODES,
        )
    if payload.get("metadata") and not _is_trusted_loopback_user(ctx.user):
        _deny_authz(
            identity,
            action=action,
            detail="Native thread metadata filters are disabled on the public summary surface",
        )
    # The runtime omits select/extract from its auth value. The outer body
    # middleware therefore enforces the public projection before this handler;
    # this handler owns only fields the runtime actually exposes to auth.
    return scope_filter(identity)


@auth.on.threads.update
async def on_threads_update(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsUpdate
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    payload = _mapping(value)
    trusted_loopback = _is_trusted_loopback_user(ctx.user)
    if payload.get("action") in {"interrupt", "rollback"} and not trusted_loopback:
        _deny_authz(
            identity,
            action=action,
            detail=(
                "Native run cancellation is disabled; use "
                "/v1/pipelines/{thread_id}/abort so external engine cleanup runs first"
            ),
        )
    # The installed LangGraph runtime authorizes both update_state variants
    # (single values, with or without as_node) and bulk state updates as a bare
    # ThreadsUpdate(thread_id=...) -- values/as_node are not exposed to auth.
    # Metadata PATCH always carries the metadata key, while run cancellation
    # carries action + metadata and was handled above. Fail closed on the only
    # remaining bare shape so callers cannot forge phase_results, reviews,
    # handles, or graph cursors around PipelineInput.
    if "metadata" not in payload and payload.get("action") is None and not trusted_loopback:
        _deny_authz(
            identity,
            action=action,
            detail=(
                "Native thread state updates are disabled; use the validated /v1 pipeline APIs"
            ),
        )
    metadata = _mapping(payload.get("metadata"))
    _validate_persisted_json_field(
        identity,
        payload,
        "metadata",
        action=action,
        label="thread metadata",
    )
    if payload.get("metadata") is not None:
        _reject_persisted_credential_material(
            identity,
            metadata,
            action=action,
            label="thread metadata",
        )
    immutable = _IMMUTABLE_THREAD_METADATA_KEYS.intersection(metadata)
    if immutable:
        _deny_authz(
            identity,
            action=action,
            detail=("Server-owned thread metadata is immutable: " + ", ".join(sorted(immutable))),
        )
    return scope_filter(identity)


@auth.on.threads.delete
async def on_threads_delete(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsDelete
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    if _is_trusted_loopback_user(ctx.user):
        return scope_filter(identity)
    _deny_authz(
        identity,
        action=action,
        detail=(
            "Native thread/run deletion is disabled because it can discard the "
            "external engine cleanup handle"
        ),
    )


@auth.on(resources="assistants", actions=["create", "update", "delete"])
async def on_assistants_write(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.ADMIN, action=action)
    # Assistant static config is merged after run authorization. Keep that
    # deployment-wide policy surface platform-admin-only; tenant admins can use
    # ordinary run configuration, which is validated and target-stamped.
    ensure_unscoped_admin(identity, action=action, resource="assistant mutation")
    if ctx.action == "delete":
        return
    payload = _mapping(value)
    for field, label in (("graph_id", "assistant graph_id"), ("name", "assistant name")):
        _validate_persisted_text_field(
            identity,
            payload,
            field,
            action=action,
            label=label,
        )
    for field, label in (
        ("config", "assistant config"),
        ("context", "assistant context"),
        ("metadata", "assistant metadata"),
    ):
        _validate_persisted_json_field(
            identity,
            payload,
            field,
            action=action,
            label=label,
        )
        if payload.get(field) is not None:
            _reject_persisted_credential_material(
                identity,
                payload[field],
                action=action,
                label=label,
            )
    _reject_persisted_credential_material(
        identity,
        payload,
        action=action,
        label="assistant",
    )
    ensure_metadata_scope(identity, value, action=action)
    # On update, constrain the existing resource as well as stamping the new
    # metadata; otherwise a scoped admin could take over a sibling assistant by id.
    return scope_filter(identity)


@auth.on(resources="assistants", actions=["read", "search"])
async def on_assistants_read(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.VIEWER, action=action)
    payload = _mapping(value)
    _validate_bounded_page(
        identity,
        payload,
        action=action,
        default_limit=10,
        max_limit=10,
        allow_count_sentinel=True,
    )
    _validate_persisted_json_field(
        identity,
        payload,
        "metadata",
        action=action,
        label="assistant metadata filter",
        max_bytes=_MAX_STORE_FILTER_BYTES,
        max_nodes=_MAX_STORE_FILTER_NODES,
    )
    if payload.get("metadata") and not _is_trusted_loopback_user(ctx.user):
        _deny_authz(
            identity,
            action=action,
            detail="Native assistant metadata filters are disabled on the public summary surface",
        )
    for field in ("graph_id", "name"):
        _validate_persisted_text_field(
            identity,
            payload,
            field,
            action=action,
            label=f"assistant {field} filter",
            max_chars=_MAX_LANGGRAPH_IDENTIFIER_CHARS,
        )
    # Read/search payloads commonly carry no metadata. In that case the
    # returned server-side filter is sufficient and must support identities
    # spanning multiple projects. If metadata was explicitly supplied, still
    # reject an out-of-scope selector.
    if _mapping(payload.get("metadata")):
        ensure_metadata_scope(identity, value, action=action)
    return scope_filter(identity)


@auth.on(resources="crons", actions=["read", "search"])
async def on_crons_read(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    _deny_authz(
        identity,
        action=action,
        detail="Native scheduled runs are disabled; use an operator-controlled scheduler",
    )


@auth.on(resources="crons", actions=["create", "update", "delete"])
async def on_crons_write(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    _deny_authz(
        identity,
        action=action,
        detail="Native scheduled runs are disabled; use an operator-controlled scheduler",
    )


@auth.on(resources="store", actions=["get", "list", "list_namespaces", "search"])
async def on_store_read(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    _deny_authz(
        identity,
        action=action,
        detail="Native LangGraph store access is disabled; use APEX domain repositories",
    )


@auth.on(resources="store", actions=["put", "delete", "create", "update"])
async def on_store_write(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    _deny_authz(
        identity,
        action=action,
        detail="Native LangGraph store access is disabled; use APEX domain repositories",
    )


@auth.on
async def on_anything_else(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    """Fail closed for every future resource/action not classified above."""

    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.ADMIN, action=action)
    ensure_unscoped_admin(identity, action=action, resource="unclassified LangGraph operation")
    return None
