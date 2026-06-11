import pytest
from pydantic_settings import SettingsConfigDict

from apex.settings import ApexSettings


class CleanEnvSettings(ApexSettings):
    """ApexSettings without .env loading, for deterministic tests."""

    model_config = SettingsConfigDict(
        env_prefix="APEX_",
        env_nested_delimiter="__",
        env_file=None,
        extra="ignore",
    )


def test_defaults() -> None:
    settings = CleanEnvSettings()
    assert settings.app_name == "APEX Orchestration Engine"
    assert settings.database.schema_name == "apex"
    assert settings.database.uri.startswith("postgresql+asyncpg://")


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "staging")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x")
    settings = CleanEnvSettings()
    assert settings.environment == "staging"
    assert settings.database.uri == "postgresql+asyncpg://u:p@db:5432/x"
