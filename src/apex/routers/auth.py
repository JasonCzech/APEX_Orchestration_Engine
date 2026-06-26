"""Authentication/session introspection endpoints."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from apex.app.dependencies import CurrentIdentity
from apex.auth.identity import ConsumerType, Role, ScopeRef

router = APIRouter(prefix="/auth", tags=["auth"])


class CurrentPrincipalResponse(BaseModel):
    principal_kind: str = "api_consumer"
    principal_id: str
    name: str
    consumer_type: ConsumerType
    role: Role
    scopes: list[ScopeRef] = Field(default_factory=list)
    is_unscoped: bool
    org_id: str | None = None
    workspace_id: str | None = None
    session_expires_at: str | None = None
    mfa_required: bool = False
    step_up_required: bool = False
    capabilities: dict[str, Any] = Field(default_factory=dict)


@router.get("/me", operation_id="getAuthMe", response_model=CurrentPrincipalResponse)
async def get_auth_me(identity: CurrentIdentity) -> CurrentPrincipalResponse:
    return CurrentPrincipalResponse(
        principal_id=identity.consumer_id,
        name=identity.name,
        consumer_type=identity.consumer_type,
        role=identity.role,
        scopes=identity.scopes,
        is_unscoped=identity.is_unscoped,
        capabilities={
            "api_keys": identity.consumer_type is not ConsumerType.DASHBOARD,
            "platform_admin": identity.is_unscoped,
        },
    )
