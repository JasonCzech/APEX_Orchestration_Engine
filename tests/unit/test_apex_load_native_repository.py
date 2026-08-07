"""APEX Load native-control durable mirror regression tests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apex.persistence.models import ApexLoadNativeScenario
from apex.persistence.repositories.apex_load_native import ApexLoadNativeRepository, _snapshot


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
        return SimpleNamespace() if self.scalar_calls == 1 else None

    def add(self, row: object) -> None:
        self.added.append(row)

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)

    async def commit(self) -> None:
        return None


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
