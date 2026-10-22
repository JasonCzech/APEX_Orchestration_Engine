"""Migration reflection preserves caller state and the original failure."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import Connection
from sqlalchemy.exc import DBAPIError

from apex.persistence.migration_scope import migration_schema_scope


class ConnectionProbe:
    def __init__(self, dialect: str = "postgresql") -> None:
        self.dialect = SimpleNamespace(name=dialect, default_schema_name="apex")
        self.path = '"$user", public'
        self.failed = False
        self.calls: list[str] = []

    def scalar(self, statement: Any) -> str:
        self.calls.append(str(statement))
        return self.path

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> None:
        self.calls.append(str(statement))
        if self.failed:
            raise DBAPIError(str(statement), params, RuntimeError("transaction aborted"))
        self.path = params["search_path"] if params else "public"


@pytest.mark.parametrize("raises", [False, True])
def test_migration_schema_scope_restores_caller_state_after_python_error(raises: bool) -> None:
    probe = ConnectionProbe()
    failure = ValueError("autogeneration failed")
    try:
        with migration_schema_scope(cast(Connection, probe)):
            assert probe.path == probe.dialect.default_schema_name == "public"
            if raises:
                raise failure
    except ValueError as exc:
        assert raises and exc is failure
    assert probe.path == '"$user", public'
    assert probe.dialect.default_schema_name == "apex"


def test_migration_schema_scope_does_not_mask_aborted_transaction_error() -> None:
    probe = ConnectionProbe()
    failure = DBAPIError("migration statement", {}, RuntimeError("original failure"))
    with pytest.raises(DBAPIError) as caught:
        with migration_schema_scope(cast(Connection, probe)):
            probe.failed = True
            raise failure
    assert caught.value is failure
    assert probe.dialect.default_schema_name == "apex"
    # The real connection must roll back its failed transaction; the integration
    # regression verifies PostgreSQL also restores this transaction-local path.
    assert probe.path == "public"


def test_migration_schema_scope_does_not_suppress_a_standalone_restore_failure() -> None:
    probe = ConnectionProbe()
    with pytest.raises(DBAPIError, match="transaction aborted"):
        with migration_schema_scope(cast(Connection, probe)):
            probe.failed = True
    assert probe.dialect.default_schema_name == "apex"


def test_non_postgresql_migration_scope_does_not_touch_search_path() -> None:
    probe = ConnectionProbe("sqlite")
    with migration_schema_scope(cast(Connection, probe)):
        assert probe.dialect.default_schema_name == "apex"
    assert probe.calls == []
