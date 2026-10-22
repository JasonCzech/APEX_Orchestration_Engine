from fastapi import APIRouter
from pydantic import BaseModel

from apex.app.dependencies import CurrentIdentity, SettingsDep
from apex.auth.identity import Role, ScopeRef

router = APIRouter(tags=["system"])


class ConsumerInfo(BaseModel):
    id: str | None = None
    name: str
    role: Role
    scopes: list[ScopeRef]


class SystemLimits(BaseModel):
    max_test_participants: int = 32
    max_context_packets: int


class SystemInfo(BaseModel):
    name: str
    version: str
    environment: str
    features: dict[str, bool]
    limits: SystemLimits
    consumer: ConsumerInfo


@router.get("/system/info", operation_id="getSystemInfo")
async def get_system_info(settings: SettingsDep, identity: CurrentIdentity) -> SystemInfo:
    return SystemInfo(
        name=settings.app_name,
        version=settings.version,
        environment=settings.environment,
        features={"cycles": settings.cycles_enabled},
        limits=SystemLimits(
            max_context_packets=settings.runs.max_context_packets,
            max_test_participants=settings.runs.max_test_participants,
        ),
        consumer=ConsumerInfo(
            id=identity.consumer_id, name=identity.name, role=identity.role, scopes=identity.scopes
        ),
    )
