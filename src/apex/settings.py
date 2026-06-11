from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


def _package_version() -> str:
    try:
        return pkg_version("apex-orchestration-engine")
    except PackageNotFoundError:
        return "0.0.0+local"


class DatabaseSettings(BaseModel):
    uri: str = "postgresql+asyncpg://apex:apex@localhost:5432/apex"
    schema_name: str = "apex"


class ApexSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APEX_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "APEX Orchestration Engine"
    version: str = _package_version()
    environment: str = "dev"
    database: DatabaseSettings = DatabaseSettings()


@lru_cache
def get_settings() -> ApexSettings:
    return ApexSettings()
