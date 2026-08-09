"""APEX Load native-control durable mirror regression tests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apex.persistence.models import ApexLoadNativeScenario
from apex.persistence.repositories.apex_load_native import (
    ApexLoadNativeRepository,
    NativeResourceIdempotencyConflictError,
    _snapshot,
)


class _RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.executed: list[Any] = []
        self.scalar_calls = 0

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, _statement: object) -> object | None:
        self.scalar_calls += 1
        return (
            SimpleNamespace(
                connection_version=datetime(2026, 8, 7, tzinfo=UTC),
                project_id="project-1",
                claim_token="claim-token",
            )
            if self.scalar_calls == 1
            else None
        )

    def add(self, row: object) -> None:
        self.added.append(row)

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)

    async def commit(self) -> None:
        return None


class _ClaimQuerySession:
    def __init__(self) -> None:
        self.statement: object | None = None

    async def __aenter__(self) -> "_ClaimQuerySession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalars(self, statement: object) -> object:
        self.statement = statement
        return SimpleNamespace(all=lambda: [])

    async def commit(self) -> None:
        return None


class _ScalarSession:
    def __init__(self, value: object | None) -> None:
        self.value = value

    async def __aenter__(self) -> "_ScalarSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, _statement: object) -> object | None:
        return self.value


def test_native_snapshot_drops_database_unsafe_provider_scalars() -> None:
    snapshot = _snapshot(
        {
            "name": "checkout",
            "status": "RUNNING\x00",
            "revision": float("nan"),
            "started_at": "2026-08-07T12:00:00Z",
        }
    )

    assert snapshot["name"] == "checkout"
    assert snapshot["started_at"] == "2026-08-07T12:00:00Z"
    assert "status" not in snapshot
    assert "revision" not in snapshot


async def test_missing_scenario_marks_its_schedule_mirror_missing() -> None:
    session = _RecordingSession()
    repository = ApexLoadNativeRepository(cast(async_sessionmaker[AsyncSession], lambda: session))

    await repository.sync_scenarios(
        connection_id="connection-1",
        project_id="project-1",
        remote_project_id="remote-project-1",
        connection_version=datetime(2026, 8, 7, tzinfo=UTC),
        claim_token="claim-token",
        scenarios=[{"id": "scenario-current", "project_id": "remote-project-1", "status": "ready"}],
    )

    assert len(session.added) == 1
    assert isinstance(session.added[0], ApexLoadNativeScenario)
    assert len(session.executed) == 2
    schedule_update = session.executed[1]
    sql = str(schedule_update.compile(dialect=postgresql.dialect()))
    assert "UPDATE apex.apex_load_native_schedules" in sql
    assert "scenario_remote_id NOT IN" in sql


async def test_missing_native_run_is_staled_before_it_can_launch_a_pipeline() -> None:
    session = _RecordingSession()
    repository = ApexLoadNativeRepository(cast(async_sessionmaker[AsyncSession], lambda: session))

    await repository.sync_runs(
        connection_id="connection-1",
        project_id="project-1",
        remote_project_id="remote-project-1",
        connection_version=datetime(2026, 8, 7, tzinfo=UTC),
        claim_token="claim-token",
        runs=[{"id": "run-current", "project_id": "remote-project-1", "status": "COMPLETE"}],
    )

    assert len(session.executed) == 1
    update_statement = session.executed[0]
    compiled = update_statement.compile(dialect=postgresql.dialect())
    assert "UPDATE apex.apex_load_native_runs" in str(compiled)
    assert "remote_id NOT IN" in str(compiled)
    assert "missing" in compiled.params.values()
    assert "stale" in compiled.params.values()
    assert "native_run_missing" in compiled.params.values()
    assert "pipeline_status" in str(compiled)
    assert "launching" in compiled.params.values()


async def test_partial_native_run_snapshot_does_not_stale_unseen_runs() -> None:
    session = _RecordingSession()
    repository = ApexLoadNativeRepository(cast(async_sessionmaker[AsyncSession], lambda: session))

    await repository.sync_runs(
        connection_id="connection-1",
        project_id="project-1",
        remote_project_id="remote-project-1",
        connection_version=datetime(2026, 8, 7, tzinfo=UTC),
        claim_token="claim-token",
        runs=[{"id": "run-current", "project_id": "remote-project-1", "status": "COMPLETE"}],
        complete=False,
    )

    assert session.executed == []


async def test_reclaimed_connection_lease_fences_a_stale_snapshot_writer() -> None:
    session = _RecordingSession()

    async def stale_scalar(_statement: object) -> object:
        return SimpleNamespace(
            connection_version=datetime(2026, 8, 7, tzinfo=UTC),
            project_id="project-1",
            claim_token="newer-claim-token",
        )

    session.scalar = stale_scalar  # type: ignore[method-assign]
    repository = ApexLoadNativeRepository(cast(async_sessionmaker[AsyncSession], lambda: session))

    with pytest.raises(ValueError, match="claim is no longer current"):
        await repository.sync_runs(
            connection_id="connection-1",
            project_id="project-1",
            remote_project_id="remote-project-1",
            connection_version=datetime(2026, 8, 7, tzinfo=UTC),
            claim_token="stale-claim-token",
            runs=[
                {
                    "id": "run-current",
                    "project_id": "remote-project-1",
                    "status": "COMPLETE",
                }
            ],
        )

    assert session.added == []
    assert session.executed == []


async def test_stale_pipeline_launch_claims_are_recoverable_after_a_crash() -> None:
    session = _ClaimQuerySession()
    repository = ApexLoadNativeRepository(cast(async_sessionmaker[AsyncSession], lambda: session))

    assert await repository.claim_terminal_runs(
        exclude_run_ids=frozenset({"mirror-1"})
    ) == []

    assert session.statement is not None
    compiled = session.statement.compile(dialect=postgresql.dialect())  # type: ignore[union-attr]
    sql = str(compiled)
    assert "pipeline_status" in sql
    assert "claimed_at" in sql
    assert "launching" in compiled.params.values()
    assert ["pending", "retry"] in compiled.params.values()
    assert "apex_load_native_runs.id NOT IN" in sql
    assert ["mirror-1"] in compiled.params.values()


async def test_connection_claim_query_excludes_connections_seen_in_the_current_pass() -> None:
    session = _ClaimQuerySession()
    repository = ApexLoadNativeRepository(cast(async_sessionmaker[AsyncSession], lambda: session))

    assert await repository.claim_enabled_connections(
        limit=1,
        exclude_connection_ids=frozenset({"connection-1"}),
    ) == []

    assert session.statement is not None
    compiled = session.statement.compile(dialect=postgresql.dialect())  # type: ignore[union-attr]
    assert "connections.id NOT IN" in str(compiled)
    assert ["connection-1"] in compiled.params.values()


async def test_native_create_mapping_replays_only_the_original_request_fingerprint() -> None:
    fingerprint = "a" * 64
    row = SimpleNamespace(
        idempotency_fingerprint=fingerprint,
        project_id="project-1",
        status="ready",
        snapshot={"id": "scenario-1", "project_id": "remote-project-1"},
    )
    session = _ScalarSession(row)
    repository = ApexLoadNativeRepository(cast(async_sessionmaker[AsyncSession], lambda: session))

    replay = await repository.replay_created_resource(
        resource_kind="scenario",
        connection_id="connection-1",
        connection_version=datetime(2026, 8, 7, tzinfo=UTC),
        project_id="project-1",
        remote_project_id="remote-project-1",
        idempotency_key="scenario-create-1",
        idempotency_fingerprint=fingerprint,
    )

    assert replay == {
        "id": "scenario-1",
        "project_id": "remote-project-1",
        "idempotency_key": "scenario-create-1",
    }

    with pytest.raises(NativeResourceIdempotencyConflictError, match="another request"):
        await repository.replay_created_resource(
            resource_kind="scenario",
            connection_id="connection-1",
            connection_version=datetime(2026, 8, 7, tzinfo=UTC),
            project_id="project-1",
            remote_project_id="remote-project-1",
            idempotency_key="scenario-create-1",
            idempotency_fingerprint="b" * 64,
        )


async def test_native_create_mapping_accepts_provider_header_tokens() -> None:
    fingerprint = "a" * 64
    row = SimpleNamespace(
        idempotency_fingerprint=fingerprint,
        project_id="project-1",
        status="ready",
        snapshot={"id": "scenario-1", "project_id": "remote-project-1"},
    )
    repository = ApexLoadNativeRepository(
        cast(async_sessionmaker[AsyncSession], lambda: _ScalarSession(row))
    )

    replay = await repository.replay_created_resource(
        resource_kind="scenario",
        connection_id="connection-1",
        connection_version=datetime(2026, 8, 7, tzinfo=UTC),
        project_id="project-1",
        remote_project_id="remote-project-1",
        idempotency_key="scenario/retry?attempt=1@example",
        idempotency_fingerprint=fingerprint,
    )

    assert replay is not None
    assert replay["idempotency_key"] == "scenario/retry?attempt=1@example"
