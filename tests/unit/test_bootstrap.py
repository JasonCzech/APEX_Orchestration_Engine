"""Hermetic tests for the bootstrap document schema, file loader, and CLI wiring.

apply_document's DB behavior is exercised by the Postgres-gated
tests/integration/test_bootstrap_apply.py; here we cover everything that needs
no database.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from apex.adapters.registry import PortKind
from apex.bootstrap.__main__ import _load_document, main
from apex.bootstrap.runner import BootstrapError, BootstrapReport
from apex.bootstrap.schema import AdminConsumerSpec, BootstrapDocument, ConnectionSpec

VALID_DOC: dict[str, Any] = {
    "connections": [
        {
            "name": "minio-artifacts",
            "kind": "artifact_store",
            "provider": "s3",
            "options": {"endpoint": "localhost:9000"},
            "secret_ref": "env:APEX_INTEGRATION_MINIO_SECRET_KEY",
        }
    ],
    "admin": {"name": "apex-admin", "key_env": "APEX_BOOTSTRAP_ADMIN_KEY"},
}


# ── schema validation ─────────────────────────────────────────────────────────


def test_secret_ref_must_be_a_reference_not_a_literal() -> None:
    with pytest.raises(ValidationError, match="must be a reference"):
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


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BootstrapDocument.model_validate({"applications": [], "bogus": 1})


def test_admin_defaults() -> None:
    spec = AdminConsumerSpec()
    assert (spec.name, spec.role.value, spec.consumer_type.value, spec.key_env) == (
        "apex-admin",
        "admin",
        "internal",
        "APEX_BOOTSTRAP_ADMIN_KEY",
    )


def test_report_summary_is_human_readable() -> None:
    report = BootstrapReport(connections_created=["minio-artifacts"], admin_created="apex-admin")
    assert "connections +1" in report.summary()
    assert "admin=created" in report.summary()


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


def test_main_rejects_invalid_document(tmp_path: Path) -> None:
    path = tmp_path / "boot.json"
    path.write_text(
        json.dumps(
            {
                "connections": [
                    {"name": "x", "kind": "artifact_store", "provider": "s3", "secret_ref": "raw"}
                ]
            }
        )
    )
    assert main([str(path)]) == 2  # validation failure, before any DB access


def test_main_requires_a_file() -> None:
    with pytest.raises(SystemExit):
        main([])  # argparse error -> SystemExit(2)


def test_main_graceful_returns_zero_when_db_unreachable(tmp_path: Path) -> None:
    path = tmp_path / "boot.json"
    path.write_text(json.dumps(VALID_DOC))
    # Hermetic fixture points APEX_DATABASE__URI at an unreachable host.
    assert main([str(path), "--graceful"]) == 0


def test_main_strict_returns_nonzero_when_db_unreachable(tmp_path: Path) -> None:
    path = tmp_path / "boot.json"
    path.write_text(json.dumps(VALID_DOC))
    assert main([str(path)]) == 1
