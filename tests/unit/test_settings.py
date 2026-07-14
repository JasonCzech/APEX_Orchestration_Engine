import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from apex.settings import ApexSettings, RateLimitSettings, database_ssl_connect_args


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
    assert settings.distributed_remote_creation_lock is False
    assert settings.env_secret_prefixes == ["APEX_INTEGRATION_"]


def test_rate_limit_trusted_proxy_cidrs_are_validated() -> None:
    assert RateLimitSettings(trusted_proxy_cidrs=["10.40.0.0/20"]).trusted_proxy_cidrs == [
        "10.40.0.0/20"
    ]
    with pytest.raises(ValidationError, match="invalid trusted proxy CIDR"):
        RateLimitSettings(trusted_proxy_cidrs=["not-a-network"])


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "staging")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?sslmode=require")
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "pepper")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("APEX_DISTRIBUTED_REMOTE_CREATION_LOCK", "true")
    settings = CleanEnvSettings()
    assert settings.environment == "staging"
    assert settings.distributed_remote_creation_lock is True
    assert settings.database.uri == "postgresql+asyncpg://u:p@db:5432/x?sslmode=require"


def test_distributed_remote_creation_lock_requires_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_DATABASE__URI", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("APEX_DISTRIBUTED_REMOTE_CREATION_LOCK", "true")

    with pytest.raises(ValidationError, match="requires a PostgreSQL database URI"):
        CleanEnvSettings()


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
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "pepper")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("APEX_DISTRIBUTED_REMOTE_CREATION_LOCK", "true")

    settings = CleanEnvSettings()

    assert settings.database.ssl_mode == "require"


def test_locked_down_env_accepts_asyncpg_ssl_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?ssl=true")
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "pepper")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("APEX_DISTRIBUTED_REMOTE_CREATION_LOCK", "true")

    settings = CleanEnvSettings()

    assert settings.database.uri.endswith("?ssl=true")
    assert database_ssl_connect_args(settings.database.uri, settings.database.ssl_mode) == {
        "ssl": True
    }


def test_locked_down_env_requires_distributed_remote_creation_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?sslmode=require")
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "pepper")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')

    with pytest.raises(ValidationError, match="distributed_remote_creation_lock"):
        CleanEnvSettings()


def test_locked_down_env_rejects_database_without_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "pepper")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')

    with pytest.raises(ValidationError, match="TLS/SSL"):
        CleanEnvSettings()


def test_locked_down_env_rejects_missing_api_key_hash_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?sslmode=require")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')

    with pytest.raises(ValidationError, match="api_key_hash_pepper"):
        CleanEnvSettings()


def test_locked_down_env_rejects_disabled_security_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?sslmode=require")
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "pepper")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("APEX_SECURITY_HEADERS__ENABLED", "false")

    with pytest.raises(ValidationError, match="security_headers.enabled"):
        CleanEnvSettings()


def test_locked_down_env_rejects_disabled_hsts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?sslmode=require")
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "pepper")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("APEX_SECURITY_HEADERS__HSTS_MAX_AGE_S", "0")

    with pytest.raises(ValidationError, match="hsts_max_age_s"):
        CleanEnvSettings()


def test_locked_down_env_rejects_broad_secret_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?sslmode=require")
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "pepper")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("APEX_ENV_SECRET_PREFIXES", '["APEX_"]')

    with pytest.raises(ValidationError, match="env_secret_prefixes"):
        CleanEnvSettings()


def test_locked_down_env_rejects_global_private_adapter_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?sslmode=require")
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "pepper")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("APEX_ALLOW_PRIVATE_ADAPTER_HOSTS", "true")

    with pytest.raises(ValidationError, match="allow_private_adapter_hosts"):
        CleanEnvSettings()


def test_database_ssl_connect_args_defaults_remote_postgres_to_ssl() -> None:
    assert database_ssl_connect_args("postgresql+asyncpg://u:p@db.example.com:5432/x", None) == {
        "ssl": True
    }
    assert database_ssl_connect_args("postgresql+asyncpg://u:p@localhost:5432/x", None) == {}
    assert (
        database_ssl_connect_args(
            "postgresql+asyncpg://u:p@db.example.com:5432/x",
            "disable",
        )
        == {}
    )
