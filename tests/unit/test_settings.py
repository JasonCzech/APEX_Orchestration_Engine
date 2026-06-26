import pytest
from pydantic import ValidationError
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
    monkeypatch.setenv(
        "APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?sslmode=require"
    )
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    settings = CleanEnvSettings()
    assert settings.environment == "staging"
    assert settings.database.uri == "postgresql+asyncpg://u:p@db:5432/x?sslmode=require"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_locked_down_env_rejects_auth_disabled(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", environment)
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("APEX_AUTH__ENABLED", "false")
    with pytest.raises(ValidationError, match="auth.enabled=false"):
        CleanEnvSettings()


def test_locked_down_env_rejects_dev_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", "dev")
    with pytest.raises(ValidationError, match="auth.dev_api_key"):
        CleanEnvSettings()


@pytest.mark.parametrize(
    "uri",
    [
        "postgresql+asyncpg://apex:apex@localhost:5432/apex",
        "postgresql+asyncpg://u:p@127.0.0.1:5432/x",
    ],
)
def test_locked_down_env_rejects_local_database_uri(
    monkeypatch: pytest.MonkeyPatch, uri: str
) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", uri)
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    with pytest.raises(ValidationError, match="database.uri"):
        CleanEnvSettings()


def test_rejects_wildcard_cors_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["*"]')
    with pytest.raises(ValidationError, match="cors_origins"):
        CleanEnvSettings()


def test_locked_down_env_rejects_insecure_cors_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["http://dashboard.example.com"]')
    with pytest.raises(ValidationError, match="cors_origins"):
        CleanEnvSettings()


def test_locked_down_env_accepts_database_ssl_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("APEX_DATABASE__SSL_MODE", "require")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')

    settings = CleanEnvSettings()

    assert settings.database.ssl_mode == "require"


def test_locked_down_env_rejects_database_without_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')

    with pytest.raises(ValidationError, match="TLS/SSL"):
        CleanEnvSettings()
