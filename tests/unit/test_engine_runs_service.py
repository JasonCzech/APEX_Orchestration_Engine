"""Engine-run projection write helpers."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from apex.persistence.models import Base, EngineRun
from apex.services import engine_runs
from apex.services.engine_runs import (
    COMPLETION_COLLECTION_TEARDOWN,
    EngineRunReservationRejectedError,
    _authoritative_scope,
    _bind_locked_run_ownership,
    _completion_replay_status,
    _insert_reservation_statement,
    _normalize_development_connection_ids,
    _upsert_statement,
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
        artifact_namespace="engine-runs/completion",
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
        artifact_namespace="engine-runs/completion",
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
