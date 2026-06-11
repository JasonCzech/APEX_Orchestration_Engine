from fastapi import APIRouter
from pydantic import BaseModel

from apex.app.dependencies import CurrentIdentity, SettingsDep
from apex.auth.identity import Role, ScopeRef

router = APIRouter(tags=["system"])


class ConsumerInfo(BaseModel):
    name: str
    role: Role
    scopes: list[ScopeRef]


class SystemInfo(BaseModel):
    name: str
    version: str
    environment: str
    features: dict[str, bool]
    consumer: ConsumerInfo


@router.get("/system/info", operation_id="getSystemInfo")
async def get_system_info(settings: SettingsDep, identity: CurrentIdentity) -> SystemInfo:
    return SystemInfo(
        name=settings.app_name,
        version=settings.version,
        environment=settings.environment,
        features={},
        consumer=ConsumerInfo(name=identity.name, role=identity.role, scopes=identity.scopes),
    )
