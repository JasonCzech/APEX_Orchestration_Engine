"""Bounded diagnostics at provider and durable-runtime trust boundaries."""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import pytest
from langgraph.types import Command, Send
from pydantic import RootModel

from apex.adapters.ado.work_tracking import _error_text as ado_error_text
from apex.adapters.apex_load.engine import _error_text as apex_load_error_text
from apex.adapters.elk.log_search import _error_reason as elk_error_reason
from apex.adapters.jira.work_tracking import _error_text as jira_error_text
from apex.adapters.k8s.cluster_inventory import _status_message as k8s_status_message
from apex.adapters.loadrunner.engine import _error_text as loadrunner_error_text
from apex.domain.diagnostics import (
    MAX_CREDENTIAL_SCAN_KEY_CHARS,
    MAX_CREDENTIAL_SCAN_STRING_CHARS,
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


def test_contains_credential_material_inspects_langgraph_command_dataclass() -> None:
    command = Command(
        update={"phase": "execution"},
        resume={"interrupt-1": {"instructions": "password=command-secret-canary"}},
    )

    assert contains_credential_material(command) is True


def test_contains_credential_material_inspects_langgraph_send_slots() -> None:
    command = Command(
        goto=Send(
            "agent",
            {"request": {"Authorization": "Bearer send-secret-canary"}},
        )
    )

    assert contains_credential_material(command) is True


def test_contains_credential_material_handles_cyclic_dataclasses() -> None:
    @dataclass(slots=True)
    class DurableNode:
        detail: str
        child: Any = None

    node = DurableNode(detail="ordinary durable state")
    node.child = node

    assert contains_credential_material(node) is False


def test_contains_credential_material_fails_closed_for_unreadable_dataclass_field() -> None:
    @dataclass
    class UnreadableDurableValue:
        detail: str

        def __getattribute__(self, name: str) -> Any:
            if name == "detail":
                raise RuntimeError("field unavailable")
            return super().__getattribute__(name)

    assert contains_credential_material(UnreadableDurableValue("ordinary")) is True


def test_contains_credential_material_does_not_overmatch_safe_langgraph_command() -> None:
    command = Command(
        update={"phase": "execution"},
        resume={"interrupt-1": {"action": "approve"}},
        goto=Send("agent", {"project_key": "project-1"}),
    )

    assert contains_credential_material(command) is False


def test_contains_credential_material_fails_closed_when_mapping_iteration_raises() -> None:
    class BrokenMapping(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(("ordinary",))

        def __len__(self) -> int:
            return 1

        def items(self) -> Any:
            yield ("ordinary", "safe")
            raise RuntimeError("mapping iteration unavailable")

    assert contains_credential_material(BrokenMapping()) is True


def test_contains_credential_material_never_stringifies_hostile_mapping_keys() -> None:
    class HostileKey:
        called = False

        def __str__(self) -> str:
            self.called = True
            raise AssertionError("mapping key conversion must not run")

    key = HostileKey()

    assert contains_credential_material({key: "safe"}) is True
    assert key.called is False


def test_contains_credential_material_fails_closed_when_sequence_iteration_raises() -> None:
    class BrokenList(list[Any]):
        def __iter__(self) -> Iterator[Any]:
            raise RuntimeError("sequence unavailable")

    assert contains_credential_material(BrokenList(["ordinary"])) is True


def test_contains_credential_material_accepts_pinned_langgraph_uuid_controls() -> None:
    assert (
        contains_credential_material(
            {
                "assistant_id": uuid4(),
                "thread_id": uuid4(),
                "run_id": uuid4(),
            }
        )
        is False
    )


def test_contains_credential_material_bounds_mapping_iteration_lazily() -> None:
    class HugeMapping(Mapping[str, Any]):
        yielded = 0

        def __getitem__(self, key: str) -> Any:
            return "ordinary"

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("items() must be consumed directly")

        def __len__(self) -> int:
            return 1_000_000_000

        def items(self) -> Any:
            index = 0
            while True:
                self.yielded += 1
                if self.yielded > 16:
                    raise AssertionError("credential scan eagerly consumed the mapping")
                yield f"ordinary_{index}", "safe"
                index += 1

    value = HugeMapping()

    assert contains_credential_material(value, max_nodes=8) is True
    assert value.yielded <= 8


def test_contains_credential_material_bounds_sequence_iteration_lazily() -> None:
    class HugeList(list[Any]):
        yielded = 0

        def __iter__(self) -> Iterator[Any]:
            while True:
                self.yielded += 1
                if self.yielded > 16:
                    raise AssertionError("credential scan eagerly consumed the sequence")
                yield "safe"

    value = HugeList()

    assert contains_credential_material(value, max_nodes=8) is True
    assert value.yielded <= 8


def test_contains_credential_material_bounds_pydantic_root_lazily() -> None:
    class HugeList(list[Any]):
        yielded = 0

        def __iter__(self) -> Iterator[Any]:
            while True:
                self.yielded += 1
                if self.yielded > 16:
                    raise AssertionError("model root was eagerly materialized")
                yield "safe"

    root = HugeList()
    model = RootModel[list[str]].model_construct(root=root)

    assert contains_credential_material(model, max_nodes=8) is True
    assert root.yielded <= 8


def test_contains_credential_material_never_invokes_unknown_model_dump() -> None:
    class HugeModelOutput:
        called = False

        def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
            self.called = True
            return {"ordinary": ["safe"] * 1_000_000}

    value = HugeModelOutput()

    assert contains_credential_material(value) is True
    assert value.called is False


def test_contains_credential_material_inspects_plain_instance_state() -> None:
    class Carrier:
        def __init__(self, **values: Any) -> None:
            self.__dict__.update(values)

    assert contains_credential_material(Carrier(ordinary="safe")) is False
    assert contains_credential_material(Carrier(password="plain-object-secret")) is True


def test_contains_credential_material_fails_closed_for_hostile_instance_state() -> None:
    class HostileCarrier:
        def __getattribute__(self, name: str) -> Any:
            if name == "__dict__":
                raise RuntimeError("state unavailable")
            return super().__getattribute__(name)

    assert contains_credential_material(HostileCarrier()) is True


def test_contains_credential_material_fails_closed_for_malformed_slots_metadata() -> None:
    class Carrier:
        __slots__ = ("value",)

        def __init__(self) -> None:
            self.value = "safe"

    carrier = Carrier()
    Carrier.__slots__ = ([],)  # type: ignore[misc,assignment]

    assert contains_credential_material(carrier) is True


def test_contains_credential_material_rejects_oversized_scalar_before_regex_scan() -> None:
    value = "x" * (MAX_CREDENTIAL_SCAN_STRING_CHARS + 1)

    assert contains_credential_material(value) is True


def test_contains_credential_material_rejects_oversized_key_before_regex_scan() -> None:
    value = {"x" * (MAX_CREDENTIAL_SCAN_KEY_CHARS + 1): "safe"}

    assert contains_credential_material(value) is True


def test_contains_credential_material_enforces_total_character_budget() -> None:
    assert (
        contains_credential_material(
            ["12345678", "abcdefgh"],
            max_string_chars=8,
            max_total_chars=15,
        )
        is True
    )
    assert (
        contains_credential_material(
            ["12345678", "abcdefgh"],
            max_string_chars=8,
            max_total_chars=16,
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
        "secretValue",
        "secretKey",
        "secretKeyValue",
        "clientSecretKey",
        "apiKeyValue",
        "tokenValue",
        "passwordValue",
        "connectionStringValue",
        "secretString",
        "secretBinary",
        "privateKeyData",
        "credentialText",
        "passwordHash",
        "authentication",
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
        "authenticationModeValue",
        "authenticationModeData",
        "metadata",
        "tokenCount",
        "tokenCountValue",
        "signatureAlgorithm",
        "signatureAlgorithmValue",
        "publicKey",
        "secretKeyRef",
        "secretKeyIdentifier",
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
    "credential",
    [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhcGV4LXVzZXIifQ.c2lnbmF0dXJlLWNhbmFyeQ",
        "-----BEGIN PRIVATE KEY-----\ncHJpdmF0ZS1rZXktY2FuYXJ5\n-----END PRIVATE KEY-----",
        "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        "github_pat_0123456789abcdefghijklmnopqrstuv",
        "glpat-0123456789abcdefghijklmnopqrstuv",
        "xoxb-0123456789-abcdefghijklmnopqrstuv",
        "sk_live_0123456789abcdefghijklmnop",
        "sk-proj-0123456789abcdefghijklmnopqrstuv",
        "npm_0123456789abcdefghijklmnopqrstuv",
        "pypi-0123456789abcdefghijklmnopqrstuv",
        "hf_0123456789abcdefghijklmnopqrstuv",
        "AIza0123456789abcdefghijklmnopqrstuvwxy",
        "ya29.0123456789abcdefghijklmnopqrstuvwxyz",
        "SG.0123456789abcdefghijkl.0123456789abcdefghijklmnopqrstuvwxyzABCDEFG",
        "dckr_pat_0123456789abcdefghijklmnopqrstuv",
        "dckr_oat_0123456789abcdefghijklmnopqrstuv",
        "0123456789abcd.atlasv1.0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWX_=",
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnAZDOwxyz",
    ],
)
def test_standalone_high_confidence_credential_signatures_are_redacted(
    credential: str,
) -> None:
    diagnostic = f"provider returned {credential} before retry"

    rendered = bounded_diagnostic(diagnostic)

    assert rendered == "provider returned [REDACTED] before retry"
    assert credential not in rendered
    assert contains_credential_material({"value": credential}) is True


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
