"""LangGraph Server custom auth (wired via langgraph.json -> auth.path -> `auth`).

Decorated handlers are thin wiring; authorization logic lives in pure helpers
(`scope_filter`, `ensure_thread_scope`, ...) so it stays unit-testable without
the server runtime.
"""

import asyncio
from collections.abc import Mapping
from typing import Any, cast

from langgraph_sdk import Auth
from langgraph_sdk.auth import is_studio_user

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import AuthStoreUnavailableError, extract_api_key, get_default_resolver
from apex.services.audit import append_audit_event_best_effort, event_from_identity
from apex.settings import get_settings

auth = Auth()

# `langgraph dev` Studio requests bypass key auth with a StudioUser; treat as local admin.
_STUDIO_IDENTITY = ConsumerIdentity(
    consumer_id="studio",
    name="studio",
    consumer_type=ConsumerType.INTERNAL,
    role=Role.ADMIN,
)
_PROJECT_NAMESPACE_PREFIX = ("apex", "project")


def user_payload(identity: ConsumerIdentity) -> dict[str, Any]:
    """Authenticate return shape; surfaces in `configurable.langgraph_auth_user`."""
    return {
        "identity": identity.consumer_id,
        "display_name": identity.name,
        "name": identity.name,
        "role": identity.role.value,
        "consumer_type": identity.consumer_type.value,
        "scopes": [scope.model_dump(mode="json") for scope in identity.scopes],
    }


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
    try:
        return ConsumerIdentity(
            consumer_id=str(identity),
            name=str(field("name") or field("display_name") or identity),
            consumer_type=ConsumerType(consumer_type),
            role=Role(role),
            scopes=[ScopeRef.model_validate(dict(scope)) for scope in scopes],
        )
    except Exception as exc:
        raise Auth.exceptions.HTTPException(
            status_code=401, detail="Malformed auth identity"
        ) from exc


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
        asyncio.get_running_loop().create_task(
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
    except RuntimeError:
        return


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
    if identity.is_unscoped:
        return
    projects = identity.scoped_project_ids()
    project_id = _project_id(metadata.get("project_id"))
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

    app_id = _project_id(metadata.get("app_id"))
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
    input_payload: dict[str, Any],
    config_payload: dict[str, Any],
    configurable: dict[str, Any],
    metadata: dict[str, Any],
    *,
    project_id: str,
    app_id: str | None,
) -> None:
    input_payload["project_id"] = project_id
    configurable["project_id"] = project_id
    metadata["project_id"] = project_id
    if app_id is not None:
        input_payload["app_id"] = app_id
        configurable["app_id"] = app_id
        metadata["app_id"] = app_id
    payload["input"] = input_payload
    config_payload["configurable"] = configurable
    payload["config"] = config_payload
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


def _nested_mapping(root: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = root
    for key in keys:
        current = _mapping(current).get(key)
    return _mapping(current)


def _project_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def ensure_run_scope(
    identity: ConsumerIdentity,
    value: Auth.types.RunsCreate,
    *,
    action: str = "runs.create",
) -> None:
    """Validate project/app-bearing run input/config before the graph can execute.

    Thread-scoped run creates still rely on LangGraph's metadata filter for the
    target thread. Stateless runs have no thread metadata to protect and may
    target built-in graph ids, UUID-backed assistants, or future project-bearing
    graphs, so scoped consumers must always carry or receive an allowed
    project_id and, for app-scoped keys, an allowed app_id before execution starts.
    """
    if identity.is_unscoped:
        return

    payload = _mapping(value)
    input_payload = _mapping(payload.get("input"))
    config_payload = _mapping(payload.get("config"))
    configurable = _nested_mapping(payload, "config", "configurable")
    metadata = _mapping(payload.get("metadata"))

    explicit = [
        _project_id(input_payload.get("project_id")),
        _project_id(configurable.get("project_id")),
        _project_id(metadata.get("project_id")),
    ]
    explicit_apps = [
        _project_id(input_payload.get("app_id")),
        _project_id(configurable.get("app_id")),
        _project_id(metadata.get("app_id")),
    ]
    explicit_projects = [project for project in explicit if project is not None]
    for project_id in explicit_projects:
        if not identity.allows_project(project_id):
            _deny_authz(
                identity,
                action=action,
                detail=f"Project '{project_id}' is outside this consumer's scopes",
            )

    thread_id = payload.get("thread_id")
    stateless = thread_id is None
    unique_projects = sorted(set(explicit_projects))
    if len(unique_projects) > 1:
        _deny_authz(
            identity,
            action=action,
            detail="Conflicting project_id values are not allowed for scoped runs",
        )

    effective_project = unique_projects[0] if unique_projects else None
    unique_apps = sorted({app_id for app_id in explicit_apps if app_id is not None})
    if len(unique_apps) > 1:
        _deny_authz(
            identity,
            action=action,
            detail="Conflicting app_id values are not allowed for scoped runs",
        )
    effective_app = unique_apps[0] if unique_apps else None

    if effective_project is None and stateless:
        projects = identity.scoped_project_ids()
        if len(projects) != 1:
            _deny_authz(
                identity,
                action=action,
                detail="project_id is required for scoped stateless runs",
            )
        effective_project = projects[0]

    if effective_project is None and effective_app is not None:
        projects = identity.scoped_project_ids()
        if len(projects) == 1:
            effective_project = projects[0]
        else:
            _deny_authz(
                identity,
                action=action,
                detail="project_id is required when app_id is provided for scoped runs",
            )

    if effective_project is not None:
        effective_app = _require_langgraph_scope(
            identity,
            project_id=effective_project,
            app_id=effective_app,
            action=action,
        )
        if stateless or effective_app is not None:
            _stamp_run_scope(
                payload,
                input_payload,
                config_payload,
                configurable,
                metadata,
                project_id=effective_project,
                app_id=effective_app,
            )


def _scoped_project_prefix(project_id: str) -> tuple[str, str, str]:
    return (*_PROJECT_NAMESPACE_PREFIX, project_id)


def ensure_store_namespace_scope(
    identity: ConsumerIdentity,
    value: Any,
    *,
    action: str = "store.scope",
) -> None:
    """Project-prefix LangGraph store namespaces for scoped consumers.

    The SDK documents store namespace mutation as the intended auth mechanism.
    A scoped single-project consumer may use legacy unprefixed namespaces; the
    handler transparently prefixes them. Multi-project consumers must select a
    project-prefixed namespace so the target project is unambiguous.
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
    selected_project = _project_from_namespace(namespace)
    if selected_project is not None:
        if selected_project not in projects:
            _deny_authz(
                identity,
                action=action,
                detail=(
                    f"Store namespace project '{selected_project}' is outside "
                    "this consumer's scopes"
                ),
            )
        value["namespace"] = namespace
        return
    if tuple(namespace[:2]) == _PROJECT_NAMESPACE_PREFIX:
        _deny_authz(
            identity,
            action=action,
            detail="project-prefixed store namespace must include a project_id",
        )

    if len(projects) != 1:
        _deny_authz(
            identity,
            action=action,
            detail="project-prefixed store namespace is required for multi-project consumers",
        )
    value["namespace"] = (*_scoped_project_prefix(projects[0]), *namespace)


def ensure_metadata_scope(
    identity: ConsumerIdentity,
    value: Any,
    *,
    action: str,
    required: bool = False,
) -> None:
    """Stamp/verify `metadata.project_id` on assistant-like resources."""

    if identity.is_unscoped:
        return
    if not isinstance(value, dict):
        _deny_authz(identity, action=action, detail="resource payload is malformed")
    metadata = _mapping(value.get("metadata"))
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


def _project_from_namespace(namespace: tuple[Any, ...]) -> str | None:
    if len(namespace) < 3 or tuple(namespace[:2]) != _PROJECT_NAMESPACE_PREFIX:
        return None
    project_id = _project_id(namespace[2])
    if project_id is None:
        return None
    return project_id


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


@auth.authenticate
async def authenticate(headers: dict[bytes, bytes]) -> dict[str, Any]:
    try:
        identity = await get_default_resolver().resolve(extract_api_key(headers))
    except AuthStoreUnavailableError as exc:
        raise Auth.exceptions.HTTPException(
            status_code=503, detail="API key store is unavailable"
        ) from exc
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
    return user_payload(identity)


@auth.on.threads.create
async def on_threads_create(ctx: Auth.types.AuthContext, value: Auth.types.ThreadsCreate) -> None:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    metadata = value.setdefault("metadata", {})
    metadata.setdefault("created_by", identity.consumer_id)
    ensure_thread_scope(identity, metadata, action=action)


@auth.on.threads.create_run
async def on_threads_create_run(
    ctx: Auth.types.AuthContext, value: Auth.types.RunsCreate
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    ensure_run_scope(identity, value, action=action)
    return scope_filter(identity)


@auth.on.threads.read
async def on_threads_read(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsRead
) -> Auth.types.HandlerResult:
    return scope_filter(identity_from_user(ctx.user))


@auth.on.threads.search
async def on_threads_search(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsSearch
) -> Auth.types.HandlerResult:
    return scope_filter(identity_from_user(ctx.user))


@auth.on.threads.update
async def on_threads_update(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsUpdate
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    ensure_role(identity, Role.OPERATOR, action=_authz_action(ctx))
    return scope_filter(identity)


@auth.on.threads.delete
async def on_threads_delete(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsDelete
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    ensure_role(identity, Role.OPERATOR, action=_authz_action(ctx))
    return scope_filter(identity)


@auth.on(resources="assistants", actions=["create", "update", "delete"])
async def on_assistants_write(ctx: Auth.types.AuthContext, value: Any) -> None:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.ADMIN, action=action)
    if ctx.action == "delete":
        ensure_unscoped_admin(identity, action=action, resource="assistant deletion")
        return
    ensure_metadata_scope(identity, value, action=action)


@auth.on(resources="assistants", actions=["read", "search"])
async def on_assistants_read(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.VIEWER, action=action)
    ensure_metadata_scope(identity, value, action=action)
    return scope_filter(identity)


@auth.on(resources="crons", actions=["read", "search"])
async def on_crons_read(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.VIEWER, action=action)
    ensure_unscoped_admin(identity, action=action, resource="cron access")
    return None


@auth.on(resources="crons", actions=["create", "update", "delete"])
async def on_crons_write(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    ensure_unscoped_admin(identity, action=action, resource="cron mutation")
    return None


@auth.on(resources="store", actions=["get", "list", "search"])
async def on_store_read(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.VIEWER, action=action)
    ensure_store_namespace_scope(identity, value, action=action)
    return None


@auth.on(resources="store", actions=["put", "delete", "create", "update"])
async def on_store_write(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    ensure_store_namespace_scope(identity, value, action=action)
    return None


@auth.on
async def on_anything_else(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    """Fallback: authenticated but still project-filtered; scoped consumers fail closed."""
    return scope_filter(identity_from_user(ctx.user))
