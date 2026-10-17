"""Cycle evidence never promotes orchestration or missing metrics to a test pass."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from apex.routers import cycle_executions as api
from apex.services.cycle_results import compare_result, result_view


def run(*, passed=None, status="completed", kpis=None, breaches=None, **changes):
    values = dict(
        id="run",
        thread_id="thread",
        engine="apex-load",
        attempt=1,
        status=status,
        started_at=datetime.now(UTC),
        ended_at=None,
        summary={
            "engine": "apex-load",
            "passed": passed,
            "kpis": kpis or {},
            "sla_breaches": breaches or [],
        },
    )
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("passed,outcome", [(None, None), (False, "failed"), (True, "passed")])
def test_outcome_is_independent_of_completed_orchestration(passed, outcome):
    result = result_view(run(passed=passed))
    assert result.test_outcome == outcome
    assert all(m.value is None for m in result.metrics)
    assert result.bindings_verified is False


@pytest.mark.parametrize("status", ["running", "collecting", "failed", "aborted"])
@pytest.mark.parametrize("passed", [True, False, None])
@pytest.mark.parametrize("breaches", [[], ["Partial observed SLA breach"]])
def test_incomplete_or_stopped_execution_cannot_display_a_verdict(status, passed, breaches):
    result = result_view(run(passed=passed, status=status, breaches=breaches))
    assert result.test_outcome is None
    assert any("until execution completes" in warning for warning in result.warnings)


def test_breaches_override_contradictory_pass_with_explanation():
    result = result_view(run(passed=True, breaches=["Checkout p95 exceeded threshold"]))
    assert result.test_outcome == "failed"
    assert any("contradicts" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "summary",
    [None, {}, {"engine": "other", "passed": True}, {"engine": "apex-load", "passed": "true"}],
)
def test_invalid_and_conflicting_evidence_is_unavailable(summary):
    result = result_view(run(summary=summary))
    assert result.test_outcome is None
    assert result.evidence_status == "unavailable"


def test_zero_is_measured_but_invalid_metrics_are_unavailable():
    result = result_view(run(kpis={"tps_avg": 0, "p95_ms": -1, "error_rate": 2}))
    assert [m.value for m in result.metrics] == [0, None, None, None]


def test_comparison_differences_preserve_equal_missing_and_zero_baselines():
    baseline = result_view(run(id="baseline", kpis={"tps_avg": 10, "p95_ms": 100, "error_rate": 0}))
    candidate = result_view(run(kpis={"tps_avg": 10, "p95_ms": 120, "error_rate": 0.01}))
    result = compare_result(candidate, baseline)
    assert [(m.difference, m.percent_difference) for m in result.metrics] == [
        (0, 0),
        (20, 20),
        (0.01, None),
        (None, None),
    ]
    assert all(m.difference is None for m in candidate.metrics)
    assert result.comparison_available is True


@pytest.mark.parametrize("change", [{"engine": "loadrunner"}, {"status": "aborted"}])
def test_incompatible_attempts_are_not_compared(change):
    result = compare_result(
        result_view(run(kpis={"tps_avg": 20}, **change)),
        result_view(run(id="baseline", kpis={"tps_avg": 10})),
    )
    assert not result.comparison_available
    assert all(m.difference is None for m in result.metrics)


def test_extreme_percentage_is_unavailable_instead_of_infinity():
    result = compare_result(
        result_view(run(kpis={"tps_avg": 1})),
        result_view(run(id="baseline", kpis={"tps_avg": 1e-320})),
    )
    assert result.metrics[0].percent_difference is None
    result.model_dump_json()


@pytest.mark.asyncio
async def test_results_query_scopes_links_and_does_not_resolve_external_baseline(monkeypatch):
    monkeypatch.setattr(
        api,
        "find",
        AsyncMock(
            return_value=SimpleNamespace(id="cycle", project_id="project", app_id="app", revision=3)
        ),
    )
    session = AsyncMock()
    session.scalars.return_value = Mock(all=Mock(return_value=[run()]))
    with pytest.raises(HTTPException) as error:
        await api.read_results("cycle", session, Mock(), "unlinked-baseline")
    assert error.value.status_code == 404
    statement = session.scalars.await_args.args[0]
    assert statement.compile().params == {
        "cycle_id_1": "cycle",
        "project_id_1": "project",
        "app_id_1": "app",
    }
    assert "ownership_known IS true" in str(statement)
    session.commit.assert_not_awaited()
