import json
import ssl
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from apex.settings import (
    ApexSettings,
    AuthSettings,
    DatabaseSettings,
    DocumentIngestionSettings,
    LLMSettings,
    RateLimitSettings,
    SecurityHeadersSettings,
    database_ssl_connect_args,
)


class CleanEnvSettings(ApexSettings):
    """ApexSettings without .env loading, for deterministic tests."""

    model_config = SettingsConfigDict(
        env_prefix="APEX_",
        env_nested_delimiter="__",
        env_file=None,
        extra="ignore",
    )


def _cors_config(origin: str = "https://dashboard.example.com") -> str:
    return json.dumps(
        {
            "allow_origins": [origin],
            "allow_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            "allow_headers": [
                "authorization",
                "content-type",
                "idempotency-key",
                "last-event-id",
                "x-api-key",
                "x-request-id",
            ],
            "allow_credentials": True,
        }
    )


def test_nested_settings_reject_unknown_environment_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_RATE_LIMIT__ENABELD", "false")

    with pytest.raises(ValidationError, match="enabeld"):
        CleanEnvSettings()


def test_llm_fetch_allowed_hosts_are_canonicalized() -> None:
    settings = LLMSettings(
        fetch_allowed_hosts=[
            " Results.Example.COM. ",
            "[2001:0DB8:0:0:0:0:0:1]",
        ]
    )

    assert settings.fetch_allowed_hosts == ["results.example.com", "2001:db8::1"]


@pytest.mark.parametrize(
    "host",
    [
        "",
        "https://results.example.com",
        "results.example.com:443",
        "results.example.com/path",
        "user@results.example.com",
        "bad_host.example.com",
        "-bad.example.com",
        "a" * 254,
    ],
)
def test_llm_fetch_allowed_hosts_reject_non_host_values(host: str) -> None:
    with pytest.raises(ValidationError, match="fetch_allowed_hosts|fetch allow-list"):
        LLMSettings(fetch_allowed_hosts=[host])


def test_llm_fetch_allowed_hosts_reject_duplicates_after_normalization() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        LLMSettings(fetch_allowed_hosts=["RESULTS.example.com", "results.example.com."])


def test_llm_fetch_allowed_hosts_are_count_bounded() -> None:
    hosts = [f"results-{index}.example.com" for index in range(257)]

    with pytest.raises(ValidationError):
        LLMSettings(fetch_allowed_hosts=hosts)


@pytest.mark.parametrize(
    "uri",
    [
        "postgresql+asyncpg://u:p@db:5432/x?sslmode=require&sslmode=disable",
        "postgresql+asyncpg://u:p@db:5432/x?sslmode=require&ssl=false",
    ],
)
def test_database_settings_reject_ambiguous_tls_query_options(
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
) -> None:
    monkeypatch.setenv("APEX_DATABASE__URI", uri)

    with pytest.raises(ValidationError, match="unambiguous TLS option"):
        CleanEnvSettings()


def test_database_settings_reject_competing_tls_configuration_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "APEX_DATABASE__URI",
        "postgresql+asyncpg://u:p@db:5432/x?sslmode=require",
    )
    monkeypatch.setenv("APEX_DATABASE__SSL_MODE", "require")

    with pytest.raises(ValidationError, match="must not both be configured"):
        CleanEnvSettings()


def test_langgraph_unauthenticated_meta_routes_are_disabled() -> None:
    config_path = Path(__file__).resolve().parents[2] / "langgraph.json"
    config = json.loads(config_path.read_text())

    assert config["http"]["disable_meta"] is True
    assert config["http"]["disable_webhooks"] is True
    assert config["http"]["disable_store"] is True
    assert config["env"] == ".env"
    assert config["http"]["cors"]["allow_origins"] == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    assert config["http"]["cors"]["allow_credentials"] is True
    assert "last-event-id" in config["http"]["cors"]["allow_headers"]


def test_defaults() -> None:
    settings = CleanEnvSettings()
    assert settings.app_name == "APEX Orchestration Engine"
    assert settings.database.schema_name == "apex"
    assert settings.database.uri.startswith("postgresql+asyncpg://")
    assert settings.distributed_remote_creation_lock is False
    assert settings.env_secret_prefixes == ["APEX_INTEGRATION_"]


def test_database_settings_reject_unsupported_schema_name() -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings(schema_name="ignored_schema")  # type: ignore[arg-type]


def test_startup_validation_errors_do_not_render_secret_inputs() -> None:
    database_password = "sentinel-database-password"
    api_key_pepper = "sentinel-api-key-pepper-" + "p" * 32
    provider_key = "sentinel-anthropic-api-key"

    with pytest.raises(ValidationError) as raised:
        CleanEnvSettings(
            environment="production",
            database=DatabaseSettings(
                uri=(
                    f"postgresql+asyncpg://apex:{database_password}@db.example/apex?sslmode=require"
                ),
            ),
            auth=AuthSettings(api_key_hash_pepper=api_key_pepper),
            llm=LLMSettings(anthropic_api_key=provider_key),
        )

    rendered = str(raised.value)
    assert "input_value=" not in rendered
    assert "input_type=" not in rendered
    assert database_password not in rendered
    assert api_key_pepper not in rendered
    assert provider_key not in rendered


def test_custom_settings_diagnostics_do_not_reflect_environment_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cors_secret = "cors-origin-secret-canary"
    monkeypatch.setenv(
        "APEX_CORS_ORIGINS",
        json.dumps([f"https://user:{cors_secret}@dashboard.example.com:not-a-port"]),
    )
    with pytest.raises(ValidationError, match="invalid CORS origin") as cors_error:
        CleanEnvSettings()
    assert cors_secret not in str(cors_error.value)

    proxy_secret = "trusted-proxy-secret-canary"
    with pytest.raises(ValidationError, match="invalid trusted proxy CIDR") as proxy_error:
        RateLimitSettings(trusted_proxy_cidrs=[proxy_secret])
    assert proxy_secret not in str(proxy_error.value)

    environment_secret = "hostile-environment-secret-canary"
    monkeypatch.delenv("APEX_CORS_ORIGINS")
    with pytest.raises(ValidationError, match="Unsafe locked-environment") as env_error:
        CleanEnvSettings(environment=environment_secret)
    assert environment_secret not in str(env_error.value)


def test_nested_settings_validation_errors_hide_inputs_when_built_directly() -> None:
    secret = "direct-nested-settings-secret-canary"
    factories = (
        lambda: DatabaseSettings(uri=f"postgresql://user:{secret}@db.example/app?ssl=a&ssl=b"),
        lambda: LLMSettings(
            anthropic_api_key=secret,
            default_model="missing",
            allowed_models=["allowed"],
        ),
        lambda: AuthSettings(unexpected_secret=secret),  # type: ignore[call-arg]
    )

    for factory in factories:
        with pytest.raises(ValidationError) as raised:
            factory()
        rendered = str(raised.value)
        assert "input_value=" not in rendered
        assert "input_type=" not in rendered
        assert secret not in rendered


@pytest.mark.parametrize(
    "values",
    [
        {"summary_chars": 4_001},
        {"max_context_chars_per_doc": 150_001},
    ],
)
def test_document_settings_cannot_exceed_context_packet_contract(
    values: dict[str, int],
) -> None:
    with pytest.raises(ValidationError):
        DocumentIngestionSettings(**values)


def test_rate_limit_trusted_proxy_cidrs_are_validated() -> None:
    assert RateLimitSettings(trusted_proxy_cidrs=["10.40.0.0/20"]).trusted_proxy_cidrs == [
        "10.40.0.0/20"
    ]
    with pytest.raises(ValidationError, match="invalid trusted proxy CIDR"):
        RateLimitSettings(trusted_proxy_cidrs=["not-a-network"])


def test_rate_limit_prefixes_are_normalized_and_validated() -> None:
    settings = RateLimitSettings(protected_path_prefixes=[" /v1/ ", "/runs", "/runs"])
    assert settings.protected_path_prefixes == ["/v1/", "/runs"]
    with pytest.raises(ValidationError, match="absolute paths"):
        RateLimitSettings(protected_path_prefixes=["runs"])
    with pytest.raises(ValidationError, match="absolute paths"):
        RateLimitSettings(protected_path_prefixes=[" "])


def test_sse_concurrency_budgets_are_positive_and_bounded() -> None:
    settings = RateLimitSettings(
        sse_global_concurrency=64,
        sse_source_concurrency=8,
        sse_credential_concurrency=4,
        sse_lease_ttl_s=15,
    )
    assert settings.sse_global_concurrency == 64
    assert settings.sse_lease_ttl_s == 15
    with pytest.raises(ValidationError):
        RateLimitSettings(sse_global_concurrency=0)
    with pytest.raises(ValidationError):
        RateLimitSettings(sse_lease_ttl_s=4)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APEX_DATABASE__POOL_SIZE", "0"),
        ("APEX_RATE_LIMIT__REQUESTS", "0"),
        ("APEX_DOCUMENTS__MAX_EXTRACT_CHARS", "0"),
    ],
)
def test_operational_budgets_reject_nonpositive_values(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        CleanEnvSettings()


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "staging")
    monkeypatch.setenv(
        "APEX_DATABASE__URI",
        "postgresql+asyncpg://u:p@db:5432/x?sslmode=verify-full",
    )
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "p" * 32)
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("CORS_CONFIG", _cors_config())
    monkeypatch.setenv("APEX_DISTRIBUTED_REMOTE_CREATION_LOCK", "true")
    monkeypatch.setenv("REDIS_URI", "rediss://redis.example.com:6380/0")
    monkeypatch.setenv("APEX_RATE_LIMIT__BACKEND", "redis")
    monkeypatch.setenv(
        "DATABASE_URI",
        "postgresql://u:p@db:5432/x?sslmode=verify-full&sslrootcert=system",
    )
    settings = CleanEnvSettings()
    assert settings.environment == "staging"
    assert settings.distributed_remote_creation_lock is True
    assert settings.database.uri == "postgresql+asyncpg://u:p@db:5432/x?sslmode=verify-full"


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


def test_local_env_caps_previous_pepper_count() -> None:
    previous = [f"pepper-{index}" for index in range(17)]

    with pytest.raises(ValidationError, match="at most 16 entries"):
        AuthSettings(previous_api_key_hash_peppers=previous)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dev_api_key", "é" * 2_049),
        ("api_key_hash_pepper", "é" * 2_049),
        ("previous_api_key_hash_peppers", ["é" * 2_049]),
    ],
)
def test_local_env_caps_auth_secret_bytes(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match="byte limit"):
        AuthSettings.model_validate({field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dev_api_key", "\ud800"),
        ("api_key_hash_pepper", "\ud800"),
        ("previous_api_key_hash_peppers", ["\ud800"]),
    ],
)
def test_local_env_rejects_non_unicode_auth_secrets_without_exception_chain(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError, match="valid string") as exc_info:
        AuthSettings.model_validate({field: value})

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None


def test_local_env_rejects_empty_previous_pepper_item() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        AuthSettings(previous_api_key_hash_peppers=[""])


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


def test_cors_origins_are_normalized_for_exact_browser_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "APEX_CORS_ORIGINS",
        '[" HTTP://LOCALHOST:5173 ", "https://Dashboard.Example.com:443"]',
    )

    assert CleanEnvSettings().cors_origins == [
        "http://localhost:5173",
        "https://dashboard.example.com",
    ]


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "dashboard.example.com",
        "https://dashboard.example.com/",
        "https://dashboard.example.com/path",
        "https://dashboard.example.com?tenant=p1",
        "https://dashboard.example.com#fragment",
        "https://user:secret@dashboard.example.com",
        "https://dashboard.example.com:not-a-port",
        "ftp://dashboard.example.com",
    ],
)
def test_rejects_values_that_are_not_exact_web_origins(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    monkeypatch.setenv("APEX_CORS_ORIGINS", json.dumps([origin]))

    with pytest.raises(ValidationError, match="cors_origins|CORS origin"):
        CleanEnvSettings()


def test_rejects_duplicate_cors_origins_after_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "APEX_CORS_ORIGINS",
        '["https://DASHBOARD.example.com", " https://dashboard.example.com "]',
    )

    with pytest.raises(ValidationError, match="duplicates"):
        CleanEnvSettings()


def test_locked_down_env_rejects_insecure_cors_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["http://dashboard.example.com"]')
    with pytest.raises(ValidationError, match="cors_origins"):
        CleanEnvSettings()


def test_locked_down_env_accepts_authenticated_database_ssl_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("APEX_DATABASE__SSL_MODE", "verify-full")
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "p" * 32)
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("CORS_CONFIG", _cors_config())
    monkeypatch.setenv("APEX_DISTRIBUTED_REMOTE_CREATION_LOCK", "true")
    monkeypatch.setenv("REDIS_URI", "rediss://redis.example.com:6380/0")
    monkeypatch.setenv("APEX_RATE_LIMIT__BACKEND", "redis")
    monkeypatch.setenv(
        "DATABASE_URI",
        "postgresql://u:p@db:5432/x?sslmode=verify-full&sslrootcert=system",
    )

    settings = CleanEnvSettings()

    assert settings.database.ssl_mode == "verify-full"
    context = database_ssl_connect_args(settings.database.uri, settings.database.ssl_mode)["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_locked_down_env_accepts_asyncpg_ssl_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?ssl=true")
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "p" * 32)
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("CORS_CONFIG", _cors_config())
    monkeypatch.setenv("APEX_DISTRIBUTED_REMOTE_CREATION_LOCK", "true")
    monkeypatch.setenv("REDIS_URI", "rediss://redis.example.com:6380/0")
    monkeypatch.setenv("APEX_RATE_LIMIT__BACKEND", "redis")
    monkeypatch.setenv(
        "DATABASE_URI",
        "postgresql://u:p@db:5432/x?sslmode=verify-full&sslrootcert=system",
    )

    settings = CleanEnvSettings()

    assert settings.database.uri.endswith("?ssl=true")
    assert database_ssl_connect_args(settings.database.uri, settings.database.ssl_mode) == {
        "ssl": True
    }


@pytest.mark.parametrize("mode", ["require", "verify-ca"])
def test_locked_down_env_rejects_postgres_tls_without_hostname_authentication(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv(
        "APEX_DATABASE__URI",
        f"postgresql+asyncpg://u:p@db:5432/x?sslmode={mode}",
    )

    with pytest.raises(ValidationError, match="authenticate the TLS server"):
        CleanEnvSettings()


@pytest.mark.parametrize(
    "uri",
    [
        ("postgresql://u:p@db:5432/x?sslmode=verify-full&sslmode=disable&sslrootcert=system"),
        "postgresql://u:p@db:5432/x?sslmode=verify-full&ssl=false",
        (
            "postgresql://u:p@db:5432/x?sslmode=verify-full&"
            "sslrootcert=system&sslrootcert=%2Ftmp%2Fother-ca.pem"
        ),
    ],
)
def test_langgraph_database_uri_rejects_ambiguous_tls_controls(
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URI", uri)

    with pytest.raises(ValidationError, match="unambiguous TLS option"):
        CleanEnvSettings()


@pytest.mark.parametrize(
    "uri",
    [
        " postgresql://u:p@db:5432/x?sslmode=verify-full",
        "postgresql://u:p@db:5432\\x?sslmode=verify-full",
        "postgresql://u:p@db:5432/x?sslmode=verify-full\x1f",
        "postgresql://u:p@db:5432/x?sslmode=verify-full\x85",
        "postgresql://u:p@db:5432/x?sslmode=verify-full&pad=" + ("x" * 16_384),
    ],
)
def test_database_uris_reject_unsafe_raw_text_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
) -> None:
    with pytest.raises(ValidationError, match="bounded and free of unsafe characters"):
        DatabaseSettings(uri=uri)

    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URI", uri)
    with pytest.raises(ValidationError, match="DATABASE_URI must be bounded"):
        CleanEnvSettings()


def test_locked_down_langgraph_database_requires_hostname_authenticated_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv(
        "DATABASE_URI",
        "postgresql://u:p@db:5432/x?sslmode=require&sslrootcert=system",
    )

    with pytest.raises(ValidationError, match="DATABASE_URI must authenticate"):
        CleanEnvSettings()


def test_locked_down_langgraph_database_accepts_system_ca_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    uri = "postgresql://u:p@db:5432/x?sslmode=verify-full&sslrootcert=system"
    monkeypatch.setenv("DATABASE_URI", uri)

    assert CleanEnvSettings().langgraph_database_uri == uri


@pytest.mark.parametrize(
    "query",
    [
        "ssl_cert_reqs=none",
        "ssl_cert_reqs=optional",
        "ssl_check_hostname=false",
        "ssl_check_hostname=0",
        "ssl_cert_reqs=required&ssl_cert_reqs=none",
        "ssl_check_hostname=true&ssl_check_hostname=false",
        "ssl_ca_certs=%2Fca-one.pem&ssl_ca_certs=%2Fca-two.pem",
    ],
)
def test_locked_down_redis_rejects_disabled_or_ambiguous_tls_verification(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv("REDIS_URI", f"rediss://redis.example.com:6380/0?{query}")

    with pytest.raises(ValidationError, match="certificate and hostname verification"):
        CleanEnvSettings()


def test_locked_down_redis_accepts_explicit_verification_and_ca_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    uri = (
        "rediss://redis.example.com:6380/0?ssl_cert_reqs=required&"
        "ssl_check_hostname=true&ssl_ca_certs=%2Fetc%2Fssl%2Fcustom-ca.pem&"
        "ssl_certfile=%2Fetc%2Fssl%2Fclient.pem&ssl_keyfile=%2Fetc%2Fssl%2Fclient.key"
    )
    monkeypatch.setenv("REDIS_URI", uri)

    assert CleanEnvSettings().redis_uri == uri


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

    with pytest.raises(ValidationError, match="authenticate the TLS server"):
        CleanEnvSettings()


def test_locked_down_env_rejects_missing_api_key_hash_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv("APEX_DATABASE__URI", "postgresql+asyncpg://u:p@db:5432/x?sslmode=require")
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')

    with pytest.raises(ValidationError, match="api_key_hash_pepper"):
        CleanEnvSettings()


def _set_valid_locked_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "APEX_DATABASE__URI",
        "postgresql+asyncpg://u:p@db:5432/x?sslmode=verify-full",
    )
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "p" * 32)
    monkeypatch.setenv("APEX_CORS_ORIGINS", '["https://dashboard.example.com"]')
    monkeypatch.setenv("CORS_CONFIG", _cors_config())
    monkeypatch.setenv("APEX_DISTRIBUTED_REMOTE_CREATION_LOCK", "true")
    monkeypatch.setenv("REDIS_URI", "rediss://redis.example.com:6380/0")
    monkeypatch.setenv("APEX_RATE_LIMIT__BACKEND", "redis")
    monkeypatch.setenv(
        "DATABASE_URI",
        "postgresql://u:p@db:5432/x?sslmode=verify-full&sslrootcert=system",
    )


def test_locked_down_environment_requires_langgraph_database_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.delenv("DATABASE_URI")

    with pytest.raises(ValidationError, match="DATABASE_URI is required"):
        CleanEnvSettings()


@pytest.mark.parametrize(
    "config",
    [
        None,
        _cors_config("https://other.example.com"),
        json.dumps(
            {
                "allow_origins": ["https://dashboard.example.com"],
                "allow_methods": ["GET", "POST"],
                "allow_headers": ["x-api-key"],
                "allow_credentials": False,
            }
        ),
        json.dumps(
            {
                "allow_origins": ["https://dashboard.example.com"],
                "allow_origin_regex": ".*",
                "allow_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                "allow_headers": [
                    "authorization",
                    "content-type",
                    "idempotency-key",
                    "last-event-id",
                    "x-api-key",
                    "x-request-id",
                ],
                "allow_credentials": True,
            }
        ),
    ],
)
def test_locked_down_env_rejects_missing_or_divergent_runtime_cors(
    monkeypatch: pytest.MonkeyPatch,
    config: str | None,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    if config is None:
        monkeypatch.delenv("CORS_CONFIG")
    else:
        monkeypatch.setenv("CORS_CONFIG", config)

    with pytest.raises(ValidationError, match="CORS_CONFIG"):
        CleanEnvSettings()


def test_locked_down_env_requires_redis_distributed_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.delenv("REDIS_URI")

    with pytest.raises(ValidationError, match="REDIS_URI is required"):
        CleanEnvSettings()

    monkeypatch.setenv("REDIS_URI", "rediss://redis.example.com:6380/0")
    monkeypatch.setenv("APEX_RATE_LIMIT__BACKEND", "local")
    with pytest.raises(ValidationError, match="backend='redis'"):
        CleanEnvSettings()


def test_locked_down_env_rejects_short_api_key_hash_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "short")

    with pytest.raises(ValidationError, match="at least 32 bytes"):
        CleanEnvSettings()


@pytest.mark.parametrize(
    ("previous", "message"),
    [
        (["q" * 31], "every auth.previous_api_key_hash_peppers entry"),
        (["q" * 32, "q" * 32], "must not contain duplicates"),
        (["p" * 32], "must not contain the current pepper"),
    ],
)
def test_locked_down_env_rejects_unsafe_previous_peppers(
    monkeypatch: pytest.MonkeyPatch,
    previous: list[str],
    message: str,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv("APEX_AUTH__PREVIOUS_API_KEY_HASH_PEPPERS", json.dumps(previous))

    with pytest.raises(ValidationError, match=message):
        CleanEnvSettings()


def test_locked_down_env_caps_previous_peppers(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_locked_environment(monkeypatch)
    previous = [f"{index:02d}" + "q" * 30 for index in range(17)]
    monkeypatch.setenv("APEX_AUTH__PREVIOUS_API_KEY_HASH_PEPPERS", json.dumps(previous))

    with pytest.raises(ValidationError, match="at most 16 entries"):
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


@pytest.mark.parametrize(
    "policy",
    [
        "",
        "default-src *; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        (
            "default-src *; default-src 'none'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'"
        ),
        (
            "default-src 'none'; frame-ancestors https://example.com; "
            "base-uri 'none'; form-action 'none'"
        ),
    ],
)
def test_locked_down_env_requires_deny_by_default_csp(
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv("APEX_SECURITY_HEADERS__CONTENT_SECURITY_POLICY", policy)

    with pytest.raises(ValidationError, match="content_security_policy must enforce"):
        CleanEnvSettings()


def test_security_header_settings_reject_csp_header_injection() -> None:
    with pytest.raises(ValidationError, match="safe HTTP header"):
        SecurityHeadersSettings(content_security_policy="default-src 'none'\r\nx-forged: yes")


def test_locked_down_env_requires_rate_limit_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv("APEX_RATE_LIMIT__PROTECTED_PATH_PREFIXES", '["/v1/"]')

    with pytest.raises(ValidationError, match="protected_path_prefixes"):
        CleanEnvSettings()


def test_locked_down_env_requires_all_builtin_rate_limit_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv(
        "APEX_RATE_LIMIT__PROTECTED_PATH_PREFIXES",
        '["/v1/", "/threads", "/runs"]',
    )

    with pytest.raises(ValidationError, match="/assistants"):
        CleanEnvSettings()


def test_locked_down_env_rejects_disabled_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv("APEX_RATE_LIMIT__ENABLED", "false")

    with pytest.raises(ValidationError, match="rate_limit.enabled"):
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


def test_locked_down_env_rejects_private_agent_fetch_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_locked_environment(monkeypatch)
    monkeypatch.setenv("APEX_LLM__FETCH_ALLOW_PRIVATE_HOSTS", "true")

    with pytest.raises(ValidationError, match="llm.fetch_allow_private_hosts"):
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
    verify_full_context = database_ssl_connect_args(
        "postgresql+asyncpg://u:p@db.example.com:5432/x?sslmode=verify-full",
        None,
    )["ssl"]
    assert isinstance(verify_full_context, ssl.SSLContext)
    assert verify_full_context.verify_mode is ssl.CERT_REQUIRED
    assert verify_full_context.check_hostname is True
