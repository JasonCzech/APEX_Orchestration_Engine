"""Hermetic tests for the bootstrap document schema, file loader, and CLI wiring.

apply_document's DB behavior is exercised by the Postgres-gated
tests/integration/test_bootstrap_apply.py; here we cover everything that needs
no database.
"""

import builtins
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from apex.adapters.registry import PortKind
from apex.bootstrap import __main__ as bootstrap_main
from apex.bootstrap.__main__ import MAX_BOOTSTRAP_BYTES, _load_document, main
from apex.bootstrap.runner import (
    BootstrapError,
    BootstrapReport,
    _admin_drift_fields,
    _environment_drift_fields,
    _reconcile_known_connection_alias,
    apply_document,
    safe_bootstrap_diagnostic,
)
from apex.bootstrap.schema import (
    AdminConsumerSpec,
    ApplicationSpec,
    BootstrapDocument,
    ConnectionSpec,
    EnvironmentSpec,
)
from apex.domain.input_limits import MAX_CHILD_ITEMS
from apex.persistence.models import (
    ApiConsumer,
    Connection,
    ConsumerKey,
    ConsumerScope,
    Environment,
    EnvironmentHost,
)

VALID_DOC: dict[str, Any] = {
    "connections": [
        {
            "name": "minio-artifacts",
            "kind": "artifact_store",
            "provider": "s3",
            "options": {
                "endpoint": "localhost:9000",
                "_apex_trusted_private_host": True,
            },
            "secret_ref": "env:APEX_INTEGRATION_MINIO_SECRET_KEY",
        }
    ],
    "admin": {"name": "apex-admin", "key_env": "APEX_BOOTSTRAP_ADMIN_KEY"},
}


def test_bootstrap_diagnostics_are_single_line_bounded_and_credential_safe() -> None:
    canary = "bootstrap-diagnostic-secret-canary"
    rendered = safe_bootstrap_diagnostic(
        f"connection password=\n{canary}\r\x1b[31m\u2028forged" + ("x" * 20_000)
    )

    assert canary not in rendered
    assert "[REDACTED]" in rendered
    assert len(rendered) <= 4_096
    assert not any(character in rendered for character in ("\n", "\r", "\x1b", "\u2028"))


def test_bootstrap_error_sanitizes_caller_controlled_diagnostics_at_construction() -> None:
    canary = "bootstrap-error-secret-canary"

    rendered = str(BootstrapError(f"admin api_token={canary}\nforged-error"))

    assert canary not in rendered
    assert "\n" not in rendered
    assert "[REDACTED]" in rendered
    assert "\\n" in str(BootstrapError("ordinary\nforged-error"))


async def test_apply_document_sanitizes_progress_log_values() -> None:
    canary = "bootstrap-progress-secret-canary"
    # Bypass input validation deliberately to exercise the final logging sink's
    # defense against a corrupt/internally forged model instance.
    document = BootstrapDocument.model_construct(
        applications=[
            ApplicationSpec.model_construct(
                project_id="project-a",
                name=f"release-password={canary}\nforged-progress",
            )
        ]
    )
    messages: list[str] = []

    class Session:
        async def scalar(self, _statement: object) -> None:
            return None

        def add(self, _row: object) -> None:
            return None

        async def flush(self) -> None:
            return None

    await apply_document(document, Session(), env={}, log=messages.append)  # type: ignore[arg-type]

    assert len(messages) == 1
    assert canary not in messages[0]
    assert "\n" not in messages[0]
    assert "[REDACTED]" in messages[0]


# ── schema validation ─────────────────────────────────────────────────────────


def test_secret_ref_must_be_a_reference_not_a_literal() -> None:
    with pytest.raises(ValidationError, match="supported env:NAME"):
        ConnectionSpec(
            name="c",
            kind=PortKind.ARTIFACT_STORE,
            provider="s3",
            secret_ref="super-secret-value",
        )


def test_secret_ref_reference_and_none_are_accepted() -> None:
    ref = ConnectionSpec(name="c", kind=PortKind.SECRETS, provider="env", secret_ref="env:FOO")
    assert ref.secret_ref == "env:FOO"
    none = ConnectionSpec(name="c", kind=PortKind.SECRETS, provider="env", secret_ref=None)
    assert none.secret_ref is None


@pytest.mark.parametrize(
    "secret_ref",
    [
        "raw:anything",
        "http://vault/secret",
        "vault:path/to/key",
        "file:/run/secrets/key",
        "env:",
        "env:NAME-WITH-DASH",
    ],
)
def test_bootstrap_secret_ref_requires_a_runtime_supported_scheme(secret_ref: str) -> None:
    with pytest.raises(ValueError, match="supported env:NAME"):
        ConnectionSpec(name="c", kind=PortKind.SECRETS, provider="env", secret_ref=secret_ref)


@pytest.mark.parametrize(
    "options",
    [
        {"password": "literal"},
        {"nested": {"api_token": "literal"}},
        {"items": [{"clientSecret": "literal"}]},
        {"connection_string": "postgresql://user:password@example.test/db"},
        {"nested": {"database_uri": "postgresql://user:password@example.test/db"}},
        {"items": [{"dsn": "host=db password=literal"}]},
        {"ssh_key": "literal"},
        {"signing_key": "literal"},
        {"encryption_key": "literal"},
        {"sas": "literal"},
        {"authentication": "literal"},
        {"header": "Authorization: Bearer raw-auth-canary"},
        {"ordinary": "postgresql://user:raw-uri-canary@example.test/db"},
        {"items": ["Authorization: Bearer nested-auth-canary"]},
        {"value": "ghp_0123456789abcdefghijklmnopqrstuvwxyz"},
        {"data": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhcGV4LXVzZXIifQ.c2lnbmF0dXJlLWNhbmFyeQ"},
        {
            "value": "-----BEGIN PRIVATE KEY-----\n"
            "cHJpdmF0ZS1rZXktY2FuYXJ5\n"
            "-----END PRIVATE KEY-----"
        },
    ],
)
def test_bootstrap_rejects_raw_secrets_in_connection_options(
    options: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="secret_ref"):
        ConnectionSpec(
            name="c",
            kind=PortKind.ARTIFACT_STORE,
            provider="s3",
            options=options,
        )


def test_bootstrap_rejects_raw_secrets_in_environment_options() -> None:
    with pytest.raises(ValueError, match="external connection secret_ref"):
        EnvironmentSpec.model_validate(
            {
                "project_id": "project-a",
                "application": "app-a",
                "name": "prod",
                "options": {"nested": {"broker_uri": "amqp://user:raw-secret@broker"}},
            }
        )


@pytest.mark.parametrize(
    "credential",
    [
        "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhcGV4LXVzZXIifQ.c2lnbmF0dXJlLWNhbmFyeQ",
        "-----BEGIN PRIVATE KEY-----\ncHJpdmF0ZS1rZXktY2FuYXJ5\n-----END PRIVATE KEY-----",
    ],
)
def test_bootstrap_rejects_standalone_credential_signatures_in_environment_options(
    credential: str,
) -> None:
    with pytest.raises(ValueError, match="external connection secret_ref"):
        EnvironmentSpec.model_validate(
            {
                "project_id": "project-a",
                "application": "app-a",
                "name": "prod",
                "options": {"value": credential},
            }
        )


@pytest.mark.parametrize(
    "document",
    [
        {
            "applications": [
                {
                    "project_id": "project-a",
                    "name": "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
                }
            ]
        },
        {
            "applications": [
                {
                    "project_id": "project-a",
                    "name": "safe",
                    "description": "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
                }
            ]
        },
        {
            "environments": [
                {
                    "project_id": "project-a",
                    "application": "app-a",
                    "name": "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
                }
            ]
        },
        {
            "environments": [
                {
                    "project_id": "project-a",
                    "application": "app-a",
                    "name": "prod",
                    "hosts": [
                        {
                            "hostname": "api.example.test",
                            "role": "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
                        }
                    ],
                }
            ]
        },
        {
            "connections": [
                {
                    "name": "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
                    "kind": "artifact_store",
                    "provider": "s3",
                }
            ]
        },
        {"admin": {"key_env": "ghp_0123456789abcdefghijklmnopqrstuvwxyz"}},
    ],
)
def test_bootstrap_rejects_credentials_in_all_scalar_label_models(
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="credential material"):
        BootstrapDocument.model_validate(document)


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BootstrapDocument.model_validate({"applications": [], "bogus": 1})


def test_compose_bootstrap_document_matches_the_secret_free_schema() -> None:
    path = Path(__file__).resolve().parents[2] / "deploy" / "bootstrap" / "compose.json"

    document = BootstrapDocument.model_validate(_load_document(str(path)))

    assert document.connections[0].options["endpoint"] == "minio:9000"
    assert document.connections[0].secret_ref == "env:APEX_INTEGRATION_MINIO_SECRET_KEY"
    assert document.admin is not None
    assert document.admin.key_env == "APEX_BOOTSTRAP_ADMIN_KEY"


def test_bootstrap_loader_rejects_oversized_document(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.json"
    path.write_bytes(b" " * (MAX_BOOTSTRAP_BYTES + 1))

    with pytest.raises(BootstrapError, match="byte limit"):
        _load_document(str(path))


def test_bootstrap_loader_rejects_deep_json_before_decoder_recursion(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.json"
    path.write_text("[" * 40 + "]" * 40)

    with pytest.raises(BootstrapError, match="maximum depth"):
        _load_document(str(path))


def test_bootstrap_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.json"
    path.write_text('{"admin": null, "admin": {}}')

    with pytest.raises(BootstrapError, match="duplicate key"):
        _load_document(str(path))


def test_bootstrap_loader_rejects_yaml_aliases_and_duplicate_keys(tmp_path: Path) -> None:
    alias_path = tmp_path / "alias.yaml"
    alias_path.write_text("options: &options {key: value}\ncopy: *options\n")
    with pytest.raises(BootstrapError, match="aliases"):
        _load_document(str(alias_path))

    duplicate_path = tmp_path / "duplicate.yaml"
    duplicate_path.write_text("admin: null\nadmin: {}\n")
    with pytest.raises(BootstrapError, match="duplicate key"):
        _load_document(str(duplicate_path))


def test_bootstrap_loader_detaches_missing_yaml_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bootstrap.yaml"
    path.write_text("admin: null\n")
    canary = "missing-yaml-dependency-canary"
    import_error = ModuleNotFoundError(canary)
    real_import = builtins.__import__

    def fail_yaml_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "yaml":
            raise import_error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_yaml_import)

    with pytest.raises(BootstrapError, match="PyYAML") as caught:
        _load_document(str(path))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert canary not in str(caught.value)


def test_admin_defaults() -> None:
    spec = AdminConsumerSpec()
    assert (spec.name, spec.role.value, spec.consumer_type.value, spec.key_env) == (
        "apex-admin",
        "admin",
        "internal",
        "APEX_BOOTSTRAP_ADMIN_KEY",
    )


@pytest.mark.parametrize("key_env", ["BAD-NAME", "1STARTS_WITH_NUMBER", "LINE\nBREAK"])
def test_admin_key_env_must_be_a_real_environment_variable_name(key_env: str) -> None:
    with pytest.raises(ValueError, match="environment-variable"):
        AdminConsumerSpec(key_env=key_env)


def test_existing_bootstrap_admin_must_match_authority_and_only_active_key() -> None:
    expected_hash = "a" * 64
    spec = AdminConsumerSpec.model_validate({"scopes": [{"project_id": "project-a"}]})
    consumer = ApiConsumer(
        name=spec.name,
        key_hash=expected_hash,
        consumer_type=spec.consumer_type.value,
        role=spec.role.value,
        enabled=True,
        scopes=[ConsumerScope(project_id="project-a", app_id=None)],
        keys=[ConsumerKey(key_hash=expected_hash, expiry_source="independent")],
    )

    assert _admin_drift_fields(consumer, spec, (expected_hash,)) == []

    consumer.role = "viewer"
    assert "role" in _admin_drift_fields(consumer, spec, (expected_hash,))
    consumer.role = spec.role.value
    consumer.keys.append(ConsumerKey(key_hash="b" * 64, expiry_source="independent"))
    assert "active_keys" in _admin_drift_fields(consumer, spec, (expected_hash,))


def test_existing_bootstrap_admin_drift_never_reports_key_material() -> None:
    expected_hash = "a" * 64
    supplied_hash = "b" * 64
    spec = AdminConsumerSpec()
    consumer = ApiConsumer(
        name=spec.name,
        key_hash=expected_hash,
        consumer_type="headless",
        role="viewer",
        enabled=False,
        scopes=[],
        keys=[ConsumerKey(key_hash=expected_hash, expiry_source="independent")],
    )

    rendered = repr(_admin_drift_fields(consumer, spec, (supplied_hash,)))

    assert expected_hash not in rendered
    assert supplied_hash not in rendered


@pytest.mark.parametrize(
    "document",
    [
        {"applications": [{"project_id": "p" * 256, "name": "app"}]},
        {"applications": [{"project_id": "p", "name": "a" * 256}]},
        {
            "environments": [
                {"project_id": "p", "application": "app", "name": "e", "kind": "k" * 65}
            ]
        },
        {
            "environments": [
                {
                    "project_id": "p",
                    "application": "app",
                    "name": "e",
                    "base_url": "x" * 1025,
                }
            ]
        },
        {
            "environments": [
                {
                    "project_id": "p",
                    "application": "app",
                    "name": "e",
                    "hosts": [{"hostname": "h" * 1025}],
                }
            ]
        },
        {"admin": {"name": "a" * 256}},
        {
            "connections": [
                {
                    "name": "c",
                    "kind": "artifact_store",
                    "provider": "p" * 65,
                }
            ]
        },
    ],
)
def test_bootstrap_rejects_values_that_exceed_database_columns(
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        BootstrapDocument.model_validate(document)


@pytest.mark.parametrize(
    "document",
    [
        {
            "applications": [
                {"project_id": "p", "name": f"app-{index}"} for index in range(MAX_CHILD_ITEMS + 1)
            ]
        },
        {
            "environments": [
                {"project_id": "p", "application": "app", "name": f"env-{index}"}
                for index in range(MAX_CHILD_ITEMS + 1)
            ]
        },
        {
            "connections": [
                {
                    "name": f"connection-{index}",
                    "kind": "artifact_store",
                    "provider": "s3",
                }
                for index in range(MAX_CHILD_ITEMS + 1)
            ]
        },
        {
            "admin": {
                "scopes": [
                    {"project_id": f"project-{index}"} for index in range(MAX_CHILD_ITEMS + 1)
                ]
            }
        },
    ],
)
def test_bootstrap_rejects_unbounded_child_collections(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="at most 256 items"):
        BootstrapDocument.model_validate(document)


def test_bootstrap_environment_options_must_be_finite_bounded_json() -> None:
    base = {"project_id": "p", "application": "app", "name": "env"}
    with pytest.raises(ValidationError, match="must be finite"):
        BootstrapDocument.model_validate(
            {"environments": [base | {"options": {"threshold": float("nan")}}]}
        )


@pytest.mark.parametrize(
    "document",
    [
        {"applications": [{"project_id": "p", "name": "app\x00name"}]},
        {
            "applications": [
                {"project_id": "p", "name": "app", "description": "unsafe\x00description"}
            ]
        },
        {
            "environments": [
                {"project_id": "p", "application": "app\x00name", "name": "environment"}
            ]
        },
        {
            "environments": [
                {
                    "project_id": "p",
                    "application": "app",
                    "name": "environment",
                    "hosts": [{"hostname": "host", "role": "db\x00primary"}],
                }
            ]
        },
        {
            "connections": [
                {
                    "name": "connection\x00name",
                    "kind": "artifact_store",
                    "provider": "s3",
                }
            ]
        },
        {
            "connections": [
                {
                    "name": "connection",
                    "kind": "artifact_store",
                    "provider": "s3",
                    "options": {"endpoint": "minio\x00:9000"},
                }
            ]
        },
        {"admin": {"name": "admin\x00name"}},
        {"admin": {"key_env": "APEX\x00ADMIN_KEY"}},
    ],
)
def test_bootstrap_rejects_nul_before_postgres_persistence(
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        BootstrapDocument.model_validate(document)


def test_bootstrap_admin_rejects_duplicate_and_redundant_scopes() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        AdminConsumerSpec.model_validate({"scopes": [{"project_id": "p"}, {"project_id": "p"}]})
    with pytest.raises(ValidationError, match="redundant"):
        AdminConsumerSpec.model_validate(
            {"scopes": [{"project_id": "p"}, {"project_id": "p", "app_id": "a"}]}
        )


@pytest.mark.parametrize(
    "document",
    [
        {
            "applications": [
                {"project_id": "p", "name": "app"},
                {"project_id": "p", "name": "app"},
            ]
        },
        {
            "environments": [
                {"project_id": "p", "application": "app", "name": "prod"},
                {"project_id": "p", "application": "app", "name": "prod"},
            ]
        },
        {
            "connections": [
                {"name": "c", "kind": "artifact_store", "provider": "s3"},
                {"name": "c", "kind": "artifact_store", "provider": "s3"},
            ]
        },
    ],
)
def test_bootstrap_rejects_duplicate_natural_keys(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="duplicate natural keys"):
        BootstrapDocument.model_validate(document)


def test_bootstrap_rejects_duplicate_environment_hosts() -> None:
    with pytest.raises(ValidationError, match="duplicate hostname/role"):
        EnvironmentSpec.model_validate(
            {
                "project_id": "p",
                "application": "app",
                "name": "prod",
                "hosts": [
                    {"hostname": "api.internal", "role": "app"},
                    {"hostname": "api.internal", "role": "app"},
                ],
            }
        )


def test_existing_bootstrap_environment_must_match_complete_aggregate() -> None:
    spec = EnvironmentSpec.model_validate(
        {
            "project_id": "p",
            "application": "app",
            "name": "prod",
            "kind": "k8s",
            "base_url": "https://api.example.com",
            "options": {"region": "east"},
            "hosts": [{"hostname": "api.internal", "role": "app"}],
        }
    )
    environment = Environment(
        name="prod",
        kind="k8s",
        base_url="https://api.example.com",
        options={"region": "east"},
        hosts=[EnvironmentHost(hostname="api.internal", role="app")],
    )

    assert _environment_drift_fields(environment, spec) == []

    environment.base_url = "https://attacker.invalid"
    environment.hosts.append(EnvironmentHost(hostname="unexpected.internal", role="db"))
    assert _environment_drift_fields(environment, spec) == ["base_url", "hosts"]


def test_report_summary_is_human_readable() -> None:
    report = BootstrapReport(connections_created=["minio-artifacts"], admin_created="apex-admin")
    assert "connections +1" in report.summary()
    assert "admin=created" in report.summary()


@pytest.mark.parametrize("namespace", ["apex", "perf", "customer-a"])
def test_bootstrap_reconciles_previous_minio_service_alias_in_any_namespace(
    namespace: str,
) -> None:
    connection = Connection(
        id="c1",
        name="minio-artifacts",
        kind="artifact_store",
        provider="s3",
        options={
            "endpoint": f"apex-minio.{namespace}.svc.cluster.local:9000",
            "bucket": "b",
        },
    )
    expected: dict[str, object] = {"options": {"endpoint": "apex-minio:9000", "bucket": "b"}}

    assert _reconcile_known_connection_alias(connection, expected, ["options"]) is True
    assert connection.options["endpoint"] == "apex-minio:9000"


@pytest.mark.parametrize(
    "endpoint",
    [
        "attacker.invalid:9000",
        "apex-minio.bad.namespace.svc.cluster.local:9000",
        "apex-minio.-invalid.svc.cluster.local:9000",
        "other-service.apex.svc.cluster.local:9000",
    ],
)
def test_bootstrap_rejects_unrelated_minio_endpoint_drift(endpoint: str) -> None:
    connection = Connection(
        id="c1",
        name="minio-artifacts",
        kind="artifact_store",
        provider="s3",
        options={"endpoint": endpoint, "bucket": "b"},
    )
    expected: dict[str, object] = {"options": {"endpoint": "apex-minio:9000", "bucket": "b"}}

    assert _reconcile_known_connection_alias(connection, expected, ["options"]) is False


# ── file loader ───────────────────────────────────────────────────────────────


def test_load_json_document(tmp_path: Path) -> None:
    path = tmp_path / "boot.json"
    path.write_text(json.dumps(VALID_DOC))
    assert _load_document(str(path)) == VALID_DOC


def test_load_yaml_document(tmp_path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    path = tmp_path / "boot.yaml"
    path.write_text(yaml.safe_dump(VALID_DOC))
    assert _load_document(str(path)) == VALID_DOC


def test_load_non_mapping_is_bootstrap_error(tmp_path: Path) -> None:
    path = tmp_path / "boot.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(BootstrapError, match="must be a mapping"):
        _load_document(str(path))


# ── CLI wiring (DB is unreachable under the hermetic fixture) ──────────────────


def test_main_rejects_invalid_document_without_logging_raw_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = "CANARY-super-secret-never-log"
    path = tmp_path / "boot.json"
    path.write_text(
        json.dumps(
            {
                "connections": [
                    {
                        "name": "x",
                        "kind": "artifact_store",
                        "provider": "s3",
                        "options": {"password": canary},
                        "secret_ref": "raw",
                    }
                ]
            }
        )
    )
    assert main([str(path)]) == 2  # validation failure, before any DB access
    captured = capsys.readouterr()
    assert canary not in captured.err
    assert "input_value" not in captured.err


def test_main_does_not_echo_malformed_json_snippets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = "CANARY-malformed-secret-never-log"
    path = tmp_path / "boot.json"
    path.write_text('{"admin": "' + canary)

    assert main([str(path)]) == 2
    assert canary not in capsys.readouterr().err

    with pytest.raises(BootstrapError) as raised:
        _load_document(str(path))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_main_requires_a_file() -> None:
    with pytest.raises(SystemExit):
        main([])  # argparse error -> SystemExit(2)


async def test_run_rejects_unauthenticated_remote_database_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap_main,
        "get_settings",
        lambda: SimpleNamespace(
            database=SimpleNamespace(
                uri="postgresql+asyncpg://u:p@db.example/apex?sslmode=require",
                ssl_mode=None,
            )
        ),
    )

    with pytest.raises(BootstrapError, match="authenticate every remote server"):
        await bootstrap_main._run(BootstrapDocument())


def test_main_graceful_returns_zero_when_db_unreachable(tmp_path: Path) -> None:
    path = tmp_path / "boot.json"
    path.write_text(json.dumps(VALID_DOC))
    # Hermetic fixture points APEX_DATABASE__URI at an unreachable host.
    assert main([str(path), "--graceful"]) == 0


def test_main_rejects_graceful_mode_in_locked_environments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "boot.json"
    path.write_text(json.dumps(VALID_DOC))
    monkeypatch.setattr(
        "apex.bootstrap.__main__.get_settings",
        lambda: SimpleNamespace(is_locked_down=True),
    )

    assert main([str(path), "--graceful"]) == 2
    assert "only in local/test environments" in capsys.readouterr().err


def test_main_strict_returns_nonzero_when_db_unreachable(tmp_path: Path) -> None:
    path = tmp_path / "boot.json"
    path.write_text(json.dumps(VALID_DOC))
    assert main([str(path)]) == 1
