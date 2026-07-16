"""Boundary tests for settings parsers that protect startup transport policy."""

from importlib.metadata import PackageNotFoundError
from typing import Any, cast

import pytest
from pydantic import ValidationError

import apex.settings as settings


@pytest.mark.parametrize(
    "host",
    [cast(Any, 42), "[host.example", "host.example..", "\ud800.example"],
)
def test_fetch_allowlist_rejects_malformed_type_brackets_dots_and_idna(host: Any) -> None:
    with pytest.raises(ValueError, match="fetch allow-list"):
        settings.normalize_fetch_allowed_host(host)


def test_package_version_has_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(settings, "pkg_version", missing)
    assert settings._package_version() == "0.0.0+local"


def test_nested_budget_validators_reject_empty_oversized_and_inverted_values() -> None:
    with pytest.raises(ValidationError, match="2048"):
        settings.RateLimitSettings(protected_path_prefixes=["/" + "x" * 2_048])
    with pytest.raises(ValidationError, match="at least one"):
        settings.RateLimitSettings(protected_path_prefixes=[])
    with pytest.raises(ValidationError, match="at least max_bytes"):
        settings.RequestBodySettings(max_bytes=2_048, document_upload_max_bytes=1_024)
    with pytest.raises(ValidationError, match="allowed_models"):
        settings.LLMSettings(default_model="missing", allowed_models=[])
    with pytest.raises(ValidationError, match="duplicates"):
        settings.LLMSettings(default_model="same", allowed_models=["same", "same"])


@pytest.mark.parametrize(
    "uri",
    [
        " rediss://redis.example/0",
        "rediss://redis.example:bad/0",
        "redis://redis.example/0",
        "rediss:///0",
    ],
)
def test_redis_tls_parser_rejects_ambiguous_invalid_and_unauthenticated_uris(uri: str) -> None:
    assert settings._redis_uri_has_authenticated_tls(uri) is False


@pytest.mark.parametrize(
    ("uri", "ssl_mode", "allow_asyncpg", "expected"),
    [
        ("postgresql://[", None, False, False),
        ("postgresql://db.example:bad/app", None, False, False),
        ("http://db.example/app", "verify-full", False, False),
        ("postgresql:///app", "verify-full", False, False),
        (
            "postgresql+asyncpg://db.example/app?ssl=true",
            None,
            True,
            True,
        ),
        (
            "postgresql+asyncpg://db.example/app?ssl=true",
            None,
            False,
            False,
        ),
    ],
)
def test_database_server_authentication_parser_fails_closed(
    uri: str,
    ssl_mode: str | None,
    allow_asyncpg: bool,
    expected: bool,
) -> None:
    assert (
        settings._database_uri_authenticates_server(
            uri,
            ssl_mode,
            allow_asyncpg_ssl_true=allow_asyncpg,
        )
        is expected
    )


def test_database_transport_helpers_cover_invalid_local_and_explicit_modes() -> None:
    malformed = "postgresql://["
    assert settings._database_tls_query_options(malformed) == []
    assert settings._database_tls_query_is_unambiguous(malformed) is False
    assert settings._is_local_database_uri(malformed) is False
    assert settings.database_uses_ssl(malformed, None) is False
    assert settings.database_asyncpg_uri(malformed) == malformed
    assert settings._database_ssl_mode(malformed, None) == ""

    assert settings.database_uses_ssl("sqlite:///:memory:", None) is False
    assert settings._database_ssl_mode("sqlite:///:memory:", None) == ""
    assert settings.database_ssl_connect_args("postgresql+asyncpg://db.example/app", "require") == {
        "ssl": "require"
    }
    assert (
        settings._database_ssl_mode("postgresql+asyncpg://db.example/app?ssl=false", None)
        == "disable"
    )


@pytest.mark.parametrize(
    ("uri", "ssl_mode", "expected"),
    [
        ("postgresql+asyncpg://u:p@localhost/apex", None, True),
        ("sqlite+aiosqlite:///:memory:", None, True),
        ("postgresql+asyncpg://u:p@db.example/apex", None, True),
        ("postgresql+asyncpg://u:p@db.example/apex?ssl=true", None, True),
        ("postgresql+asyncpg://u:p@db.example/apex", "verify-full", True),
        ("postgresql+asyncpg://u:p@db.example/apex?sslmode=require", None, False),
        ("postgresql+asyncpg://u:p@db.example/apex?sslmode=verify-ca", None, False),
        ("postgresql+asyncpg://u:p@db.example/apex?ssl=false", None, False),
        ("postgresql+asyncpg://u:p@db.example:0/apex", None, False),
        ("http://u:p@db.example/apex", None, False),
        ("postgresql+asyncpg://[", None, False),
    ],
)
def test_database_safe_transport_matches_concrete_asyncpg_configuration(
    uri: str,
    ssl_mode: str | None,
    expected: bool,
) -> None:
    assert settings.database_uri_has_safe_transport(uri, ssl_mode) is expected
