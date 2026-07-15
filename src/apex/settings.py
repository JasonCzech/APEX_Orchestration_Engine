import ssl
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from ipaddress import ip_address, ip_network
from typing import Literal
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from apex.domain.pipeline import MAX_CONTEXT_SUMMARY_CHARS, MAX_CONTEXT_TEXT_CHARS

DEFAULT_DATABASE_URI = "postgresql+asyncpg://apex:apex@localhost:5432/apex"
LOCKED_DOWN_ENVIRONMENTS = {"production", "prod", "staging", "stage"}
UNLOCKED_ENVIRONMENTS = {"local", "development", "dev", "test", "testing", "compose"}
LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
DATABASE_SSL_MODES = {"require", "verify-ca", "verify-full"}
DATABASE_AUTHENTICATED_SSL_MODE = "verify-full"
MIN_API_KEY_PEPPER_BYTES = 32
MAX_PREVIOUS_API_KEY_PEPPERS = 16
MAX_HTTP_REQUEST_BODY_BYTES = 26 * 1024 * 1024
MAX_FETCH_ALLOWED_HOSTS = 256
MAX_FETCH_ALLOWED_HOST_CHARS = 253
_LOCKED_CSP_NONE_DIRECTIVES = (
    "default-src",
    "frame-ancestors",
    "base-uri",
    "form-action",
)


def normalize_fetch_allowed_host(raw_host: str) -> str:
    """Canonicalize one bare fetch allow-list host without accepting URL syntax."""

    if not isinstance(raw_host, str):
        raise ValueError("fetch allow-list entries must be strings")
    host = raw_host.strip().casefold()
    if (
        not host
        or len(host) > MAX_FETCH_ALLOWED_HOST_CHARS
        or "%" in host
        or any(ord(char) < 0x21 or ord(char) == 0x7F for char in host)
    ):
        raise ValueError("fetch allow-list entries must be bounded bare hosts")

    address_candidate = host
    if host.startswith("[") and host.endswith("]"):
        address_candidate = host[1:-1]
    elif "[" in host or "]" in host:
        raise ValueError("fetch allow-list entries contain malformed IP brackets")
    try:
        return ip_address(address_candidate).compressed.casefold()
    except ValueError:
        pass

    if any(marker in host for marker in (":", "/", "\\", "@", "?", "#")):
        raise ValueError("fetch allow-list entries must contain a host only")
    if host.endswith("."):
        host = host[:-1]
    if not host or host.endswith("."):
        raise ValueError("fetch allow-list entries contain an invalid hostname")
    try:
        host = host.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ValueError("fetch allow-list entries contain an invalid hostname") from exc
    if len(host) > MAX_FETCH_ALLOWED_HOST_CHARS:
        raise ValueError("fetch allow-list entries exceed the hostname length limit")
    labels = host.split(".")
    allowed_chars = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
    if any(
        not label
        or len(label) > 63
        or label[0] == "-"
        or label[-1] == "-"
        or any(char not in allowed_chars for char in label)
        for label in labels
    ):
        raise ValueError("fetch allow-list entries contain an invalid hostname")
    return host


def _has_locked_csp_minimum(policy: str) -> bool:
    directives: dict[str, tuple[str, ...]] = {}
    for raw_directive in policy.split(";"):
        parts = raw_directive.split()
        if not parts:
            continue
        name = parts[0].casefold()
        if name in directives:
            # Browsers use the first duplicate directive; rejecting duplicates
            # avoids validators and clients disagreeing over which value governs.
            return False
        directives[name] = tuple(source.casefold() for source in parts[1:])
    return all(directives.get(name) == ("'none'",) for name in _LOCKED_CSP_NONE_DIRECTIVES)


def _database_tls_query_options(uri: str) -> list[tuple[str, str]]:
    """Return TLS query options without collapsing duplicate/conflicting keys."""

    try:
        query = urlsplit(uri).query
    except ValueError:
        return []
    return [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key in {"ssl", "sslmode"}
    ]


def _database_tls_query_is_unambiguous(uri: str) -> bool:
    try:
        query_options = parse_qsl(urlsplit(uri).query, keep_blank_values=True)
    except ValueError:
        return False
    tls_option_names = [name for name, _value in query_options if name.startswith("ssl")]
    mode_option_names = [name for name in tls_option_names if name in {"ssl", "sslmode"}]
    return len(tls_option_names) == len(set(tls_option_names)) and len(set(mode_option_names)) <= 1


def _redis_uri_has_authenticated_tls(uri: str) -> bool:
    """Reject Redis URL controls that weaken or ambiguously configure TLS."""

    if (
        not uri
        or uri != uri.strip()
        or len(uri) > 16_384
        or "\\" in uri
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in uri)
    ):
        return False
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "rediss"
        or not parsed.hostname
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        return False

    seen_tls_options: set[str] = set()
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        if name.startswith("ssl_"):
            if name in seen_tls_options:
                return False
            seen_tls_options.add(name)
        if name == "ssl_cert_reqs" and value != "required":
            return False
        if name == "ssl_check_hostname" and value.strip().casefold() not in {
            "1",
            "on",
            "t",
            "true",
            "y",
            "yes",
        }:
            return False
    return True


def _package_version() -> str:
    try:
        return pkg_version("apex-orchestration-engine")
    except PackageNotFoundError:
        return "0.0.0+local"


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    uri: str = DEFAULT_DATABASE_URI
    # ORM metadata and Alembic migrations intentionally target one fixed schema.
    # Reject misleading overrides until dynamic-schema support is end-to-end.
    schema_name: Literal["apex"] = "apex"
    pool_size: int = Field(default=5, ge=1, le=100)
    max_overflow: int = Field(default=10, ge=0, le=200)
    pool_recycle_s: int = Field(default=1800, ge=30, le=86_400)
    ssl_mode: str | None = None

    @model_validator(mode="after")
    def validate_tls_configuration(self) -> "DatabaseSettings":
        options = _database_tls_query_options(self.uri)
        if not _database_tls_query_is_unambiguous(self.uri):
            raise ValueError("database URI must contain one unambiguous TLS option")
        if self.ssl_mode is not None and options:
            raise ValueError(
                "database.ssl_mode and URI TLS query options must not both be configured"
            )
        return self


class AuthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = True
    # Local-dev shortcut: a key resolving to a synthetic unscoped admin without DB access.
    dev_api_key: str | None = None
    # Server-held secret used to HMAC API keys at rest. Required in locked environments.
    api_key_hash_pepper: str | None = None
    # Ordered fallback peppers used only during rotation. Successful auth is
    # rehashed with the current pepper, so old values can be removed after use.
    previous_api_key_hash_peppers: list[str] = Field(default_factory=list)


class RateLimitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = True
    backend: Literal["local", "redis"] = "local"
    requests: int = Field(default=600, ge=1, le=1_000_000)
    window_s: int = Field(default=60, ge=1, le=86_400)
    max_buckets: int = Field(default=10_000, ge=1, le=1_000_000)
    auth_failures: int = Field(default=10, ge=1, le=10_000)
    auth_failure_window_s: int = Field(default=300, ge=1, le=86_400)
    auth_lockout_s: int = Field(default=300, ge=1, le=604_800)
    # Long-lived SSE connections consume a worker/proxy slot for their entire
    # lifetime, so entry-rate limiting alone is insufficient.
    sse_global_concurrency: int = Field(default=128, ge=1, le=10_000)
    sse_source_concurrency: int = Field(default=16, ge=1, le=1_000)
    sse_credential_concurrency: int = Field(default=8, ge=1, le=1_000)
    # Redis SSE permits are short leases renewed while the response is alive,
    # so a crashed pod cannot leak capacity indefinitely.
    sse_lease_ttl_s: int = Field(default=30, ge=5, le=300)
    run_create_requests: int = Field(default=10, ge=1, le=10_000)
    run_create_window_s: int = Field(default=60, ge=1, le=86_400)
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

    @field_validator("protected_path_prefixes")
    @classmethod
    def validate_protected_path_prefixes(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            prefix = value.strip()
            if not prefix or not prefix.startswith("/"):
                raise ValueError("protected path prefixes must be non-empty absolute paths")
            if len(prefix) > 2048:
                raise ValueError("protected path prefixes must not exceed 2048 characters")
            if prefix not in normalized:
                normalized.append(prefix)
        if not normalized:
            raise ValueError("at least one protected path prefix is required")
        return normalized


class SecurityHeadersSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = True
    content_security_policy: str = Field(
        default=("default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
        max_length=8_192,
    )
    hsts_max_age_s: int = 31536000
    hsts_include_subdomains: bool = True

    @field_validator("content_security_policy")
    @classmethod
    def validate_content_security_policy_header(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("content_security_policy must be a single safe HTTP header value")
        return value.strip()


class RequestBodySettings(BaseModel):
    """Streaming HTTP request-body limits applied before routing or auth."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    max_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=MAX_HTTP_REQUEST_BODY_BYTES)
    document_upload_max_bytes: int = Field(
        default=MAX_HTTP_REQUEST_BODY_BYTES,
        ge=1024,
        le=MAX_HTTP_REQUEST_BODY_BYTES,
    )
    timeout_s: float = Field(default=120.0, gt=0, le=600.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_upload_limit(self) -> "RequestBodySettings":
        if self.document_upload_max_bytes < self.max_bytes:
            raise ValueError("document_upload_max_bytes must be at least max_bytes")
        return self


class LLMSettings(BaseModel):
    """Anthropic LLM agent config (env: APEX_LLM__*).

    `anthropic_api_key` doubles as the opt-in gate: a run that requests the
    `anthropic` agent backend falls back to the deterministic stub when no key
    is present, so the offline test suite (which sets neither) stays stub-only.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

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
    fetch_allowed_hosts: list[str] = Field(default_factory=list, max_length=MAX_FETCH_ALLOWED_HOSTS)
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
        normalized_fetch_hosts: list[str] = []
        seen_fetch_hosts: set[str] = set()
        for raw_host in self.fetch_allowed_hosts:
            host = normalize_fetch_allowed_host(raw_host)
            if host in seen_fetch_hosts:
                raise ValueError("llm.fetch_allowed_hosts must not contain duplicates")
            normalized_fetch_hosts.append(host)
            seen_fetch_hosts.add(host)
        self.fetch_allowed_hosts = normalized_fetch_hosts
        return self


class RunControlSettings(BaseModel):
    """Deployment-owned request and prompt budgets (env: APEX_RUNS__*)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

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

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    max_extract_chars: int = Field(default=200_000, ge=1_000, le=2_000_000)
    max_context_chars_per_doc: int = Field(
        default=50_000,
        ge=1_000,
        le=MAX_CONTEXT_TEXT_CHARS,
    )
    max_context_chars_total: int = Field(default=150_000, ge=1_000, le=2_000_000)
    summary_chars: int = Field(default=280, ge=32, le=MAX_CONTEXT_SUMMARY_CHARS)


class ApexSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APEX_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Settings validation can fail before structured logging is installed.
        # Never let Pydantic render the complete environment-derived input in
        # an unhandled startup traceback (database URIs and provider keys live
        # in that input mapping).
        hide_input_in_errors=True,
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
    langgraph_cors_config: dict[str, object] | None = Field(
        default=None,
        validation_alias="CORS_CONFIG",
    )
    redis_uri: str | None = Field(default=None, validation_alias="REDIS_URI")
    auth: AuthSettings = AuthSettings()
    rate_limit: RateLimitSettings = RateLimitSettings()
    security_headers: SecurityHeadersSettings = SecurityHeadersSettings()
    request_body: RequestBodySettings = RequestBodySettings()
    llm: LLMSettings = LLMSettings()
    documents: DocumentIngestionSettings = DocumentIngestionSettings()
    runs: RunControlSettings = RunControlSettings()

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, values: list[str]) -> list[str]:
        """Normalize exact web origins and reject values browsers never emit."""

        normalized: list[str] = []
        for raw_value in values:
            origin = raw_value.strip()
            if not origin or origin == "*":
                raise ValueError("cors_origins entries must be explicit non-wildcard origins")
            try:
                parsed = urlsplit(origin)
                # Accessing ``port`` also validates malformed/non-numeric ports.
                parsed_port = parsed.port
            except ValueError as exc:
                raise ValueError(f"invalid CORS origin {origin!r}") from exc
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "cors_origins entries must contain only an http(s) scheme, host, "
                    "and optional port"
                )
            scheme = parsed.scheme.lower()
            hostname = parsed.hostname.lower()
            host = f"[{hostname}]" if ":" in hostname else hostname
            port = parsed_port
            if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
                host = f"{host}:{port}"
            canonical = urlunsplit((scheme, host, "", "", ""))
            if canonical in normalized:
                raise ValueError("cors_origins must not contain duplicates")
            normalized.append(canonical)
        return normalized

    @field_validator("langgraph_database_uri")
    @classmethod
    def validate_langgraph_database_tls_query(cls, uri: str | None) -> str | None:
        if uri is not None and not _database_tls_query_is_unambiguous(uri):
            raise ValueError("DATABASE_URI must contain one unambiguous TLS option")
        return uri

    @property
    def is_locked_down(self) -> bool:
        """True for production/staging-class environments (no dev affordances)."""
        return self.environment.strip().lower() not in UNLOCKED_ENVIRONMENTS

    @model_validator(mode="after")
    def validate_production_lockdown(self) -> "ApexSettings":
        env = self.environment.strip().lower()
        errors: list[str] = []
        normalized_origins = self.cors_origins
        if self.distributed_remote_creation_lock:
            try:
                database_scheme = urlsplit(self.database.uri).scheme.lower()
            except ValueError:
                database_scheme = ""
            if not database_scheme.startswith("postgresql"):
                errors.append("distributed_remote_creation_lock requires a PostgreSQL database URI")
        if self.rate_limit.backend == "redis" and not self.redis_uri:
            errors.append("rate_limit.backend='redis' requires REDIS_URI")
        if env in UNLOCKED_ENVIRONMENTS:
            if errors:
                raise ValueError(f"Unsafe configuration: {'; '.join(errors)}")
            return self

        runtime_cors = self.langgraph_cors_config
        if runtime_cors is None:
            errors.append("CORS_CONFIG is required in locked environments")
        else:
            runtime_origins = runtime_cors.get("allow_origins")
            if runtime_origins != self.cors_origins:
                errors.append("CORS_CONFIG.allow_origins must exactly match cors_origins")
            if runtime_cors.get("allow_origin_regex"):
                errors.append("CORS_CONFIG.allow_origin_regex is forbidden in locked environments")
            runtime_headers = runtime_cors.get("allow_headers")
            if not isinstance(runtime_headers, list) or any(
                not isinstance(header, str) for header in runtime_headers
            ):
                errors.append("CORS_CONFIG.allow_headers must be a list of strings")
            else:
                required_headers = {
                    "authorization",
                    "content-type",
                    "idempotency-key",
                    "last-event-id",
                    "x-api-key",
                    "x-request-id",
                }
                normalized_headers = {header.strip().lower() for header in runtime_headers}
                if "*" in normalized_headers:
                    errors.append("CORS_CONFIG.allow_headers must not contain '*' with credentials")
                elif not required_headers.issubset(normalized_headers):
                    errors.append(
                        "CORS_CONFIG.allow_headers must cover authenticated API and SSE headers"
                    )
            runtime_methods = runtime_cors.get("allow_methods")
            if not isinstance(runtime_methods, list) or any(
                not isinstance(method, str) for method in runtime_methods
            ):
                errors.append("CORS_CONFIG.allow_methods must be a list of strings")
            elif not {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}.issubset(
                {method.strip().upper() for method in runtime_methods}
            ):
                errors.append("CORS_CONFIG.allow_methods does not cover the APEX API")
            if runtime_cors.get("allow_credentials") is not True:
                errors.append("CORS_CONFIG.allow_credentials must be true")

        if not self.auth.enabled:
            errors.append("auth.enabled=false is allowed only in local/test environments")
        if self.auth.dev_api_key:
            errors.append("auth.dev_api_key is allowed only in local/test environments")
        current_pepper = self.auth.api_key_hash_pepper
        previous_peppers = self.auth.previous_api_key_hash_peppers
        if not current_pepper:
            errors.append("auth.api_key_hash_pepper is required in locked environments")
        elif len(current_pepper.encode("utf-8")) < MIN_API_KEY_PEPPER_BYTES:
            errors.append(
                "auth.api_key_hash_pepper must be at least "
                f"{MIN_API_KEY_PEPPER_BYTES} bytes in locked environments"
            )
        if len(previous_peppers) > MAX_PREVIOUS_API_KEY_PEPPERS:
            errors.append(
                "auth.previous_api_key_hash_peppers must contain at most "
                f"{MAX_PREVIOUS_API_KEY_PEPPERS} entries"
            )
        if any(
            len(previous.encode("utf-8")) < MIN_API_KEY_PEPPER_BYTES
            for previous in previous_peppers
        ):
            errors.append(
                "every auth.previous_api_key_hash_peppers entry must be at least "
                f"{MIN_API_KEY_PEPPER_BYTES} bytes in locked environments"
            )
        if len(set(previous_peppers)) != len(previous_peppers):
            errors.append("auth.previous_api_key_hash_peppers must not contain duplicates")
        if current_pepper and current_pepper in previous_peppers:
            errors.append("auth.previous_api_key_hash_peppers must not contain the current pepper")
        if _is_local_database_uri(self.database.uri):
            errors.append("database.uri must not point at localhost/default credentials")
        if not _database_uri_authenticates_server(
            self.database.uri,
            self.database.ssl_mode,
            allow_asyncpg_ssl_true=True,
        ):
            errors.append(
                "database.uri must authenticate the TLS server with sslmode=verify-full "
                "in locked environments"
            )
        if self.langgraph_database_uri is not None and not _database_uri_authenticates_server(
            self.langgraph_database_uri,
            None,
            allow_asyncpg_ssl_true=False,
        ):
            errors.append(
                "DATABASE_URI must authenticate the TLS server with sslmode=verify-full "
                "in locked environments"
            )
        if not self.redis_uri:
            errors.append("REDIS_URI is required for distributed limits in locked environments")
        elif not _redis_uri_has_authenticated_tls(self.redis_uri):
            errors.append(
                "REDIS_URI must use unambiguous rediss:// certificate and hostname "
                "verification in locked environments"
            )
        if not self.security_headers.enabled:
            errors.append(
                "security_headers.enabled=false is allowed only in local/test environments"
            )
        if not _has_locked_csp_minimum(self.security_headers.content_security_policy):
            errors.append(
                "security_headers.content_security_policy must enforce default-src, "
                "frame-ancestors, base-uri, and form-action as 'none'"
            )
        if self.security_headers.hsts_max_age_s <= 0:
            errors.append("security_headers.hsts_max_age_s must be > 0 in locked environments")
        if self.allow_private_adapter_hosts:
            errors.append(
                "allow_private_adapter_hosts=true is allowed only in local/test environments; "
                "approve individual bootstrap connections instead"
            )
        if self.llm.fetch_allow_private_hosts:
            errors.append(
                "llm.fetch_allow_private_hosts=true is allowed only in local/test environments"
            )
        if not self.rate_limit.enabled:
            errors.append("rate_limit.enabled=false is allowed only in local/test environments")
        if self.rate_limit.backend != "redis":
            errors.append("rate_limit.backend='redis' is required in locked environments")
        required_rate_prefixes = {
            "/v1/",
            "/threads",
            "/runs",
            "/assistants",
            "/crons",
            "/store",
        }
        if not required_rate_prefixes.issubset(self.rate_limit.protected_path_prefixes):
            errors.append(
                "rate_limit.protected_path_prefixes must cover /v1/, /threads, /runs, "
                "/assistants, /crons, and /store in locked environments"
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


def _database_uri_authenticates_server(
    uri: str,
    ssl_mode: str | None,
    *,
    allow_asyncpg_ssl_true: bool,
) -> bool:
    """Require hostname-authenticated TLS, not merely encrypted transport."""

    if not _database_tls_query_is_unambiguous(uri):
        return False
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"} or not parsed.hostname:
        return False
    mode = _database_ssl_mode(uri, ssl_mode).strip().lower()
    if mode == DATABASE_AUTHENTICATED_SSL_MODE:
        return True
    if not allow_asyncpg_ssl_true or parsed.scheme != "postgresql+asyncpg" or ssl_mode is not None:
        return False
    options = _database_tls_query_options(uri)
    return (
        len(options) == 1
        and options[0][0] == "ssl"
        and options[0][1].strip().lower()
        in {
            "1",
            "on",
            "true",
            "yes",
        }
    )


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
        if mode == DATABASE_AUTHENTICATED_SSL_MODE:
            # asyncpg's string mode searches only PGSSLROOTCERT and
            # ~/.postgresql/root.crt. An explicit default context retains
            # certificate + hostname verification with system public CAs.
            return {"ssl": ssl.create_default_context()}
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
