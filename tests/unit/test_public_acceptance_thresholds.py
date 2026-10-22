"""The public execution entry names the thresholds its result was judged against."""

import json

import pytest

from apex.services.pipeline_public import public_pipeline_state

SUMMARY = {
    "engine": "sim",
    "passed": True,
    "kpis": {"p95_ms": 212.0, "error_rate": 0.004, "tps_avg": 18.0},
    "sla_breaches": [],
    "notes": "simulated run; KPIs are synthetic",
}


def _execution(entry: dict) -> dict:
    state = public_pipeline_state(
        {"phase_results": {"execution": {"status": "succeeded", "attempt": 1, **entry}}}
    )
    return state["phase_results"]["execution"]


def test_applied_thresholds_come_from_the_durable_engine_handle() -> None:
    execution = _execution(
        {
            "test_summary": SUMMARY,
            "engine_handle": {
                "engine": "sim",
                "external_run_id": "sim-1",
                "extras": {"slas": json.dumps({"p95_ms": 500, "error_rate": 0.05})},
            },
            "load_test_spec": {"title": "t", "slas": {"p95_ms": 999}},
        }
    )
    assert execution["acceptance_thresholds"] == {"p95_ms": 500.0, "error_rate": 0.05}
    assert "engine_handle" not in execution


def test_apex_load_falls_back_to_the_spec_it_sent_to_the_provider() -> None:
    execution = _execution(
        {
            "engine": "apex_load",
            "test_summary": SUMMARY,
            "load_test_spec": {"title": "t", "slas": {"tps_avg": 10}},
        }
    )
    assert execution["acceptance_thresholds"] == {"tps_avg": 10.0}


@pytest.mark.parametrize(
    "entry",
    [
        # LoadRunner applies its own native SLA settings, never the spec's.
        {"engine": "loadrunner", "load_test_spec": {"title": "t", "slas": {"p95_ms": 500}}},
        # Legacy sim handles kept an unqualified pass; do not invent criteria.
        {
            "engine": "sim",
            "engine_handle": {"engine": "sim", "extras": {}},
            "load_test_spec": {"title": "t", "slas": {"p95_ms": 500}},
        },
        # An empty applied set means nothing was evaluated.
        {
            "engine": "apex_load",
            "engine_handle": {"engine": "apex_load", "extras": {"slas": "{}"}},
            "load_test_spec": {"title": "t", "slas": {"p95_ms": 500}},
        },
    ],
)
def test_unapplied_spec_thresholds_are_not_presented_as_applied(entry) -> None:
    assert "acceptance_thresholds" not in _execution({"test_summary": SUMMARY, **entry})


@pytest.mark.parametrize(
    "slas",
    [
        {"p95_ms": float("inf")},
        {"p95 ms<script>": 1},
        {"p95_mś": 1},
        {"p95_ms": 10**400},
        {"p95_ms": "fast"},
        {},
        "not-json",
    ],
)
def test_unsafe_or_empty_thresholds_are_omitted(slas) -> None:
    extras = {"slas": slas if isinstance(slas, str) else json.dumps(slas, allow_nan=True)}
    execution = _execution(
        {"test_summary": SUMMARY, "engine_handle": {"engine": "sim", "extras": extras}}
    )
    assert "acceptance_thresholds" not in execution
    assert execution["test_summary"]["kpis"]["p95_ms"] == 212.0


def test_thresholds_need_a_result_to_compare_against() -> None:
    execution = _execution({"load_test_spec": {"title": "t", "slas": {"p95_ms": 500}}})
    assert "acceptance_thresholds" not in execution
