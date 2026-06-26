from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from apex.auth.identity import ConsumerIdentity, Role
from apex.auth.service import AuthStoreUnavailableError, extract_api_key, get_default_resolver
from apex.settings import ApexSettings, get_settings

SettingsDep = Annotated[ApexSettings, Depends(get_settings)]


async def get_current_identity(request: Request) -> ConsumerIdentity:
    """Resolve the calling API consumer from x-api-key / bearer headers (401 if none)."""
    try:
        identity = await get_default_resolver().resolve(extract_api_key(request.headers))
    except AuthStoreUnavailableError as exc:
        raise HTTPException(status_code=503, detail="API key store is unavailable") from exc
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return identity


CurrentIdentity = Annotated[ConsumerIdentity, Depends(get_current_identity)]


def require_role(minimum: Role) -> Callable[..., Awaitable[ConsumerIdentity]]:
    """Dependency factory: 403 unless the consumer holds `minimum` role or higher."""

    async def dependency(identity: CurrentIdentity) -> ConsumerIdentity:
        if not identity.role.at_least(minimum):
            raise HTTPException(
                status_code=403, detail=f"Requires role '{minimum.value}' or higher"
            )
        return identity

    return dependency


def require_scope(
    *,
    project_id: str | None = None,
    app_id: str | None = None,
) -> Callable[..., Awaitable[ConsumerIdentity]]:
    """Dependency factory: 403 unless the consumer may access the requested scope.

    This covers static route-level checks. Routes whose project/app owner must be
    loaded from the database should call `identity.allows_scope(...)` after the
    resource lookup so cross-scope reads can continue returning 404.
    """

    async def dependency(identity: CurrentIdentity) -> ConsumerIdentity:
        if project_id is None:
            return identity
        if not identity.allows_scope(project_id=project_id, app_id=app_id):
            raise HTTPException(
                status_code=403,
                detail=f"Project '{project_id}' is outside this consumer's scopes",
            )
        return identity

    return dependency
