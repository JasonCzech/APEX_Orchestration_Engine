"""Bounded diagnostics at provider and durable-runtime trust boundaries."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from apex.adapters.ado.work_tracking import _error_text as ado_error_text
from apex.adapters.apex_load.engine import _error_text as apex_load_error_text
from apex.adapters.elk.log_search import _error_reason as elk_error_reason
from apex.adapters.jira.work_tracking import _error_text as jira_error_text
from apex.adapters.k8s.cluster_inventory import _status_message as k8s_status_message
from apex.adapters.loadrunner.engine import _error_text as loadrunner_error_text
from apex.domain.diagnostics import (
    MAX_DIAGNOSTIC_CHARS,
    bounded_diagnostic,
    contains_credential_material,
    is_credential_field,
)


@pytest.mark.parametrize(
    ("extractor", "payload"),
    [
        (apex_load_error_text, {"error": "{detail}"}),
        (loadrunner_error_text, {"ExceptionMessage": "{detail}"}),
        (elk_error_reason, {"error": {"reason": "{detail}"}}),
        (k8s_status_message, {"message": "{detail}"}),
        (ado_error_text, {"message": "{detail}"}),
        (jira_error_text, {"errorMessages": ["{detail}"]}),
    ],
)
def test_provider_error_extractors_are_nul_safe_and_bounded(
    extractor: Callable[[httpx.Response], str],
    payload: dict[str, Any],
) -> None:
    detail = "x\x00" * (MAX_DIAGNOSTIC_CHARS + 100)

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("{detail}", detail)
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        return value

    response = httpx.Response(400, json=expand(payload))
    rendered = extractor(response)

    assert len(rendered) == MAX_DIAGNOSTIC_CHARS
    assert "\x00" not in rendered
    assert "\\0" in rendered


def test_bounded_diagnostic_survives_broken_string_conversion() -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("broken")

    assert bounded_diagnostic(Unprintable()) == "<Unprintable diagnostic unavailable>"


@pytest.mark.parametrize(
    "value",
    [
        {"nested": {"privateKey": "secret-canary"}},
        {"messages": ["Authorization: Bearer secret-canary"]},
        {"prompt": "database_uri=postgres://secret-canary"},
        {"url": "https://user:secret-canary@example.test/path"},
    ],
)
def test_contains_credential_material_detects_nested_durable_secrets(value: Any) -> None:
    assert contains_credential_material(value) is True


def test_contains_credential_material_does_not_overmatch_safe_runtime_contract() -> None:
    assert (
        contains_credential_material(
            {
                "assistant_id": "pipeline",
                "connections": {"execution_engine": "connection-1"},
                "tokenCount": 42,
                "pre_execution_context": ["exercise the authenticated user flow"],
            }
        )
        is False
    )


@pytest.mark.parametrize(
    "field",
    [
        "password",
        "database_password",
        "apiKey",
        "auth",
        "authHeader",
        "Authorization",
        "awsSecretAccessKey",
        "x-amz-signature",
        "cookie",
        "set-cookie",
        "passphrase",
        "privateKey",
        "ssh_key",
        "signing-key",
        "encryption_key",
        "connectionString",
        "database_uri",
        "postgresqlUrl",
        "redis_url",
        "broker-uri",
        "mongodb_uri",
        "dsn",
        "stripeApiKey",
        "serviceAccountPrivateKey",
        "databasePassword",
        "oauthRefreshToken",
        "sessionCookie",
        "browserCookieJar",
        "pat",
        "jiraPat",
        "bearer",
        "jwt",
        "psk",
        "sharedKey",
        "account_key",
        "storageKey",
        "subscription_key",
        "sessionId",
        "clientCertificate",
        "privatePem",
        "pkcs12",
    ],
)
def test_credential_field_names_are_detected(field: str) -> None:
    assert is_credential_field(field) is True


@pytest.mark.parametrize(
    "field",
    [
        "authorship",
        "auth_mode",
        "authenticationType",
        "tokenCount",
        "signatureAlgorithm",
        "publicKey",
        "access_key_id",
        "aws_access_key_id",
        "project_key",
        "monkey",
    ],
)
def test_noncredential_field_name_is_not_overmatched(field: str) -> None:
    assert is_credential_field(field) is False


@pytest.mark.parametrize(
    ("diagnostic", "secrets"),
    [
        ('provider failed: {"password":"hunter2", "message":"retry"}', ["hunter2"]),
        ("token=session-secret; retry later", ["session-secret"]),
        ("Authorization: Bearer bearer-secret", ["bearer-secret"]),
        ("Basic dXNlcjpwYXNzd29yZA== rejected", ["dXNlcjpwYXNzd29yZA=="]),
        ("GET https://user:pass@example.com/path failed", ["user", "pass"]),
        (
            "GET https://example.com/report?X-Amz-Signature=signed-secret&part=1 failed",
            ["signed-secret"],
        ),
        ("stripeApiKey=camel-api-secret; retry later", ["camel-api-secret"]),
        (
            '{"serviceAccountPrivateKey":"camel-private-secret","safe":"ok"}',
            ["camel-private-secret"],
        ),
        (
            '{"detail":"stripeApiKey=nested-camel-secret","safe":"ok"}',
            ["nested-camel-secret"],
        ),
        (
            "jira_pat=provider-pat-canary; jwt=provider-jwt-canary; "
            "shared_key=provider-shared-key-canary",
            [
                "provider-pat-canary",
                "provider-jwt-canary",
                "provider-shared-key-canary",
            ],
        ),
    ],
)
def test_bounded_diagnostic_redacts_common_credential_shapes(
    diagnostic: str, secrets: list[str]
) -> None:
    rendered = bounded_diagnostic(diagnostic)

    assert "[REDACTED]" in rendered
    assert all(secret not in rendered for secret in secrets)


@pytest.mark.parametrize(
    ("extractor", "payload"),
    [
        (apex_load_error_text, {"error": "{detail}"}),
        (loadrunner_error_text, {"ExceptionMessage": "{detail}"}),
        (elk_error_reason, {"error": {"reason": "{detail}"}}),
        (k8s_status_message, {"message": "{detail}"}),
        (ado_error_text, {"message": "{detail}"}),
        (jira_error_text, {"errorMessages": ["{detail}"]}),
    ],
)
def test_provider_error_extractors_redact_echoed_credentials(
    extractor: Callable[[httpx.Response], str], payload: dict[str, Any]
) -> None:
    detail = (
        "upstream password=provider-password; Authorization: Bearer provider-token; "
        "GET https://user:pass@example.com/a?sig=signed-secret&part=1 failed"
    )

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("{detail}", detail)
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        return value

    rendered = extractor(httpx.Response(400, json=expand(payload)))

    assert "[REDACTED]" in rendered
    for secret in (
        "provider-password",
        "provider-token",
        "user:pass",
        "signed-secret",
    ):
        assert secret not in rendered
