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
from pydantic import ValidationError

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import AuthStoreUnavailableError, extract_api_key, get_default_resolver
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.services.audit import append_audit_event_best_effort, event_from_identity
from apex.services.langgraph_client import TRUSTED_LOOPBACK_CLAIM, is_trusted_loopback
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
_PROJECT_NAMESPACE_PREFIX = ("apex", "project")


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
    text = str(value).strip()
    return text or None


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
    if identity.is_unscoped:
        return None

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
    for project_id in explicit_projects:
        if not identity.allows_project(project_id):
            _deny_authz(
                identity,
                action=action,
                detail=f"Project '{project_id}' is outside this consumer's scopes",
            )

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
) -> tuple[str, int]:
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
    return target.base_url, target.version


async def ensure_run_environment(
    identity: ConsumerIdentity,
    value: Auth.types.RunsCreate,
    *,
    action: str = "runs.create",
) -> None:
    """Replace caller target data with an authorized, catalog-resolved immutable target."""

    payload = _mapping(value)
    run_args = _run_arguments(payload)
    config_payload = _mapping(run_args.get("config"))
    configurable = _mapping(config_payload.get("configurable"))
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
        try:
            target, version = await _load_run_environment_target(environment_id, project_id, app_id)
            configurable["environment_target"] = target
            configurable["environment_target_version"] = version
        except LookupError as exc:
            raise Auth.exceptions.HTTPException(status_code=404, detail=str(exc)) from exc

    config_payload["configurable"] = configurable
    run_args["config"] = config_payload


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
    try:
        requested_action = payload.get("action") or run_args.get("action")
        multitask_strategy = payload.get("multitask_strategy") or run_args.get("multitask_strategy")
        if not trusted_loopback and requested_action in {"interrupt", "rollback"}:
            raise ValueError(
                "run action cannot interrupt or roll back an existing run; use "
                "/v1/pipelines/{thread_id}/abort"
            )
        if not trusted_loopback and multitask_strategy in {"interrupt", "rollback"}:
            raise ValueError(
                "multitask_strategy cannot interrupt or roll back an existing run; "
                "use 'reject' or /v1/pipelines/{thread_id}/abort"
            )
        if raw_input is not None and not isinstance(raw_input, Mapping):
            raise ValueError("run input must be a JSON object")
        requested_thread_id = _project_id(payload.get("thread_id"))
        configured_thread_id = _project_id(configurable.get("thread_id"))
        if configured_thread_id is not None and configured_thread_id != requested_thread_id:
            raise ValueError(
                "config.configurable.thread_id is server-owned and must match the target thread"
            )
        PipelineConfigurable.model_validate(configurable)
        validate_public_run_input(input_payload)
        assistant_id = str(payload.get("assistant_id") or run_args.get("assistant_id") or "")
        if assistant_id == "context":
            validate_context_run_input(input_payload)
        elif assistant_id == "playground":
            validate_playground_run_input(input_payload)
        command = run_args.get("command")
        if command is not None:
            validate_gate_payload(command)
    except (ValidationError, ValueError) as exc:
        _schedule_auth_decision(
            identity,
            action=action,
            decision="denied",
            status_code=422,
            reason="run resource or payload limits rejected the request",
        )
        if isinstance(exc, ValidationError):
            error = exc.errors(include_url=False, include_context=False, include_input=False)[0]
            location = ".".join(str(part) for part in error.get("loc") or ())
            detail = f"{location}: {error['msg']}" if location else str(error["msg"])
        else:
            detail = str(exc)
        raise Auth.exceptions.HTTPException(
            status_code=422, detail=f"Invalid run controls: {detail}"
        ) from exc


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
    return user_payload(identity, trusted_loopback=is_trusted_loopback(headers))


@auth.on.threads.create
async def on_threads_create(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsCreate
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    metadata = value.setdefault("metadata", {})
    metadata.setdefault("created_by", identity.consumer_id)
    ensure_thread_scope(identity, metadata, action=action)
    return scope_filter(identity)


@auth.on.threads.create_run
async def on_threads_create_run(
    ctx: Auth.types.AuthContext, value: Auth.types.RunsCreate
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    result = ensure_run_scope(identity, value, action=action)
    await ensure_run_environment(identity, value, action=action)
    ensure_run_controls(
        identity,
        value,
        action=action,
        trusted_loopback=_is_trusted_loopback_user(ctx.user),
    )
    return result


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
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
    payload = _mapping(value)
    if payload.get("action") in {"interrupt", "rollback"} and not _is_trusted_loopback_user(
        ctx.user
    ):
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
    if (
        "metadata" not in payload
        and payload.get("action") is None
        and not _is_trusted_loopback_user(ctx.user)
    ):
        _deny_authz(
            identity,
            action=action,
            detail=(
                "Native thread state updates are disabled; use the validated /v1 pipeline APIs"
            ),
        )
    metadata = _mapping(payload.get("metadata"))
    if not identity.is_unscoped and ({"project_id", "app_id"} & metadata.keys()):
        _deny_authz(
            identity,
            action=action,
            detail="Scoped consumers cannot mutate thread ownership metadata",
        )
    return scope_filter(identity)


@auth.on.threads.delete
async def on_threads_delete(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsDelete
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.OPERATOR, action=action)
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
    ensure_metadata_scope(identity, value, action=action)
    # On update, constrain the existing resource as well as stamping the new
    # metadata; otherwise a scoped admin could take over a sibling assistant by id.
    return scope_filter(identity)


@auth.on(resources="assistants", actions=["read", "search"])
async def on_assistants_read(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.VIEWER, action=action)
    # Read/search payloads commonly carry no metadata. In that case the
    # returned server-side filter is sufficient and must support identities
    # spanning multiple projects. If metadata was explicitly supplied, still
    # reject an out-of-scope selector.
    if _mapping(_mapping(value).get("metadata")):
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
    """Fail closed for every future resource/action not classified above."""

    identity = identity_from_user(ctx.user)
    action = _authz_action(ctx)
    ensure_role(identity, Role.ADMIN, action=action)
    ensure_unscoped_admin(identity, action=action, resource="unclassified LangGraph operation")
    return None
