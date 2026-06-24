"""LangGraph Server custom auth (wired via langgraph.json -> auth.path -> `auth`).

Decorated handlers are thin wiring; authorization logic lives in pure helpers
(`scope_filter`, `ensure_thread_scope`, ...) so it stays unit-testable without
the server runtime.
"""

from collections.abc import Mapping
from typing import Any, cast

from langgraph_sdk import Auth
from langgraph_sdk.auth import is_studio_user

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import AuthStoreUnavailableError, extract_api_key, get_default_resolver

auth = Auth()

# `langgraph dev` Studio requests bypass key auth with a StudioUser; treat as local admin.
_STUDIO_IDENTITY = ConsumerIdentity(
    consumer_id="studio",
    name="studio",
    consumer_type=ConsumerType.INTERNAL,
    role=Role.ADMIN,
)


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


def ensure_role(identity: ConsumerIdentity, minimum: Role) -> None:
    if not identity.role.at_least(minimum):
        raise Auth.exceptions.HTTPException(
            status_code=403, detail=f"Requires role '{minimum.value}' or higher"
        )


def ensure_thread_scope(identity: ConsumerIdentity, metadata: dict[str, Any]) -> None:
    """Stamp/verify `metadata['project_id']` against the consumer's scopes (mutates).

    Unscoped admins pass untouched. Scoped consumers: a missing project_id is stamped
    when exactly one project is in scope, otherwise required; an out-of-scope
    project_id raises 403.
    """
    if identity.is_unscoped:
        return
    projects = identity.scoped_project_ids()
    project_id = metadata.get("project_id")
    if project_id is None:
        if len(projects) == 1:
            metadata["project_id"] = projects[0]
            return
        raise Auth.exceptions.HTTPException(
            status_code=403, detail="project_id metadata is required for scoped consumers"
        )
    if project_id not in projects:
        raise Auth.exceptions.HTTPException(
            status_code=403,
            detail=f"Project '{project_id}' is outside this consumer's scopes",
        )


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


def ensure_run_scope(identity: ConsumerIdentity, value: Auth.types.RunsCreate) -> None:
    """Validate project-bearing run input/config before the graph can execute.

    Thread-scoped run creates still rely on LangGraph's metadata filter for the
    target thread. Stateless project graphs, however, have no thread metadata to
    protect, so project-scoped assistants must carry an allowed project_id.
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
    explicit_projects = [project for project in explicit if project is not None]
    for project_id in explicit_projects:
        if not identity.allows_project(project_id):
            raise Auth.exceptions.HTTPException(
                status_code=403,
                detail=f"Project '{project_id}' is outside this consumer's scopes",
            )

    thread_id = payload.get("thread_id")
    assistant_id = str(payload.get("assistant_id") or "")
    requires_project = thread_id is None and assistant_id in {"pipeline", "context"}
    if not explicit_projects and requires_project:
        projects = identity.scoped_project_ids()
        if len(projects) != 1:
            raise Auth.exceptions.HTTPException(
                status_code=403,
                detail="project_id is required for scoped stateless runs",
            )
        project_id = projects[0]
        if assistant_id == "context":
            input_payload["project_id"] = project_id
            payload["input"] = input_payload
        configurable["project_id"] = project_id
        config_payload["configurable"] = configurable
        payload["config"] = config_payload
        metadata["project_id"] = project_id
        payload["metadata"] = metadata


def scope_filter(identity: ConsumerIdentity) -> Auth.types.FilterType | bool | None:
    """Metadata filter limiting reads/searches to scoped projects.

    None = unscoped (no filter); False = no projects in scope (deny).
    """
    if identity.is_unscoped:
        return None
    projects = identity.scoped_project_ids()
    if not projects:
        return False
    if len(projects) == 1:
        return {"project_id": {"$eq": projects[0]}}
    # $or is supported by the installed runtime (langgraph_runtime_inmem ops) even
    # though the SDK's FilterType alias does not model it yet.
    return cast(
        "Auth.types.FilterType",
        {"$or": [{"project_id": {"$eq": project}} for project in projects]},
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
        raise Auth.exceptions.HTTPException(status_code=401, detail="Invalid or missing API key")
    return user_payload(identity)


@auth.on.threads.create
async def on_threads_create(ctx: Auth.types.AuthContext, value: Auth.types.ThreadsCreate) -> None:
    identity = identity_from_user(ctx.user)
    ensure_role(identity, Role.OPERATOR)
    metadata = value.setdefault("metadata", {})
    metadata.setdefault("created_by", identity.consumer_id)
    ensure_thread_scope(identity, metadata)


@auth.on.threads.create_run
async def on_threads_create_run(
    ctx: Auth.types.AuthContext, value: Auth.types.RunsCreate
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    ensure_role(identity, Role.OPERATOR)
    ensure_run_scope(identity, value)
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
    ensure_role(identity, Role.OPERATOR)
    return scope_filter(identity)


@auth.on.threads.delete
async def on_threads_delete(
    ctx: Auth.types.AuthContext, value: Auth.types.ThreadsDelete
) -> Auth.types.HandlerResult:
    identity = identity_from_user(ctx.user)
    ensure_role(identity, Role.OPERATOR)
    return scope_filter(identity)


@auth.on(resources="assistants", actions=["create", "update", "delete"])
async def on_assistants_write(ctx: Auth.types.AuthContext, value: Any) -> None:
    ensure_role(identity_from_user(ctx.user), Role.ADMIN)


@auth.on
async def on_anything_else(ctx: Auth.types.AuthContext, value: Any) -> Auth.types.HandlerResult:
    """Fallback: authenticated but still project-filtered; scoped consumers fail closed."""
    return scope_filter(identity_from_user(ctx.user))
