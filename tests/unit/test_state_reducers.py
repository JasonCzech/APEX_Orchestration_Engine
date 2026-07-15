from types import SimpleNamespace

import pytest

from apex.domain.pipeline import MAX_TOOL_CALL_RECORDS
from apex.graphs.pipeline.state import (
    MAX_DURABLE_ARTIFACTS,
    MAX_DURABLE_DIALOGUE_ENTRIES,
    MAX_DURABLE_PHASE_DIAGNOSTICS,
    append_unique_by_id,
    merge_artifacts,
    merge_context_packets,
    merge_dialogue,
    merge_phase_results,
)
from apex.services import run_validation


def test_append_unique_by_id_dedupes() -> None:
    left = [{"id": "a", "v": 1}]
    right = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    assert append_unique_by_id(left, right) == [{"id": "a", "v": 1}, {"id": "b", "v": 2}]


def test_append_unique_handles_none() -> None:
    assert append_unique_by_id(None, [{"id": "x"}]) == [{"id": "x"}]
    assert append_unique_by_id([{"id": "x"}], None) == [{"id": "x"}]


def test_artifact_reducer_retains_bounded_recent_unique_window() -> None:
    artifacts = [{"id": f"artifact-{index}"} for index in range(MAX_DURABLE_ARTIFACTS + 2)]

    merged = merge_artifacts(artifacts[:-1], artifacts[-1:])

    assert len(merged) == MAX_DURABLE_ARTIFACTS
    assert merged[0]["id"] == "artifact-2"
    assert merged[-1]["id"] == f"artifact-{MAX_DURABLE_ARTIFACTS + 1}"


def test_dialogue_reducer_prunes_old_attempts_and_caps_global_tail() -> None:
    old = [
        {
            "id": f"story_analysis-a1-d{index}",
            "phase": "story_analysis",
            "attempt": 1,
        }
        for index in range(MAX_DURABLE_DIALOGUE_ENTRIES)
    ]
    current = [
        {
            "id": "story_analysis-a2-d0",
            "phase": "story_analysis",
            "attempt": 2,
        },
        {"id": "reporting-a1-d0", "phase": "reporting", "attempt": 1},
    ]

    merged = merge_dialogue(old, current)

    assert [entry["id"] for entry in merged] == [
        "story_analysis-a2-d0",
        "reporting-a1-d0",
    ]


def test_context_packet_reducer_replaces_latest_id_before_enforcing_count_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_validation,
        "get_settings",
        lambda: SimpleNamespace(
            runs=SimpleNamespace(max_context_packets=2, max_context_chars_total=10_000)
        ),
    )
    packets = [
        {"id": f"packet-{index}", "source": "test", "title": f"Packet {index}"}
        for index in range(2)
    ]

    replaced = merge_context_packets(
        packets,
        [{"id": "packet-0", "source": "test", "title": "Replacement"}],
    )

    assert len(replaced) == 2
    assert replaced[0]["title"] == "Replacement"
    with pytest.raises(ValueError, match="context_packets exceeds"):
        merge_context_packets(
            replaced,
            [{"id": "packet-overflow", "source": "test", "title": "Overflow"}],
        )


def test_context_packet_reducer_enforces_aggregate_serialized_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_validation,
        "get_settings",
        lambda: SimpleNamespace(
            runs=SimpleNamespace(max_context_packets=4, max_context_chars_total=1_000)
        ),
    )
    first = {"id": "first", "source": "test", "title": "First", "text": "a" * 500}
    second = {"id": "second", "source": "test", "title": "Second", "text": "b" * 500}

    with pytest.raises(ValueError, match="rendered payload exceeds"):
        merge_context_packets([first], [second])


def test_merge_same_attempt_unions_lists_and_overwrites_scalars() -> None:
    left = {
        "test_planning": {
            "attempt": 1,
            "status": "running",
            "approvals": [{"id": "ap1", "action": "approve"}],
            "tool_calls": [],
            "warnings": ["w1"],
            "errors": [],
            "artifact_ids": [],
        }
    }
    right = {
        "test_planning": {
            "attempt": 1,
            "status": "succeeded",
            "approvals": [{"id": "ap1", "action": "approve"}, {"id": "ap2", "action": "approve"}],
            "tool_calls": [{"id": "tc1", "tool": "jira.search"}],
            "warnings": ["w1", "w2"],
            "errors": [],
            "artifact_ids": ["art1"],
        }
    }
    merged = merge_phase_results(left, right)["test_planning"]
    assert merged["status"] == "succeeded"
    assert [a["id"] for a in merged["approvals"]] == ["ap1", "ap2"]
    assert merged["warnings"] == ["w1", "w2"]
    assert merged["artifact_ids"] == ["art1"]


def test_merge_new_attempt_replaces_wholesale() -> None:
    left = {"execution": {"attempt": 1, "status": "failed", "warnings": ["old"]}}
    right = {"execution": {"attempt": 2, "status": "running", "warnings": []}}
    merged = merge_phase_results(left, right)["execution"]
    assert merged["attempt"] == 2
    assert merged["warnings"] == []


def test_merge_preserves_untouched_phases() -> None:
    left = {"story_analysis": {"attempt": 1, "status": "succeeded"}}
    right = {"env_triage": {"attempt": 1, "status": "running"}}
    merged = merge_phase_results(left, right)
    assert set(merged) == {"story_analysis", "env_triage"}


def test_merge_caps_durable_tool_call_history() -> None:
    left = {
        "reporting": {
            "attempt": 1,
            "tool_calls": [
                {"id": f"old-{index}", "tool": "fetch"} for index in range(MAX_TOOL_CALL_RECORDS)
            ],
        }
    }
    right = {
        "reporting": {
            "attempt": 1,
            "tool_calls": [{"id": "new", "tool": "fetch"}],
        }
    }

    calls = merge_phase_results(left, right)["reporting"]["tool_calls"]

    assert len(calls) == MAX_TOOL_CALL_RECORDS
    assert calls[-1]["id"] == "new"


def test_merge_caps_phase_diagnostics_on_merge_and_new_attempt() -> None:
    oversized = [f"diagnostic-{index}" for index in range(MAX_DURABLE_PHASE_DIAGNOSTICS + 2)]

    initial = merge_phase_results(
        {},
        {"execution": {"attempt": 1, "errors": oversized}},
    )["execution"]
    replacement = merge_phase_results(
        {"execution": {"attempt": 1, "warnings": ["old"]}},
        {"execution": {"attempt": 2, "warnings": oversized}},
    )["execution"]

    assert initial["errors"] == oversized[-MAX_DURABLE_PHASE_DIAGNOSTICS:]
    assert replacement["warnings"] == oversized[-MAX_DURABLE_PHASE_DIAGNOSTICS:]
