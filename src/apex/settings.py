from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from ipaddress import ip_network
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URI = "postgresql+asyncpg://apex:apex@localhost:5432/apex"
LOCKED_DOWN_ENVIRONMENTS = {"production", "prod", "staging", "stage"}
UNLOCKED_ENVIRONMENTS = {"local", "development", "dev", "test", "testing", "compose"}
LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
DATABASE_SSL_MODES = {"require", "verify-ca", "verify-full"}


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
    ssl_mode: str | None = None


class AuthSettings(BaseModel):
    enabled: bool = True
    # Local-dev shortcut: a key resolving to a synthetic unscoped admin without DB access.
    dev_api_key: str | None = None
    # Server-held secret used to HMAC API keys at rest. Required in locked environments.
    api_key_hash_pepper: str | None = None


class RateLimitSettings(BaseModel):
    enabled: bool = True
    requests: int = 600
    window_s: int = 60
    max_buckets: int = 10000
    auth_failures: int = 10
    auth_failure_window_s: int = 300
    auth_lockout_s: int = 300
    # X-Forwarded-For is used only when the immediate ASGI peer is in one of
    # these networks. Empty by default so direct clients cannot spoof source IPs.
    trusted_proxy_cidrs: list[str] = []
    protected_path_prefixes: list[str] = [
        "/v1/",
        "/threads",
        "/runs",
        "/assistants",
        "/crons",
        "/store",
    ]

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def validate_trusted_proxy_cidrs(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                ip_network(value, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy CIDR {value!r}") from exc
        return values


class SecurityHeadersSettings(BaseModel):
    enabled: bool = True
    content_security_policy: str = (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    hsts_max_age_s: int = 31536000
    hsts_include_subdomains: bool = True


class LLMSettings(BaseModel):
    """Anthropic LLM agent config (env: APEX_LLM__*).

    `anthropic_api_key` doubles as the opt-in gate: a run that requests the
    `anthropic` agent backend falls back to the deterministic stub when no key
    is present, so the offline test suite (which sets neither) stays stub-only.
    """

    anthropic_api_key: str | None = None
    default_model: str = Field(default="claude-opus-4-8", min_length=1, max_length=200)
    # Per-run model overrides are deny-by-default outside this deployment-owned
    # allow-list. Configure with APEX_LLM__ALLOWED_MODELS as a JSON array.
    allowed_models: list[str] = [
        "claude-opus-4-8",
        "claude-sonnet-4-5",
        "claude-3-5-sonnet-latest",
        "claude-3-5-haiku-latest",
    ]
    max_tokens: int = Field(default=8000, ge=1, le=64_000)
    timeout_s: float = Field(default=120.0, gt=0, le=600.0, allow_inf_nan=False)
    # Adaptive thinking is the recommended mode for Opus 4.8 (budget_tokens is
    # rejected); disable only if a pinned model/library combo can't accept it.
    adaptive_thinking: bool = True

    # `fetch_results` tool (the "pass a link" pull path). Deny-by-default: inert
    # unless enabled AND given an explicit host allow-list. SSRF-guarded in
    # apex.services.results_fetch.
    fetch_tool_enabled: bool = False
    fetch_allowed_hosts: list[str] = []
    fetch_allow_private_hosts: bool = False
    fetch_max_bytes: int = Field(default=1_000_000, ge=1, le=10_000_000)
    fetch_timeout_s: float = Field(default=20.0, gt=0, le=120.0, allow_inf_nan=False)
    fetch_max_tool_iters: int = Field(default=4, ge=1, le=10)

    @model_validator(mode="after")
    def validate_model_allowlist(self) -> "LLMSettings":
        normalized = [model.strip() for model in self.allowed_models]
        if not normalized or any(not model or len(model) > 200 for model in normalized):
            raise ValueError("llm.allowed_models must contain 1-200 character model names")
        if len(set(normalized)) != len(normalized):
            raise ValueError("llm.allowed_models must not contain duplicates")
        self.allowed_models = normalized
        self.default_model = self.default_model.strip()
        if self.default_model not in normalized:
            raise ValueError("llm.default_model must be present in llm.allowed_models")
        return self


class RunControlSettings(BaseModel):
    """Deployment-owned request and prompt budgets (env: APEX_RUNS__*)."""

    max_context_packets: int = Field(default=32, ge=1, le=64)
    # Counts the complete serialized packet payload: ids, source, title, summary,
    # refs, text, keys, and separators. Rendering applies a second cap below.
    max_context_chars_total: int = Field(default=160_000, ge=1_000, le=500_000)
    max_gate_payload_chars: int = Field(default=100_000, ge=1_000, le=250_000)
    max_gate_payload_nodes: int = Field(default=512, ge=16, le=2_000)
    max_gate_string_chars: int = Field(default=20_000, ge=100, le=50_000)
    max_prompt_part_chars: int = Field(default=50_000, ge=1_000, le=100_000)
    # Final system + user prompt after evidence and revision text are rendered.
    max_model_input_chars: int = Field(default=220_000, ge=10_000, le=500_000)
    # Stateless context/playground runs can be created directly through the
    # LangGraph SDK, bypassing FastAPI request models.  These limits therefore
    # bound their complete JSON tree as well as the provider calls it can fan out.
    max_work_item_keys: int = Field(default=50, ge=1, le=100)
    max_stateless_payload_bytes: int = Field(default=200_000, ge=10_000, le=1_000_000)
    max_stateless_payload_nodes: int = Field(default=512, ge=16, le=2_000)
    max_stateless_payload_depth: int = Field(default=12, ge=2, le=32)


class DocumentIngestionSettings(BaseModel):
    """Context-document text extraction + injection budgets (env: APEX_DOCUMENTS__*).

    Uploaded context files are parsed to text on upload and stored capped at
    ``max_extract_chars``. When assembled into a phase prompt they're trimmed again to
    ``max_context_chars_per_doc`` each and ``max_context_chars_total`` across all evidence,
    so a large attachment can't blow the model's context window. ``summary_chars`` bounds
    the auto-derived summary used when an uploader provides none.
    """

    max_extract_chars: int = 200_000
    max_context_chars_per_doc: int = 50_000
    max_context_chars_total: int = 150_000
    summary_chars: int = 280


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
    # Serializes costly remote run creation across API/worker replicas through
    # PostgreSQL advisory locks. Local single-process development may leave it off.
    distributed_remote_creation_lock: bool = False
    # Only this dedicated namespace is readable through the env SecretsPort.
    # Keeping it separate from the APEX_ settings namespace prevents a
    # connection secret_ref from resolving database/auth/platform credentials.
    env_secret_prefixes: list[str] = ["APEX_INTEGRATION_"]
    database: DatabaseSettings = DatabaseSettings()
    # LangGraph's own checkpoint DSN and Redis stream DSN use unprefixed
    # environment variables; mirror them here so locked-down validation covers
    # both transports rather than only the apex schema database.
    langgraph_database_uri: str | None = Field(default=None, validation_alias="DATABASE_URI")
    redis_uri: str | None = Field(default=None, validation_alias="REDIS_URI")
    auth: AuthSettings = AuthSettings()
    rate_limit: RateLimitSettings = RateLimitSettings()
    security_headers: SecurityHeadersSettings = SecurityHeadersSettings()
    llm: LLMSettings = LLMSettings()
    documents: DocumentIngestionSettings = DocumentIngestionSettings()
    runs: RunControlSettings = RunControlSettings()

    @property
    def is_locked_down(self) -> bool:
        """True for production/staging-class environments (no dev affordances)."""
        return self.environment.strip().lower() not in UNLOCKED_ENVIRONMENTS

    @model_validator(mode="after")
    def validate_production_lockdown(self) -> "ApexSettings":
        env = self.environment.strip().lower()
        errors: list[str] = []
        normalized_origins = [origin.strip() for origin in self.cors_origins]
        if "*" in normalized_origins:
            errors.append("cors_origins must not contain '*' when credentials are allowed")
        if self.distributed_remote_creation_lock:
            try:
                database_scheme = urlsplit(self.database.uri).scheme.lower()
            except ValueError:
                database_scheme = ""
            if not database_scheme.startswith("postgresql"):
                errors.append("distributed_remote_creation_lock requires a PostgreSQL database URI")
        if env in UNLOCKED_ENVIRONMENTS:
            if errors:
                raise ValueError(f"Unsafe configuration: {'; '.join(errors)}")
            return self

        if not self.auth.enabled:
            errors.append("auth.enabled=false is allowed only in local/test environments")
        if self.auth.dev_api_key:
            errors.append("auth.dev_api_key is allowed only in local/test environments")
        if not self.auth.api_key_hash_pepper:
            errors.append("auth.api_key_hash_pepper is required in locked environments")
        if _is_local_database_uri(self.database.uri):
            errors.append("database.uri must not point at localhost/default credentials")
        if not _database_uri_requires_ssl(self.database.uri, self.database.ssl_mode):
            errors.append("database.uri must require TLS/SSL in locked environments")
        if self.langgraph_database_uri is not None and not _database_uri_requires_ssl(
            self.langgraph_database_uri, None
        ):
            errors.append("DATABASE_URI must require TLS/SSL in locked environments")
        if self.redis_uri is not None and not self.redis_uri.lower().startswith("rediss://"):
            errors.append("REDIS_URI must use rediss:// in locked environments")
        if not self.security_headers.enabled:
            errors.append(
                "security_headers.enabled=false is allowed only in local/test environments"
            )
        if self.security_headers.hsts_max_age_s <= 0:
            errors.append("security_headers.hsts_max_age_s must be > 0 in locked environments")
        if self.allow_private_adapter_hosts:
            errors.append(
                "allow_private_adapter_hosts=true is allowed only in local/test environments; "
                "approve individual bootstrap connections instead"
            )
        if not self.distributed_remote_creation_lock:
            errors.append(
                "distributed_remote_creation_lock=true is required in locked environments"
            )
        if not self.env_secret_prefixes or any(
            not prefix.startswith("APEX_INTEGRATION_") for prefix in self.env_secret_prefixes
        ):
            errors.append(
                "env_secret_prefixes must be restricted to APEX_INTEGRATION_ namespaces "
                "in locked environments"
            )
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


def _database_uri_requires_ssl(uri: str, ssl_mode: str | None) -> bool:
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    if parsed.scheme.startswith("sqlite"):
        return True
    return _database_ssl_mode(uri, ssl_mode).lower() in DATABASE_SSL_MODES


def database_uses_ssl(uri: str, ssl_mode: str | None) -> bool:
    """True when SQLAlchemy/asyncpg should force a TLS connection."""

    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    if parsed.scheme.startswith("sqlite"):
        return False
    mode = _database_ssl_mode(uri, ssl_mode).lower()
    if mode:
        return mode in DATABASE_SSL_MODES
    return not _is_local_database_uri(uri)


def database_ssl_connect_args(uri: str, ssl_mode: str | None) -> dict[str, object]:
    if database_uses_ssl(uri, ssl_mode):
        mode = _database_ssl_mode(uri, ssl_mode).lower()
        # Preserve the historical secure default for remote databases; explicit
        # modes retain their exact asyncpg semantics.
        if not ssl_mode and not parse_qs(urlsplit(uri).query).get("sslmode"):
            return {"ssl": True}
        return {"ssl": mode if mode in DATABASE_SSL_MODES else "require"}
    return {}


def database_asyncpg_uri(uri: str) -> str:
    """Remove psycopg-style TLS query keys before handing a URL to asyncpg."""
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return uri
    if parsed.scheme != "postgresql+asyncpg":
        return uri
    query = [
        (key, value)
        for key, value in parse_qs(parsed.query, keep_blank_values=True).items()
        if key not in {"sslmode", "ssl"}
        for value in value
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _database_ssl_mode(uri: str, ssl_mode: str | None) -> str:
    mode = ssl_mode or ""
    if mode:
        return mode
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return ""
    if parsed.scheme.startswith("sqlite"):
        return ""
    query = parse_qs(parsed.query)
    mode = (query.get("sslmode") or [""])[0]
    if mode:
        return mode
    # asyncpg-style URLs use ``ssl=true`` rather than libpq's ``sslmode``.
    # Normalize booleans into the same policy vocabulary used by validation.
    asyncpg_ssl = (query.get("ssl") or [""])[0].strip().lower()
    if asyncpg_ssl in {"1", "true", "yes", "on"}:
        return "require"
    if asyncpg_ssl in {"0", "false", "no", "off", "disable"}:
        return "disable"
    return asyncpg_ssl


@lru_cache
def get_settings() -> ApexSettings:
    return ApexSettings()
