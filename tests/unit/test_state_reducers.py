from apex.graphs.pipeline.state import append_unique_by_id, merge_phase_results


def test_append_unique_by_id_dedupes() -> None:
    left = [{"id": "a", "v": 1}]
    right = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    assert append_unique_by_id(left, right) == [{"id": "a", "v": 1}, {"id": "b", "v": 2}]


def test_append_unique_handles_none() -> None:
    assert append_unique_by_id(None, [{"id": "x"}]) == [{"id": "x"}]
    assert append_unique_by_id([{"id": "x"}], None) == [{"id": "x"}]


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
