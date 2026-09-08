"""Exact bindings and fail-closed preflight regressions."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from apex.domain.cycle_executions import PrepareExecution, execution_checks, prepare_snapshot
from apex.domain.cycles import version_digest
from apex.domain.deliverables import DELIVERABLE_FIELDS, content_issues
from apex.persistence.models import TestCycle as CycleRow
from apex.routers.cycle_executions import reserve_revision, run_scope


def approved(kind, identifier, **content):
    values = {
        f.key: (f.minimum or 1)
        if f.control == "number"
        else f.options[0]
        if f.options
        else "Documented"
        for f in DELIVERABLE_FIELDS[kind]
        if f.required
    }
    values.update(content)
    return dict(
        id=identifier,
        kind=kind,
        title=kind,
        content=values,
        checksum=version_digest(values),
        status="approved",
        archived=False,
        dependencies=[] if kind == "test_plan" else ["plan"],
        reviews=[],
    )


@pytest.fixture
def versions():
    return [
        approved("test_plan", "plan", vusers=10, ramp_s=1, duration_s=60, p95_ms_threshold=500),
        approved("script", "script", source='{"steps": ["example"]}'),
        approved(
            "readiness", "readiness", status="ready", inventory_snapshot_id="scan", target_version=2
        ),
    ]


def snapshot(versions, **changes):
    return prepare_snapshot(
        PrepareExecution(
            expected_revision=1,
            idempotency_key="request",
            plan_version_id="plan",
            script_version_id="script",
            readiness_version_id="readiness",
            **changes,
        ),
        versions,
        mode="live",
        environment_id="env",
        environment_version=2,
        connection_version="generation",
    )


def checks(prepared, versions, **overrides):
    now = datetime.now(UTC)
    args: dict[str, Any] = dict(
        target_version=2,
        target_valid=True,
        connection_valid=True,
        connection_version="generation",
        inventory_id="scan",
        inventory_at=now,
        now=now,
    )
    args.update(overrides)
    return {c.key: c.status for c in execution_checks(prepared, versions, **args)}


def test_preparation_freezes_workload_and_content_without_claiming_launch_ready(versions):
    before = deepcopy(versions)
    prepared = snapshot(versions)
    assert versions == before
    assert prepared.workload == {
        "vusers": 10,
        "ramp_s": 1,
        "duration_s": 60,
        "slas": {"p95_ms": 500},
    }
    result = checks(prepared, versions)
    assert result["workload"] == result["readiness_evidence"] == "ready"
    assert result["provider_access"] == "not_checked"
    assert result["script_binding"] == result["launch_authority"] == "blocked"
    versions[0]["content"]["vusers"] = 20
    assert prepared.workload["vusers"] == 10
    assert checks(prepared, versions)["plan"] == "blocked"


@pytest.mark.parametrize("kind", [0, 1, 2])
def test_changed_approved_content_cannot_be_prepared(versions, kind):
    versions[kind]["content"]["tampered"] = True
    with pytest.raises(ValueError):
        snapshot(versions)


def test_script_must_reference_exact_selected_plan(versions):
    versions[1]["dependencies"] = []
    with pytest.raises(ValueError, match="reference"):
        snapshot(versions)


def test_native_launch_authority_cannot_be_assigned_to_loadrunner(versions):
    versions[1] = approved(
        "script", "script", engine="loadrunner", format="web_http_html", source="source"
    )
    with pytest.raises(ValueError, match="Native schedule"):
        snapshot(versions, launch_authority="native_schedule")


@pytest.mark.parametrize(
    "override,key",
    [
        ({"target_version": 3}, "target"),
        ({"target_valid": False}, "target"),
        ({"connection_version": "new"}, "connection"),
        ({"connection_valid": False}, "connection"),
        ({"inventory_id": "rescan"}, "readiness_evidence"),
        ({"inventory_at": None}, "readiness_evidence"),
        ({"inventory_at": datetime.now(UTC) - timedelta(days=2)}, "readiness_evidence"),
        ({"inventory_at": datetime.now(UTC) + timedelta(days=1)}, "readiness_evidence"),
    ],
)
def test_changed_or_stale_prerequisites_block(versions, override, key):
    assert checks(snapshot(versions), versions, **override)[key] == "blocked"


def test_unknown_thresholds_and_unsupported_arrival_rate_are_blockers(versions):
    content = versions[0]["content"]
    del content["p95_ms_threshold"]
    content["arrival_rate"] = 5
    versions[0]["checksum"] = version_digest(content)
    result = checks(snapshot(versions), versions)
    assert result["thresholds"] == result["arrival_rate"] == "blocked"


def test_superseded_plan_invalidates_prepared_script(versions):
    prepared = snapshot(versions)
    versions.append({**versions[0], "id": "replacement", "replaces": "plan"})
    result = checks(prepared, versions)
    assert result["plan"] == result["script"] == "blocked"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_threshold_cannot_pass_review(versions, value):
    content = {**versions[0]["content"], "p95_ms_threshold": value}
    assert any("latency limit" in issue for issue in content_issues("test_plan", content))


@pytest.mark.asyncio
async def test_execution_mutations_require_database_revision_cas():
    session = AsyncMock()
    session.scalar.return_value = None
    with pytest.raises(HTTPException) as error:
        await reserve_revision(session, CycleRow(id="cycle"), 2)
    assert error.value.status_code == 409
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    statement = session.scalar.call_args.args[0].compile(dialect=postgresql.dialect())
    assert statement.params["revision_1"] == 2


def test_existing_run_links_require_exact_scope_and_verified_ownership():
    clauses = run_scope(CycleRow(project_id="project", app_id="app"))
    sql = " ".join(str(c.compile(dialect=postgresql.dialect())) for c in clauses)
    assert "project_id" in sql and "app_id" in sql
    assert "ownership_known IS true" in sql and "scope_ownership_known IS true" in sql


@pytest.mark.asyncio
async def test_idempotent_preparation_returns_existing_without_revalidating_or_mutating(
    monkeypatch,
):
    from types import SimpleNamespace

    from apex.routers import cycle_executions as api

    body = PrepareExecution(
        expected_revision=1,
        idempotency_key="request",
        plan_version_id="plan",
        script_version_id="script",
    )
    row = SimpleNamespace(
        id="prepared",
        request_fingerprint=version_digest(
            body.model_dump(exclude={"expected_revision", "idempotency_key"})
        ),
    )
    session = AsyncMock()
    session.scalar.return_value = row
    monkeypatch.setattr(api, "find", AsyncMock(return_value=CycleRow(id="cycle", revision=9)))
    result = await api.prepare_execution("cycle", body, session, Mock(consumer_id="operator"))
    assert result.id == "prepared" and result.revision == 9
    session.commit.assert_not_awaited()
    row.request_fingerprint = "different"
    with pytest.raises(HTTPException) as error:
        await api.prepare_execution("cycle", body, session, Mock(consumer_id="operator"))
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_unscoped_or_unknown_run_is_rejected_before_link_mutation(monkeypatch):

    from apex.domain.cycle_executions import LinkCycleRun
    from apex.routers import cycle_executions as api

    session = AsyncMock()
    session.scalar.return_value = None
    monkeypatch.setattr(
        api, "find", AsyncMock(return_value=CycleRow(id="cycle", project_id="p", app_id="a"))
    )
    with pytest.raises(HTTPException) as error:
        await api.link_run(
            "cycle",
            LinkCycleRun(expected_revision=1, engine_run_id="other-run"),
            session,
            Mock(consumer_id="operator"),
        )
    assert error.value.status_code == 404
    session.commit.assert_not_awaited()
