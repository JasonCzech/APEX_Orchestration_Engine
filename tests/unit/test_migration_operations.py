"""Exercise every revision through Alembic's operation boundary.

The offline-SQL tests validate the complete upgrade chain with Alembic itself.
These tests complement them by executing each revision function in-process, so
both upgrade and downgrade code paths stay importable and their operation order
can be inspected without requiring a live PostgreSQL server.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

VERSIONS_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "apex" / "persistence" / "migrations" / "versions"
)
REVISION_MODULES = tuple(
    f"apex.persistence.migrations.versions.{path.stem}"
    for path in sorted(VERSIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.py"))
)


@dataclass(frozen=True)
class OperationCall:
    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class RecordingOperations:
    """Small Alembic operation facade that preserves constructed SQL objects."""

    def __init__(self, dialect: str = "postgresql") -> None:
        self.calls: list[OperationCall] = []
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))

    def get_bind(self) -> Any:
        return self._bind

    def f(self, name: str) -> str:
        return name

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append(OperationCall(name=name, args=args, kwargs=kwargs))

        return record


@pytest.mark.parametrize("module_name", REVISION_MODULES)
def test_revision_upgrade_and_downgrade_operations_are_executable(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> None:
    module = importlib.import_module(module_name)
    operations = RecordingOperations()
    monkeypatch.setattr(module, "op", operations, raising=False)

    module.upgrade()
    upgrade_calls = tuple(operations.calls)
    module.downgrade()
    downgrade_calls = tuple(operations.calls[len(upgrade_calls) :])

    if module.revision == "0001":
        assert upgrade_calls == downgrade_calls == ()
    else:
        assert upgrade_calls, f"revision {module.revision} upgrade emitted no operations"
        # These revisions intentionally preserve data/schema on downgrade.
        if module.revision not in {"0012", "0014", "0016"}:
            assert downgrade_calls, f"revision {module.revision} downgrade emitted no operations"


@pytest.mark.parametrize(
    "module_name",
    tuple(
        name
        for name in REVISION_MODULES
        if Path(name.rpartition(".")[2]).name
        in {
            "0012_backfill_consumer_keys",
            "0014_audit_chain_seq_default",
            "0016_durable_reference_hardening",
            "0017_independent_consumer_key_expiry",
            "0018_document_deletion_tombstones",
            "0019_consumer_key_expiry_provenance",
            "0020_document_upload_intents",
            "0021_artifact_upload_intents",
            "0022_legacy_audit_writer_compatibility",
            "0023_work_item_mutations",
            "0029_saved_query_connection_affinity",
        }
    ),
)
def test_postgresql_specific_revision_paths_fail_closed_on_other_dialects(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> None:
    module = importlib.import_module(module_name)
    operations = RecordingOperations(dialect="sqlite")
    monkeypatch.setattr(module, "op", operations)

    module.upgrade()
    module.downgrade()

    assert not any(call.name == "execute" for call in operations.calls)


def test_0029_downgrade_checks_for_bound_queries_before_destructive_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module(
        "apex.persistence.migrations.versions.0029_saved_query_connection_affinity"
    )
    operations = RecordingOperations()
    monkeypatch.setattr(module, "op", operations)

    module.downgrade()

    assert [call.name for call in operations.calls] == ["execute", "drop_index", "drop_column"]
    guard_sql = operations.calls[0].args[0]
    assert "WHERE connection_id IS NOT NULL" in guard_sql
    assert "cannot downgrade with bound saved queries present" in guard_sql


def test_domain_table_revision_has_symmetric_schema_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("apex.persistence.migrations.versions.0003_m2_domain_tables")
    operations = RecordingOperations()
    monkeypatch.setattr(module, "op", operations)

    module.upgrade()
    created = {call.args[0] for call in operations.calls if call.name == "create_table"}
    operations.calls.clear()
    module.downgrade()
    dropped = {call.args[0] for call in operations.calls if call.name == "drop_table"}

    assert created == dropped
    assert {"applications", "connections", "documents", "prompts"} <= created
