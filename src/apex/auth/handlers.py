"""LangGraph Server custom auth (wired via langgraph.json -> auth.path -> `auth`).

Decorated handlers are thin wiring; authorization logic lives in pure helpers
(`scope_filter`, `ensure_thread_scope`, ...) so it stays unit-testable without
the server runtime.
"""

from typing import Any, cast

from langgraph_sdk import Auth
from langgraph_sdk.auth import is_studio_user

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import extract_api_key, get_default_resolver

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

    scopes = field("scopes") or []
    return ConsumerIdentity(
        consumer_id=str(field("identity")),
        name=str(field("name") or field("display_name") or field("identity")),
        consumer_type=ConsumerType(field("consumer_type") or ConsumerType.HEADLESS),
        role=Role(field("role") or Role.VIEWER),
        scopes=[ScopeRef.model_validate(dict(scope)) for scope in scopes],
    )


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
    identity = await get_default_resolver().resolve(extract_api_key(headers))
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


@auth.on(resources="assistants", actions=["create", "update", "delete"])
async def on_assistants_write(ctx: Auth.types.AuthContext, value: Any) -> None:
    ensure_role(identity_from_user(ctx.user), Role.ADMIN)


@auth.on
async def on_anything_else(ctx: Auth.types.AuthContext, value: Any) -> bool:
    """Fallback: any authenticated consumer may use resources without a stricter rule."""
    return True
