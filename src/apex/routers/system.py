from fastapi import APIRouter
from pydantic import BaseModel

from apex.app.dependencies import SettingsDep

router = APIRouter(tags=["system"])


class SystemInfo(BaseModel):
    name: str
    version: str
    environment: str
    features: dict[str, bool]


@router.get("/system/info", operation_id="getSystemInfo")
async def get_system_info(settings: SettingsDep) -> SystemInfo:
    return SystemInfo(
        name=settings.app_name,
        version=settings.version,
        environment=settings.environment,
        features={},
    )
