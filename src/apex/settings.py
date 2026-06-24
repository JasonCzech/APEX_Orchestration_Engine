from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from urllib.parse import urlsplit

from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URI = "postgresql+asyncpg://apex:apex@localhost:5432/apex"
LOCKED_DOWN_ENVIRONMENTS = {"production", "prod", "staging", "stage"}
LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def _package_version() -> str:
    try:
        return pkg_version("apex-orchestration-engine")
    except PackageNotFoundError:
        return "0.0.0+local"


class DatabaseSettings(BaseModel):
    uri: str = DEFAULT_DATABASE_URI
    schema_name: str = "apex"
    pool_size: int = 5
    max_overflow: int = 10
    pool_recycle_s: int = 1800


class AuthSettings(BaseModel):
    enabled: bool = True
    # Local-dev shortcut: a key resolving to a synthetic unscoped admin without DB access.
    dev_api_key: str | None = None


class RateLimitSettings(BaseModel):
    enabled: bool = True
    requests: int = 600
    window_s: int = 60
    max_buckets: int = 10000


class SecurityHeadersSettings(BaseModel):
    enabled: bool = True
    content_security_policy: str = (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )


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
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    allow_private_adapter_hosts: bool = False
    analytics_cost_visible: bool = False
    env_secret_prefixes: list[str] = ["APEX_"]
    database: DatabaseSettings = DatabaseSettings()
    auth: AuthSettings = AuthSettings()
    rate_limit: RateLimitSettings = RateLimitSettings()
    security_headers: SecurityHeadersSettings = SecurityHeadersSettings()

    @property
    def is_locked_down(self) -> bool:
        """True for production/staging-class environments (no dev affordances)."""
        return self.environment.strip().lower() in LOCKED_DOWN_ENVIRONMENTS

    @model_validator(mode="after")
    def validate_production_lockdown(self) -> "ApexSettings":
        env = self.environment.strip().lower()
        errors: list[str] = []
        normalized_origins = [origin.strip() for origin in self.cors_origins]
        if "*" in normalized_origins:
            errors.append("cors_origins must not contain '*' when credentials are allowed")
        if env not in LOCKED_DOWN_ENVIRONMENTS:
            if errors:
                raise ValueError(f"Unsafe configuration: {'; '.join(errors)}")
            return self

        if not self.auth.enabled:
            errors.append("auth.enabled=false is allowed only in local/test environments")
        if self.auth.dev_api_key:
            errors.append("auth.dev_api_key is allowed only in local/test environments")
        if _is_local_database_uri(self.database.uri):
            errors.append("database.uri must not point at localhost/default credentials")
        insecure_origins = [
            origin for origin in normalized_origins if origin and not origin.startswith("https://")
        ]
        if insecure_origins:
            errors.append("cors_origins must be explicit https:// origins in locked environments")
        if errors:
            raise ValueError(f"Unsafe {self.environment!r} configuration: {'; '.join(errors)}")
        return self


def _is_local_database_uri(uri: str) -> bool:
    if uri == DEFAULT_DATABASE_URI:
        return True
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in LOCAL_DATABASE_HOSTS


@lru_cache
def get_settings() -> ApexSettings:
    return ApexSettings()
