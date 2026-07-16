"""Engine-run projection write helpers."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from apex.persistence.models import Base, Connection, EngineRun
from apex.services import engine_runs
from apex.services.engine_runs import (
    COMPLETION_COLLECTION_TEARDOWN,
    EngineRunReservationRejectedError,
    _authoritative_scope,
    _bind_locked_run_ownership,
    _completion_replay_status,
    _insert_reservation_statement,
    _normalize_development_connection_ids,
    _normalize_scope_provenance,
    _upsert_statement,
    _verify_artifact_connection_reservation,
    _verify_execution_connection_reservation,
    _verify_reservation_connection_generation,
    _verify_reservation_handle_identity,
)


def _terminal_completion_row(
    *,
    execution_version: datetime,
    artifact_version: datetime,
) -> EngineRun:
    return EngineRun(
        thread_id="completion-thread",
        attempt=2,
        project_id="project-a",
        app_id="app-a",
        ownership_known=True,
        scope_ownership_known=True,
        engine="loadrunner",
        external_run_id="lre-42",
        artifact_namespace=engine_runs.engine_artifact_namespace("completion-thread-execution-a2"),
        artifact_connection_id="artifact-a",
        artifact_connection_version=artifact_version,
        execution_connection_version=execution_version,
        completion_kind=COMPLETION_COLLECTION_TEARDOWN,
        handle={
            "engine": "loadrunner",
            "connection_id": "engine-a",
            "external_run_id": "lre-42",
            "idempotency_key": "completion-thread-execution-a2",
            "extras": {"run_id": "42"},
        },
        status="completed",
    )


def _recover_completion(
    row: EngineRun,
    execution_version: datetime,
    artifact_version: datetime,
) -> str | None:
    return _completion_replay_status(
        row,
        thread_id="completion-thread",
        attempt=2,
        engine="loadrunner",
        handle={
            "engine": "loadrunner",
            "connection_id": "engine-a",
            "external_run_id": "lre-42",
            "idempotency_key": "completion-thread-execution-a2",
            "extras": {"run_id": "42"},
        },
        project_id="project-a",
        app_id="app-a",
        external_run_id="lre-42",
        artifact_namespace=engine_runs.engine_artifact_namespace("completion-thread-execution-a2"),
        artifact_connection_id="artifact-a",
        artifact_connection_version=artifact_version,
        connection_version=execution_version,
        completion_kind=COMPLETION_COLLECTION_TEARDOWN,
        expected_statuses=frozenset({"completed"}),
    )


def test_recovered_reservation_requires_exact_connection_generation() -> None:
    original_generation = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    replacement_generation = datetime(2026, 7, 15, 12, 1, tzinfo=UTC)
    row = EngineRun(
        thread_id="generation-fence",
        attempt=1,
        ownership_known=True,
        scope_ownership_known=True,
        engine="loadrunner",
        connection_id="engine-a",
        execution_connection_version=original_generation,
        handle={"idempotency_key": "generation-fence-execution-a1"},
        status="ready",
    )

    with pytest.raises(EngineRunReservationRejectedError, match="connection generation"):
        _verify_reservation_connection_generation(
            row,
            connection_id="engine-a",
            connection_version=replacement_generation,
            operation="start",
        )

    _verify_reservation_connection_generation(
        row,
        connection_id="engine-a",
        connection_version=original_generation,
        operation="provision",
    )


def test_execution_connection_reservation_requires_adapter_and_project_affinity() -> None:
    def connection(*, project_id: str | None = None) -> Connection:
        return Connection(
            id="engine-a",
            kind="execution_engine",
            provider="loadrunner",
            name="Engine A",
            project_id=project_id,
            enabled=True,
        )

    _verify_execution_connection_reservation(
        connection(project_id=None),
        engine="loadrunner",
        project_id="project-a",
    )
    _verify_execution_connection_reservation(
        connection(project_id="project-a"),
        engine="loadrunner",
        project_id="project-a",
    )

    with pytest.raises(EngineRunReservationRejectedError, match="project ownership"):
        _verify_execution_connection_reservation(
            connection(project_id="project-b"),
            engine="loadrunner",
            project_id="project-a",
        )

    wrong_adapter = connection(project_id="project-a")
    wrong_adapter.kind = "artifact_store"
    with pytest.raises(EngineRunReservationRejectedError, match="adapter identity"):
        _verify_execution_connection_reservation(
            wrong_adapter,
            engine="loadrunner",
            project_id="project-a",
        )

    wrong_provider = connection(project_id="project-a")
    wrong_provider.provider = "sim"
    with pytest.raises(EngineRunReservationRejectedError, match="adapter identity"):
        _verify_execution_connection_reservation(
            wrong_provider,
            engine="loadrunner",
            project_id="project-a",
        )


def test_artifact_connection_reservation_requires_kind_and_project_affinity() -> None:
    def connection(*, project_id: str | None = None) -> Connection:
        return Connection(
            id="artifact-a",
            kind="artifact_store",
            provider="s3",
            name="Artifact A",
            project_id=project_id,
            enabled=True,
        )

    _verify_artifact_connection_reservation(
        connection(project_id=None),
        project_id="project-a",
    )
    _verify_artifact_connection_reservation(
        connection(project_id="project-a"),
        project_id="project-a",
    )

    with pytest.raises(EngineRunReservationRejectedError, match="project ownership"):
        _verify_artifact_connection_reservation(
            connection(project_id="project-b"),
            project_id="project-a",
        )

    wrong_kind = connection(project_id="project-a")
    wrong_kind.kind = "execution_engine"
    with pytest.raises(EngineRunReservationRejectedError, match="wrong kind"):
        _verify_artifact_connection_reservation(
            wrong_kind,
            project_id="project-a",
        )


def test_recovered_reservation_requires_exact_handle_affinity() -> None:
    row_handle = {
        "engine": "loadrunner",
        "connection_id": "engine-b",
        "idempotency_key": "handle-fence-execution-a1",
    }

    with pytest.raises(EngineRunReservationRejectedError, match="connection affinity"):
        _verify_reservation_handle_identity(
            row_handle,
            engine="loadrunner",
            idempotency_key="handle-fence-execution-a1",
            connection_id="engine-a",
            operation="provision",
        )


def test_terminal_completion_replay_requires_exact_post_effect_witness() -> None:
    execution_version = datetime.now(UTC)
    artifact_version = datetime.now(UTC)
    row = _terminal_completion_row(
        execution_version=execution_version,
        artifact_version=artifact_version,
    )

    assert _recover_completion(row, execution_version, artifact_version) == "completed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt", 1),
        ("engine", "sim"),
        ("project_id", "project-b"),
        ("ownership_known", False),
        ("scope_ownership_known", False),
        ("artifact_connection_id", "artifact-b"),
        ("completion_kind", None),
        ("status", "aborted"),
    ],
)
def test_terminal_completion_replay_rejects_stale_or_malformed_projection(
    field: str,
    value: object,
) -> None:
    execution_version = datetime.now(UTC)
    artifact_version = datetime.now(UTC)
    row = _terminal_completion_row(
        execution_version=execution_version,
        artifact_version=artifact_version,
    )
    setattr(row, field, value)

    with pytest.raises(EngineRunReservationRejectedError, match="completion witness"):
        _recover_completion(row, execution_version, artifact_version)


def test_terminal_completion_replay_rejects_wrong_idempotency_or_generation() -> None:
    execution_version = datetime.now(UTC)
    artifact_version = datetime.now(UTC)
    row = _terminal_completion_row(
        execution_version=execution_version,
        artifact_version=artifact_version,
    )
    row.handle = {**row.handle, "idempotency_key": "different-attempt"}

    with pytest.raises(EngineRunReservationRejectedError, match="completion witness"):
        _recover_completion(row, execution_version, artifact_version)

    row = _terminal_completion_row(
        execution_version=execution_version,
        artifact_version=artifact_version,
    )
    with pytest.raises(EngineRunReservationRejectedError, match="completion witness"):
        _recover_completion(row, datetime.now(UTC), artifact_version)
    with pytest.raises(EngineRunReservationRejectedError, match="completion witness"):
        _recover_completion(row, execution_version, datetime.now(UTC))


def test_dev_artifact_id_does_not_hide_real_engine_terminal_projection() -> None:
    skip, connection_id, connection_version, artifact_connection_id = (
        _normalize_development_connection_ids(
            {"connection_id": "real-engine-connection"},
            None,
            None,
            "dev-artifact-store-memory",
            is_locked_down=False,
        )
    )

    assert skip is False
    assert connection_id is None
    assert connection_version is None
    assert artifact_connection_id is None


def test_fully_static_engine_projection_still_skips_postgres() -> None:
    version = datetime.now(UTC)
    skip, connection_id, connection_version, artifact_connection_id = (
        _normalize_development_connection_ids(
            {"connection_id": "dev-engine-sim"},
            "dev-engine-sim",
            version,
            "dev-artifact-store-memory",
            is_locked_down=False,
        )
    )

    assert skip is True
    assert connection_id is None
    assert connection_version is None
    assert artifact_connection_id is None


def test_only_required_complete_scope_is_authoritative() -> None:
    assert (
        _authoritative_scope(
            "project-a",
            "app-a",
            required=True,
            operation="test",
        )
        is True
    )
    assert (
        _authoritative_scope(
            "project-a",
            "app-a",
            required=False,
            operation="test",
        )
        is False
    )
    for project_id, app_id in ((None, None), ("project-a", None), (" ", "app-a")):
        with pytest.raises(EngineRunReservationRejectedError, match="exact project"):
            _authoritative_scope(
                project_id,
                app_id,
                required=True,
                operation="test",
            )


def test_scope_authority_rejects_noncanonical_unbounded_and_hostile_values() -> None:
    calls: list[str] = []

    class HostileScope(str):
        def strip(self, *_args: object, **_kwargs: object) -> str:
            calls.append("strip")
            raise AssertionError("hostile scope hook ran")

    for project_id, app_id in (
        (HostileScope("project-a"), "app-a"),
        ("project-a", HostileScope("app-a")),
        ("p" * 256, "app-a"),
        (" project-a", "app-a"),
        ("project-a", "app-a\nshadow"),
    ):
        with pytest.raises(EngineRunReservationRejectedError, match="exact project"):
            _authoritative_scope(
                project_id,
                app_id,
                required=True,
                operation="test",
            )
        normalized = _normalize_scope_provenance(
            {
                "ownership_known": True,
                "scope_ownership_known": True,
                "project_id": project_id,
                "app_id": app_id,
            }
        )
        assert normalized["ownership_known"] is False
        assert normalized["scope_ownership_known"] is False

    assert calls == []


def test_required_persisted_connection_reservation_requires_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        engine_runs,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=False),
    )

    with pytest.raises(RuntimeError, match="missing its reservation version"):
        asyncio.run(
            engine_runs.record_engine_run(
                "thread-1",
                1,
                "sim",
                {
                    "engine": "sim",
                    "connection_id": "real-engine",
                    "idempotency_key": "thread-1-execution-a1",
                },
                "provisioning",
                project_id="project-a",
                app_id="app-a",
                connection_id="real-engine",
                connection_version=None,
                required=True,
            )
        )


async def test_engine_run_boundaries_reject_credentials_before_settings_or_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "engine-writer-secret-canary"
    logged: list[dict[str, Any]] = []

    class CapturingLogger:
        def warning(self, _event: str, **fields: Any) -> None:
            logged.append(fields)

    def forbidden_settings() -> None:
        raise AssertionError("unsafe projection must fail before reading runtime settings")

    monkeypatch.setattr(engine_runs, "logger", CapturingLogger())
    monkeypatch.setattr(engine_runs, "get_settings", forbidden_settings)
    unsafe_handle = {
        "engine": "sim",
        "idempotency_key": f"api_key={canary}",
    }
    safe_handle = {
        "engine": "sim",
        "idempotency_key": "engine-writer-execution-a1",
    }

    calls = (
        engine_runs.record_engine_run(
            "thread-writer",
            1,
            "sim",
            unsafe_handle,
            "running",
            project_id="project-a",
            app_id="app-a",
            required=True,
        ),
        engine_runs.record_engine_run(
            "thread-writer",
            1,
            "sim",
            safe_handle,
            "completed",
            project_id="project-a",
            app_id="app-a",
            summary={
                "engine": "sim",
                "passed": True,
                "notes": f"api_key={canary}",
            },
            required=True,
        ),
        engine_runs.prepare_engine_start(
            "thread-writer",
            1,
            "sim",
            unsafe_handle,
            project_id="project-a",
            app_id="app-a",
        ),
        engine_runs.prepare_engine_provision(
            "thread-writer",
            1,
            "sim",
            unsafe_handle,
            project_id="project-a",
            app_id="app-a",
        ),
        engine_runs.recover_engine_completion(
            "thread-writer",
            1,
            "sim",
            unsafe_handle,
            project_id="project-a",
            app_id="app-a",
            artifact_namespace=engine_runs.engine_artifact_namespace("engine-writer-execution-a1"),
            completion_kind=COMPLETION_COLLECTION_TEARDOWN,
            expected_statuses=frozenset({"completed"}),
        ),
    )
    for call in calls:
        with pytest.raises(ValueError, match="credential material") as raised:
            await call
        assert canary not in str(raised.value)

    assert canary not in repr(logged)


async def test_required_projection_detaches_unexpected_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "engine-projection-backend-secret-canary"

    class BackendFailure(Exception):
        pass

    def fail_settings() -> None:
        raise BackendFailure(canary)

    monkeypatch.setattr(engine_runs, "get_settings", fail_settings)

    with pytest.raises(engine_runs.EngineRunProjectionError) as excinfo:
        await engine_runs.record_engine_run(
            "thread-1",
            1,
            "sim",
            {
                "engine": "sim",
                "idempotency_key": "thread-1-execution-a1",
            },
            "running",
            project_id="project-a",
            app_id="app-a",
            required=True,
        )

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in repr(excinfo.value)


def test_engine_run_model_validation_does_not_retain_raw_projection_values() -> None:
    canary = "bare-engine-projection-canary"

    with pytest.raises(ValueError, match="handle is invalid") as handle_error:
        engine_runs._validated_projection_handle(
            {
                "engine": "sim",
                "idempotency_key": "safe-engine-attempt",
                "external_run_id": canary,
                "extras": [],
            },
            engine="sim",
            allow_empty=False,
        )

    assert handle_error.value.__cause__ is None
    assert handle_error.value.__context__ is None
    assert canary not in str(handle_error.value)

    with pytest.raises(ValueError, match="summary is invalid") as summary_error:
        engine_runs._validated_projection_summary(
            {"engine": "sim", "passed": f"invalid-{canary}"},
            engine="sim",
        )

    assert summary_error.value.__cause__ is None
    assert summary_error.value.__context__ is None
    assert canary not in str(summary_error.value)


async def test_invalid_thread_identifier_is_redacted_from_engine_run_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "thread-secret-canary"
    logged: list[dict[str, Any]] = []

    class CapturingLogger:
        def warning(self, _event: str, **fields: Any) -> None:
            logged.append(fields)

    def forbidden_settings() -> None:
        raise AssertionError("unsafe projection must fail before reading runtime settings")

    monkeypatch.setattr(engine_runs, "logger", CapturingLogger())
    monkeypatch.setattr(engine_runs, "get_settings", forbidden_settings)

    with pytest.raises(ValueError, match="credential material") as raised:
        await engine_runs.record_engine_run(
            f"api_key={canary}",
            1,
            "sim",
            {"engine": "sim", "idempotency_key": "safe-engine-attempt"},
            "running",
            project_id="project-a",
            app_id="app-a",
            required=True,
        )

    assert canary not in str(raised.value)
    assert canary not in repr(logged)


def test_required_upsert_rejects_zero_row_terminal_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RejectedSession:
        committed = False
        rolled_back = False

        async def __aenter__(self) -> "RejectedSession":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, _statement: object) -> SimpleNamespace:
            # Models ON CONFLICT ... DO UPDATE WHERE rejecting a nonterminal
            # transition because attempt 1 is already terminal.
            return SimpleNamespace(rowcount=0)

        async def rollback(self) -> None:
            self.rolled_back = True

        async def commit(self) -> None:
            self.committed = True

    class Factory:
        def __init__(self, session: RejectedSession) -> None:
            self.session = session

        def __call__(self) -> RejectedSession:
            return self.session

    class Engine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    session = RejectedSession()
    database_engine = Engine()
    monkeypatch.setattr(
        engine_runs,
        "get_settings",
        lambda: SimpleNamespace(
            is_locked_down=False,
            database=SimpleNamespace(uri="sqlite+aiosqlite://", ssl_mode="disable"),
        ),
    )
    monkeypatch.setattr(
        engine_runs, "create_async_engine", lambda *_args, **_kwargs: database_engine
    )
    monkeypatch.setattr(
        engine_runs,
        "async_sessionmaker",
        lambda *_args, **_kwargs: Factory(session),
    )

    with pytest.raises(RuntimeError, match="rejected by the durable projection"):
        asyncio.run(
            engine_runs.record_engine_run(
                "thread-terminal",
                1,
                "sim",
                {
                    "engine": "sim",
                    "connection_id": "real-engine",
                    "idempotency_key": "thread-terminal-execution-a1",
                },
                "provisioning",
                project_id="project-a",
                app_id="app-a",
                required=True,
            )
        )

    assert session.rolled_back is True
    assert session.committed is False
    assert database_engine.disposed is True


def test_sqlite_upsert_is_replay_safe_and_preserves_omitted_ownership_fields() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        connection.execute(
            _upsert_statement(
                {
                    "thread_id": "thread-1",
                    "attempt": 1,
                    "project_id": "project-1",
                    "app_id": "app-1",
                    "ownership_known": True,
                    "scope_ownership_known": True,
                    "engine": "sim",
                    "handle": {"idempotency_key": "key-1"},
                    "status": "running",
                },
                "sqlite",
            )
        )
        # A later partial replay updates mutable projection fields without
        # erasing ownership fields that were intentionally omitted.
        connection.execute(
            _upsert_statement(
                {
                    "thread_id": "thread-1",
                    "attempt": 1,
                    "ownership_known": True,
                    "scope_ownership_known": True,
                    "engine": "sim",
                    "handle": {"idempotency_key": "key-1", "external_run_id": "run-1"},
                    "external_run_id": "run-1",
                    "status": "completed",
                },
                "sqlite",
            )
        )

    with Session(engine) as session:
        rows = list(session.scalars(select(EngineRun)))

    assert len(rows) == 1
    assert rows[0].project_id == "project-1"
    assert rows[0].app_id == "app-1"
    assert rows[0].external_run_id == "run-1"
    assert rows[0].status == "completed"


def test_sqlite_upsert_rejects_known_cross_app_rebinding() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        base = {
            "thread_id": "ownership-collision",
            "attempt": 1,
            "project_id": "project-1",
            "ownership_known": True,
            "scope_ownership_known": True,
            "engine": "sim",
            "handle": {"idempotency_key": "ownership-collision-a1"},
        }
        connection.execute(
            _upsert_statement({**base, "app_id": "app-a", "status": "running"}, "sqlite")
        )
        collision = connection.execute(
            _upsert_statement(
                {**base, "app_id": "app-b", "status": "collecting"},
                "sqlite",
            )
        )

    with Session(engine) as session:
        row = session.scalar(select(EngineRun))

    assert collision.rowcount == 0
    assert row is not None
    assert row.project_id == "project-1"
    assert row.app_id == "app-a"
    assert row.status == "running"


def test_sqlite_upsert_rejects_provider_rebinding_within_same_scope() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        base = {
            "thread_id": "provider-collision",
            "attempt": 1,
            "project_id": "project-1",
            "app_id": "app-a",
            "ownership_known": True,
            "scope_ownership_known": True,
        }
        connection.execute(
            _upsert_statement(
                {
                    **base,
                    "engine": "sim",
                    "handle": {"idempotency_key": "provider-collision-a1"},
                    "status": "running",
                },
                "sqlite",
            )
        )
        collision = connection.execute(
            _upsert_statement(
                {
                    **base,
                    "engine": "loadrunner",
                    "handle": {
                        "idempotency_key": "provider-collision-a1",
                        "external_run_id": "foreign-run",
                    },
                    "status": "collecting",
                },
                "sqlite",
            )
        )

    with Session(engine) as session:
        row = session.scalar(select(EngineRun))

    assert collision.rowcount == 0
    assert row is not None
    assert row.engine == "sim"
    assert row.status == "running"
    assert row.handle == {"idempotency_key": "provider-collision-a1"}


def test_sqlite_upsert_rejects_projectless_callback_for_known_owned_run() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        connection.execute(
            _upsert_statement(
                {
                    "thread_id": "known-owned",
                    "attempt": 1,
                    "project_id": "project-1",
                    "app_id": "app-a",
                    "ownership_known": True,
                    "scope_ownership_known": True,
                    "engine": "sim",
                    "handle": {"idempotency_key": "known-owned-a1"},
                    "status": "running",
                },
                "sqlite",
            )
        )
        projectless = connection.execute(
            _upsert_statement(
                {
                    "thread_id": "known-owned",
                    "attempt": 1,
                    "project_id": None,
                    "app_id": None,
                    "ownership_known": True,
                    "scope_ownership_known": True,
                    "engine": "sim",
                    "handle": {"idempotency_key": "known-owned-a1"},
                    "status": "collecting",
                },
                "sqlite",
            )
        )

    assert projectless.rowcount == 0


def test_locked_recovery_requires_exact_scope_or_explicit_app_repair() -> None:
    known = EngineRun(
        thread_id="known",
        attempt=1,
        project_id="project-1",
        app_id="app-a",
        ownership_known=True,
        scope_ownership_known=True,
        engine="sim",
        handle={},
        status="running",
    )
    with pytest.raises(EngineRunReservationRejectedError, match="exact project"):
        _bind_locked_run_ownership(
            known,
            project_id=None,
            app_id=None,
            operation="start",
        )

    ambiguous = EngineRun(
        thread_id="ambiguous",
        attempt=1,
        project_id="project-1",
        app_id=None,
        ownership_known=True,
        scope_ownership_known=False,
        engine="sim",
        handle={},
        status="running",
    )
    with pytest.raises(EngineRunReservationRejectedError, match="exact project"):
        _bind_locked_run_ownership(
            ambiguous,
            project_id="project-1",
            app_id=None,
            operation="start",
        )

    _bind_locked_run_ownership(
        ambiguous,
        project_id="project-1",
        app_id="app-a",
        operation="start",
    )
    assert ambiguous.app_id == "app-a"
    assert ambiguous.ownership_known is True
    assert ambiguous.scope_ownership_known is True


def test_sqlite_upsert_only_promotes_quarantined_row_with_explicit_app_owner() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        for thread_id in ("legacy-project", "legacy-app"):
            connection.execute(
                _upsert_statement(
                    {
                        "thread_id": thread_id,
                        "attempt": 1,
                        "project_id": "project-1",
                        "ownership_known": True,
                        "scope_ownership_known": False,
                        "engine": "sim",
                        "handle": {"idempotency_key": f"{thread_id}-a1"},
                        "status": "provisioning",
                    },
                    "sqlite",
                )
            )
        connection.execute(
            _upsert_statement(
                {
                    "thread_id": "legacy-project",
                    "attempt": 1,
                    "project_id": "project-1",
                    "ownership_known": True,
                    "scope_ownership_known": True,
                    "engine": "sim",
                    "handle": {"idempotency_key": "legacy-project-a1"},
                    "status": "ready",
                },
                "sqlite",
            )
        )
        enriched = connection.execute(
            _upsert_statement(
                {
                    "thread_id": "legacy-app",
                    "attempt": 1,
                    "project_id": "project-1",
                    "app_id": "app-a",
                    "ownership_known": True,
                    "scope_ownership_known": True,
                    "engine": "sim",
                    "handle": {"idempotency_key": "legacy-app-a1"},
                    "status": "ready",
                },
                "sqlite",
            )
        )

    with Session(engine) as session:
        rows = {row.thread_id: row for row in session.scalars(select(EngineRun))}

    assert enriched.rowcount == 1
    assert rows["legacy-project"].ownership_known is False
    assert rows["legacy-project"].scope_ownership_known is False
    assert rows["legacy-project"].app_id is None
    assert rows["legacy-app"].ownership_known is True
    assert rows["legacy-app"].scope_ownership_known is True
    assert rows["legacy-app"].app_id == "app-a"


def test_best_effort_callback_cannot_promote_or_poison_quarantined_scope() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        connection.execute(
            _upsert_statement(
                {
                    "thread_id": "quarantined-callback",
                    "attempt": 1,
                    "project_id": None,
                    "app_id": None,
                    "ownership_known": False,
                    "scope_ownership_known": False,
                    "engine": "sim",
                    "handle": {"idempotency_key": "quarantined-callback-a1"},
                    "status": "provisioning",
                },
                "sqlite",
            )
        )
        best_effort = connection.execute(
            _upsert_statement(
                {
                    "thread_id": "quarantined-callback",
                    "attempt": 1,
                    "project_id": "attacker-project",
                    "app_id": "attacker-app",
                    "ownership_known": False,
                    "scope_ownership_known": False,
                    "engine": "sim",
                    "handle": {"idempotency_key": "quarantined-callback-a1"},
                    "status": "ready",
                },
                "sqlite",
            )
        )
        required = connection.execute(
            _upsert_statement(
                {
                    "thread_id": "quarantined-callback",
                    "attempt": 1,
                    "project_id": "project-a",
                    "app_id": "app-a",
                    "ownership_known": True,
                    "scope_ownership_known": True,
                    "engine": "sim",
                    "handle": {"idempotency_key": "quarantined-callback-a1"},
                    "status": "ready",
                },
                "sqlite",
            )
        )

    with Session(engine) as session:
        row = session.scalar(select(EngineRun))

    assert best_effort.rowcount == required.rowcount == 1
    assert row is not None
    assert row.project_id == "project-a"
    assert row.app_id == "app-a"
    assert row.ownership_known is True
    assert row.scope_ownership_known is True


def test_sqlite_upsert_does_not_reopen_or_replace_a_terminal_attempt() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        base = {
            "thread_id": "thread-terminal",
            "attempt": 1,
            "ownership_known": True,
            "scope_ownership_known": True,
            "engine": "sim",
            "handle": {"idempotency_key": "key-terminal"},
        }
        connection.execute(_upsert_statement({**base, "status": "aborted"}, "sqlite"))
        connection.execute(_upsert_statement({**base, "status": "running"}, "sqlite"))
        connection.execute(_upsert_statement({**base, "status": "completed"}, "sqlite"))

    with Session(engine) as session:
        row = session.scalar(select(EngineRun))

    assert row is not None
    assert row.status == "aborted"


def test_sqlite_terminal_replay_acknowledges_without_mutating_first_winner() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        base = {
            "thread_id": "thread-terminal-replay",
            "attempt": 1,
            "ownership_known": True,
            "scope_ownership_known": True,
            "engine": "sim",
            "status": "failed",
        }
        connection.execute(
            _upsert_statement(
                {
                    **base,
                    "handle": {
                        "idempotency_key": "key-terminal-replay",
                        "external_run_id": "winning-run",
                    },
                    "external_run_id": "winning-run",
                    "summary": {"winner": True},
                },
                "sqlite",
            )
        )
        replay = connection.execute(
            _upsert_statement(
                {
                    **base,
                    "handle": {},
                    "summary": {"stale_callback": True},
                },
                "sqlite",
            )
        )

    with Session(engine) as session:
        row = session.scalar(select(EngineRun))

    assert replay.rowcount == 1
    assert row is not None
    assert row.status == "failed"
    assert row.external_run_id == "winning-run"
    assert row.handle == {
        "idempotency_key": "key-terminal-replay",
        "external_run_id": "winning-run",
    }
    assert row.summary == {"winner": True}


def test_provision_reservation_replay_never_erases_committed_full_handle() -> None:
    engine = create_engine("sqlite://")
    full_handle = {
        "engine": "loadrunner",
        "connection_id": "engine-a",
        "external_run_id": "lre-1042",
        "idempotency_key": "provision-replay-execution-a1",
        "extras": {"run_id": "1042", "test_id": "88"},
    }
    placeholder = {
        "engine": "loadrunner",
        "connection_id": "engine-a",
        "idempotency_key": "provision-replay-execution-a1",
        "extras": {},
    }
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        connection.execute(
            _upsert_statement(
                {
                    "thread_id": "provision-replay",
                    "attempt": 1,
                    "ownership_known": True,
                    "scope_ownership_known": True,
                    "engine": "loadrunner",
                    "handle": full_handle,
                    "external_run_id": "lre-1042",
                    "status": "provisioning",
                },
                "sqlite",
            )
        )
        for _ in range(2):
            replay = connection.execute(
                _insert_reservation_statement(
                    {
                        "thread_id": "provision-replay",
                        "attempt": 1,
                        "ownership_known": True,
                        "scope_ownership_known": True,
                        "engine": "loadrunner",
                        "handle": placeholder,
                        "status": "provisioning",
                    },
                    "sqlite",
                )
            )
            assert replay.rowcount == 0

    with Session(engine) as session:
        row = session.scalar(select(EngineRun))

    assert row is not None
    assert row.handle == full_handle
    assert row.external_run_id == "lre-1042"
    assert row.status == "provisioning"


def test_nonterminal_replay_never_downgrades_or_erases_later_stage() -> None:
    engine = create_engine("sqlite://")
    full_handle = {
        "engine": "loadrunner",
        "connection_id": "engine-a",
        "external_run_id": "lre-1042",
        "idempotency_key": "nonterminal-order-execution-a1",
        "extras": {"run_id": "1042"},
    }
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        base = {
            "thread_id": "nonterminal-order",
            "attempt": 1,
            "ownership_known": True,
            "scope_ownership_known": True,
            "engine": "loadrunner",
        }
        connection.execute(
            _upsert_statement(
                {
                    **base,
                    "handle": full_handle,
                    "external_run_id": "lre-1042",
                    "status": "running",
                },
                "sqlite",
            )
        )
        stale_ready = connection.execute(
            _upsert_statement(
                {
                    **base,
                    "handle": {
                        "idempotency_key": "nonterminal-order-execution-a1",
                        "extras": {},
                    },
                    "status": "ready",
                },
                "sqlite",
            )
        )
        connection.execute(
            _upsert_statement(
                {
                    **base,
                    "handle": full_handle,
                    "external_run_id": "lre-1042",
                    "artifact_connection_id": "artifact-a",
                    "status": "collecting",
                },
                "sqlite",
            )
        )
        stale_running = connection.execute(
            _upsert_statement(
                {
                    **base,
                    "handle": {
                        "idempotency_key": "nonterminal-order-execution-a1",
                        "extras": {},
                    },
                    "status": "running",
                },
                "sqlite",
            )
        )

    with Session(engine) as session:
        row = session.scalar(select(EngineRun))

    assert stale_ready.rowcount == stale_running.rowcount == 1
    assert row is not None
    assert row.status == "collecting"
    assert row.handle == full_handle
    assert row.external_run_id == "lre-1042"
    assert row.artifact_connection_id == "artifact-a"


class _AsyncProjectionSession:
    def __init__(self, scalar_results: list[Any] | None = None) -> None:
        self.scalar_results = iter(scalar_results or [])
        self.executed: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> "_AsyncProjectionSession":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def scalar(self, _statement: Any) -> Any:
        return next(self.scalar_results)

    async def execute(self, statement: Any) -> SimpleNamespace:
        self.executed.append(statement)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _AsyncProjectionFactory:
    def __init__(self, session: _AsyncProjectionSession) -> None:
        self.session = session

    def __call__(self) -> _AsyncProjectionSession:
        return self.session


def _install_async_projection_db(
    monkeypatch: pytest.MonkeyPatch,
    session: _AsyncProjectionSession,
) -> tuple[object, list[object]]:
    database_engine = object()
    disposed: list[object] = []
    monkeypatch.setattr(
        engine_runs,
        "get_settings",
        lambda: SimpleNamespace(
            is_locked_down=True,
            database=SimpleNamespace(uri="sqlite+aiosqlite://", ssl_mode="disable"),
        ),
    )
    monkeypatch.setattr(
        engine_runs,
        "create_async_engine",
        lambda *_args, **_kwargs: database_engine,
    )
    monkeypatch.setattr(
        engine_runs,
        "async_sessionmaker",
        lambda *_args, **_kwargs: _AsyncProjectionFactory(session),
    )

    async def dispose(value: object) -> None:
        disposed.append(value)

    monkeypatch.setattr(engine_runs, "dispose_engine_instance_definitively", dispose)
    return database_engine, disposed


async def test_required_record_locks_both_connection_generations_and_commits_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = datetime.now(UTC)
    execution_connection = SimpleNamespace(
        enabled=True,
        kind="execution_engine",
        provider="loadrunner",
        project_id="project-a",
        runtime_version=version,
    )
    artifact_connection = SimpleNamespace(
        enabled=True,
        kind="artifact_store",
        project_id="project-a",
        runtime_version=version,
    )
    session = _AsyncProjectionSession([execution_connection, artifact_connection])
    database_engine, disposed = _install_async_projection_db(monkeypatch, session)

    await engine_runs.record_engine_run(
        "thread-record",
        3,
        "loadrunner",
        {
            "engine": "loadrunner",
            "connection_id": "engine-a",
            "external_run_id": "run-42",
            "idempotency_key": "thread-record-execution-a3",
        },
        "completed",
        project_id="project-a",
        app_id="app-a",
        external_run_id="run-42",
        artifact_namespace=engine_runs.engine_artifact_namespace("thread-record-execution-a3"),
        artifact_connection_id="artifact-a",
        artifact_connection_version=version,
        connection_id="engine-a",
        connection_version=version,
        summary={"engine": "loadrunner", "passed": True},
        completion_kind=COMPLETION_COLLECTION_TEARDOWN,
        required=True,
    )

    assert len(session.executed) == 1
    assert session.commits == 1
    assert session.rollbacks == 0
    assert disposed == [database_engine]


async def test_start_reservation_recovers_an_already_running_exact_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = {
        "engine": "sim",
        "idempotency_key": "thread-start-execution-a2",
        "external_run_id": "sim-42",
    }
    row = EngineRun(
        thread_id="thread-start",
        attempt=2,
        project_id="project-a",
        app_id="app-a",
        ownership_known=True,
        scope_ownership_known=True,
        engine="sim",
        handle=handle,
        status="running",
    )
    session = _AsyncProjectionSession([row])
    database_engine, disposed = _install_async_projection_db(monkeypatch, session)

    recovered = await engine_runs.prepare_engine_start(
        "thread-start",
        2,
        "sim",
        {"engine": "sim", "idempotency_key": "thread-start-execution-a2"},
        project_id="project-a",
        app_id="app-a",
    )

    assert recovered == {
        **handle,
        "connection_id": None,
        "extras": {},
    }
    assert recovered is not handle
    assert session.commits == 1
    assert disposed == [database_engine]


async def test_provision_reservation_recovers_exact_provider_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = {
        "engine": "loadrunner",
        "idempotency_key": "thread-provision-execution-a1",
        "external_run_id": "lre-88",
        "extras": {"run_id": "88"},
    }
    row = EngineRun(
        thread_id="thread-provision",
        attempt=1,
        project_id="project-a",
        app_id="app-a",
        ownership_known=True,
        scope_ownership_known=True,
        engine="loadrunner",
        handle=handle,
        artifact_namespace=engine_runs.engine_artifact_namespace("thread-provision-execution-a1"),
        status="provisioning",
    )
    session = _AsyncProjectionSession([row])
    database_engine, disposed = _install_async_projection_db(monkeypatch, session)

    recovered = await engine_runs.prepare_engine_provision(
        "thread-provision",
        1,
        "loadrunner",
        {
            "engine": "loadrunner",
            "idempotency_key": "thread-provision-execution-a1",
        },
        project_id="project-a",
        app_id="app-a",
        artifact_namespace=engine_runs.engine_artifact_namespace("thread-provision-execution-a1"),
    )

    assert recovered == {**handle, "connection_id": None}
    assert len(session.executed) == 1
    assert session.commits == 1
    assert disposed == [database_engine]


async def test_completion_recovery_reads_and_commits_exact_terminal_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_version = datetime.now(UTC)
    artifact_version = datetime.now(UTC)
    row = _terminal_completion_row(
        execution_version=execution_version,
        artifact_version=artifact_version,
    )
    session = _AsyncProjectionSession([row])
    database_engine, disposed = _install_async_projection_db(monkeypatch, session)

    recovered = await engine_runs.recover_engine_completion(
        "completion-thread",
        2,
        "loadrunner",
        {
            "engine": "loadrunner",
            "connection_id": "engine-a",
            "external_run_id": "lre-42",
            "idempotency_key": "completion-thread-execution-a2",
            "extras": {"run_id": "42"},
        },
        project_id="project-a",
        app_id="app-a",
        external_run_id="lre-42",
        artifact_namespace=engine_runs.engine_artifact_namespace("completion-thread-execution-a2"),
        artifact_connection_id="artifact-a",
        artifact_connection_version=artifact_version,
        connection_id="engine-a",
        connection_version=execution_version,
        completion_kind=COMPLETION_COLLECTION_TEARDOWN,
        expected_statuses=frozenset({"completed"}),
    )

    assert recovered == "completed"
    assert session.commits == 1
    assert disposed == [database_engine]


def test_sync_bridges_run_required_coroutines_without_an_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def record(*_args: Any, **_kwargs: Any) -> None:
        calls.append("record")

    async def start(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        calls.append("start")
        return {"stage": "start"}

    async def provision(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        calls.append("provision")
        return {"stage": "provision"}

    async def completion(*_args: Any, **_kwargs: Any) -> str:
        calls.append("completion")
        return "completed"

    monkeypatch.setattr(engine_runs, "record_engine_run", record)
    monkeypatch.setattr(engine_runs, "prepare_engine_start", start)
    monkeypatch.setattr(engine_runs, "prepare_engine_provision", provision)
    monkeypatch.setattr(engine_runs, "recover_engine_completion", completion)

    engine_runs.record_engine_run_sync(required=True)
    assert engine_runs.prepare_engine_start_sync() == {"stage": "start"}
    assert engine_runs.prepare_engine_provision_sync() == {"stage": "provision"}
    assert engine_runs.recover_engine_completion_sync() == "completed"
    assert calls == ["record", "start", "provision", "completion"]


async def test_sync_bridges_use_worker_loop_when_caller_loop_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def record(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def start(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"stage": "start"}

    async def provision(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"stage": "provision"}

    async def completion(*_args: Any, **_kwargs: Any) -> str:
        return "completed"

    monkeypatch.setattr(engine_runs, "record_engine_run", record)
    monkeypatch.setattr(engine_runs, "prepare_engine_start", start)
    monkeypatch.setattr(engine_runs, "prepare_engine_provision", provision)
    monkeypatch.setattr(engine_runs, "recover_engine_completion", completion)

    engine_runs.record_engine_run_sync(required=True)
    assert engine_runs.prepare_engine_start_sync() == {"stage": "start"}
    assert engine_runs.prepare_engine_provision_sync() == {"stage": "provision"}
    assert engine_runs.recover_engine_completion_sync() == "completed"
