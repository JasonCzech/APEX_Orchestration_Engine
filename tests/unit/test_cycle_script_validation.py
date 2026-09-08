"""Provider validation status, recovery and stale-acceptance regressions."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apex.domain.cycle_executions import ExecutionSnapshot
from apex.domain.integrations import ValidationReport
from apex.persistence.models import CycleScriptValidation
from apex.routers import cycle_script_validation as api


def snapshot():
    return ExecutionSnapshot(
        plan_version_id="plan",
        plan_title="Plan",
        plan_checksum="plan-checksum",
        script_version_id="script",
        script_title="Script",
        script_checksum="script-checksum",
        script_source_checksum="source-checksum",
        script_format="apex_native_json",
        readiness_version_id=None,
        readiness_checksum=None,
        engine="apex-load",
        mode="live",
        environment_id="environment",
        environment_version=1,
        connection_id="engine",
        connection_version="2026-09-07T12:00:00+00:00",
        launch_authority="pipeline",
        workload={},
        script_artifact_id="artifact",
    )


def row(status="accepted", age=timedelta(0)):
    now = datetime.now(UTC)
    return CycleScriptValidation(
        id="validation",
        cycle_id="cycle",
        preparation_id="prep",
        request_key="request",
        status=status,
        issues=[],
        warnings=[],
        created_by="operator",
        created_at=now - age,
        finished_at=now - age if status != "pending" else None,
    )


@pytest.mark.parametrize(
    "status,age,blockers,current",
    [
        ("accepted", timedelta(), [], True),
        ("accepted", timedelta(days=2), [], False),
        ("accepted", timedelta(days=-1), [], False),
        ("accepted", timedelta(), ["Changed source"], False),
        ("rejected", timedelta(), [], False),
        ("unavailable", timedelta(), [], False),
        ("pending", timedelta(), [], False),
    ],
)
def test_acceptance_is_not_current_when_stale_or_unavailable(status, age, blockers, current):
    assert api.validation_view(row(status, age), blockers).current is current


@pytest.mark.asyncio
async def test_simulation_and_loadrunner_have_actionable_blockers(monkeypatch):
    cycle = SimpleNamespace(id="cycle", project_id="project", versions=[])
    prepared = snapshot().model_copy(
        update={"mode": "simulation", "engine": "loadrunner", "script_format": "web_http_html"}
    )
    monkeypatch.setattr(api, "find", AsyncMock(return_value=cycle))
    session = AsyncMock()
    session.scalar.side_effect = [SimpleNamespace(snapshot=prepared.model_dump()), None, None]
    _, _, _, blockers = await api.validation_context("cycle", "prep", session, Mock())
    assert any("Simulation" in b for b in blockers)
    assert any("LoadRunner" in b for b in blockers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report,expected",
    [
        (ValidationReport(ok=True), "accepted"),
        (ValidationReport(ok=False), "rejected"),
        (ValidationReport(), "unavailable"),
        (ValidationReport(ok=True, issues=["Contradictory issue"]), "rejected"),
    ],
)
async def test_pending_attempt_resumes_without_launch_and_requires_explicit_outcome(
    monkeypatch, report, expected
):
    prepared = snapshot()
    record = row("pending")
    artifact = SimpleNamespace(artifact_key="stored-source")
    cycle = SimpleNamespace(id="cycle", project_id="project", revision=5)
    monkeypatch.setattr(
        api, "validation_context", AsyncMock(return_value=(cycle, prepared, artifact, []))
    )
    monkeypatch.setattr(api, "latest_validation_id", AsyncMock(return_value=record.id))
    session = AsyncMock()
    session.expunge = Mock()
    session.scalar.return_value = record

    async def record_status(statement):
        values = statement.compile().params
        record.status = values["status"]
        record.issues = values["issues"]
        record.warnings = values["warnings"]
        record.finished_at = values["finished_at"]

    session.execute.side_effect = record_status
    engine = Mock(validate_script_source=AsyncMock(return_value=report), aclose=AsyncMock())
    store = Mock(aclose=AsyncMock())
    resolver = AsyncMock()
    resolver.resolve_metadata.return_value = SimpleNamespace(
        persisted=True,
        config=SimpleNamespace(id="engine"),
        connection_version=datetime.fromisoformat(prepared.connection_version or ""),
        internal_project_id="project",
    )
    resolver.build_from_metadata.return_value = SimpleNamespace(adapter=engine)
    monkeypatch.setattr(api, "resolve_store", AsyncMock(return_value=store))
    monkeypatch.setattr(api, "read_verified_script", AsyncMock(return_value=b'{"name":"exact"}'))
    result = await api.validate_script(
        "cycle",
        "prep",
        api.ValidateCycleScript(expected_revision=1, idempotency_key="request"),
        session,
        Mock(),
        resolver,
    )
    assert result.status == expected
    engine.validate_script_source.assert_awaited_once_with('{"name":"exact"}')
    engine.provision.assert_not_called()
    engine.start.assert_not_called()
    engine.aclose.assert_awaited_once()
    store.aclose.assert_awaited_once()
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_idempotent_replay_does_not_contact_provider(monkeypatch):
    record = row()
    monkeypatch.setattr(
        api,
        "validation_context",
        AsyncMock(return_value=(SimpleNamespace(id="cycle"), snapshot(), None, [])),
    )
    monkeypatch.setattr(api, "latest_validation_id", AsyncMock(return_value="newer-attempt"))
    session = AsyncMock()
    session.scalar.return_value = record
    resolver = AsyncMock()
    result = await api.validate_script(
        "cycle",
        "prep",
        api.ValidateCycleScript(expected_revision=1, idempotency_key="request"),
        session,
        Mock(),
        resolver,
    )
    assert result.status == "accepted" and result.current is False
    resolver.resolve_metadata.assert_not_awaited()
    session.commit.assert_not_awaited()
