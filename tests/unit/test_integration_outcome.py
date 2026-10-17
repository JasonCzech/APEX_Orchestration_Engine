from copy import deepcopy

import pytest

from apex.domain.integration_outcome import IntegrationOutcome
from apex.services.public_projection import public_test_result_summary

OUTCOME = {
    "schema_version": 1,
    "execution_id": "run-1",
    "observed_at": "2026-09-09T12:00:00Z",
    "assessment": {
        "status": "not_assessed",
        "reason_code": "no_criteria",
        "explanation": "No criteria configured",
        "criteria_revision": None,
    },
    "execution": {
        "termination_reason": "completed",
        "planned_duration_seconds": 300,
        "observed_duration_seconds": 290,
    },
    "evidence": {"status": "partial", "coverage_percent": 80},
}


@pytest.mark.parametrize(
    "status,passed",
    [
        ("passed", True),
        ("failed", False),
        ("not_assessed", None),
        ("inconclusive", None),
        ("unavailable", None),
    ],
)
def test_structured_assessment_overrides_legacy_boolean(status, passed):
    outcome = deepcopy(OUTCOME)
    outcome["assessment"]["status"] = status
    summary = public_test_result_summary(
        {"engine": "apex_load", "passed": True, "outcome": outcome}
    )
    assert summary is not None
    assert summary["passed"] is passed
    assert summary["outcome"]["assessment"]["status"] == status


def test_unknown_contract_does_not_surface_legacy_pass():
    outcome = {**OUTCOME, "schema_version": 2}
    summary = public_test_result_summary(
        {"engine": "apex_load", "passed": True, "outcome": outcome}
    )
    assert summary is not None
    assert summary["passed"] is None
    assert "outcome" not in summary


@pytest.mark.parametrize(
    "field,value", [("observed_at", "2026-09-09T12:00:00"), ("schema_version", 2)]
)
def test_contract_rejects_ambiguous_time_and_unknown_version(field, value):
    with pytest.raises(ValueError):
        IntegrationOutcome.model_validate({**OUTCOME, field: value})


def test_structured_assessment_survives_execution_checkpoint_validation():
    from apex.domain.integrations import TestResultSummary
    from apex.graphs.pipeline.execution_phase import _validated_engine_summary
    from apex.graphs.pipeline.phase_subgraph import _validated_checkpoint_summary

    source = TestResultSummary(
        engine="apex_load", passed=None, outcome=IntegrationOutcome.model_validate(OUTCOME)
    )
    checked = _validated_engine_summary(source, expected_engine="apex_load")
    restored = _validated_checkpoint_summary(checked.model_dump(mode="json"))
    assert restored is not None
    assert restored.outcome is not None
    assert restored.outcome.assessment.status == "not_assessed"


def test_imported_outcome_survives_analysis_seeding():
    from apex.graphs.pipeline.graph import _external_seed

    entry, _ = _external_seed(
        {"source": "APEX Load", "engine": "apex_load", "passed": True, "outcome": OUTCOME},
        attempt=1,
    )
    assert entry["test_summary"]["passed"] is None
    assert entry["test_summary"]["outcome"]["assessment"]["status"] == "not_assessed"
    assert entry["source_outcome_explicit"] is True


def test_contract_does_not_coerce_boolean_version_or_string_coverage():
    with pytest.raises(ValueError):
        IntegrationOutcome.model_validate({**OUTCOME, "schema_version": True})
    with pytest.raises(ValueError):
        IntegrationOutcome.model_validate(
            {**OUTCOME, "evidence": {"status": "partial", "coverage_percent": "80"}}
        )


@pytest.mark.parametrize(
    "status,expected",
    [
        ("passed", True),
        ("failed", False),
        ("not_assessed", None),
        ("inconclusive", None),
        ("unavailable", None),
    ],
)
def test_engine_boundary_uses_structured_assessment_for_execution_decisions(status, expected):
    from apex.graphs.pipeline.execution_phase import _validated_engine_summary

    outcome = deepcopy(OUTCOME)
    outcome["assessment"]["status"] = status
    checked = _validated_engine_summary(
        {"engine": "apex_load", "passed": status != "passed", "outcome": outcome}
    )
    assert checked.passed is expected
    from apex.graphs.pipeline.phase_subgraph import _validated_checkpoint_summary

    restored = _validated_checkpoint_summary(
        {"engine": "apex_load", "passed": status != "passed", "outcome": outcome}
    )
    assert restored is not None
    assert restored.passed is expected
