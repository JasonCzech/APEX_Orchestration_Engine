from types import SimpleNamespace

import pytest

from apex.domain.pipeline import MAX_TOOL_CALL_RECORDS
from apex.graphs.pipeline.state import (
    MAX_DURABLE_APPROVALS,
    MAX_DURABLE_ARTIFACTS,
    MAX_DURABLE_DIALOGUE_ENTRIES,
    MAX_DURABLE_PHASE_DIAGNOSTICS,
    append_unique_by_id,
    merge_application_reviews,
    merge_artifacts,
    merge_context_packets,
    merge_dialogue,
    merge_phase_results,
    merge_prompt_reviews,
)
from apex.services import run_validation


def test_append_unique_by_id_dedupes() -> None:
    left = [{"id": "a", "v": 1}]
    right = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    assert append_unique_by_id(left, right) == [{"id": "a", "v": 1}, {"id": "b", "v": 2}]


def test_append_unique_handles_none() -> None:
    assert append_unique_by_id(None, [{"id": "x"}]) == [{"id": "x"}]
    assert append_unique_by_id([{"id": "x"}], None) == [{"id": "x"}]


def test_append_unique_canonicalizes_null_ids_and_repairs_inherited_duplicates() -> None:
    legacy = {"id": None, "value": "legacy"}

    first = append_unique_by_id([legacy, legacy], [])

    assert len(first) == 1
    assert type(first[0]["id"]) is str
    assert first[0]["id"].startswith("derived-")
    assert append_unique_by_id(first, [legacy]) == first


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ([{"value": "legacy"}], [{"id": None, "value": "legacy"}]),
        ([{"id": None, "value": "legacy"}], [{"value": "legacy"}]),
    ],
)
def test_append_unique_treats_missing_and_null_ids_as_the_same_entry(
    left: list[dict[str, object]],
    right: list[dict[str, object]],
) -> None:
    merged = append_unique_by_id(left, right)

    assert len(merged) == 1
    assert merged[0]["value"] == "legacy"
    assert type(merged[0]["id"]) is str
    assert merged[0]["id"].startswith("derived-")


def test_append_unique_dedupes_duplicate_ids_already_on_one_side() -> None:
    assert append_unique_by_id(
        [{"id": "same", "value": "first"}, {"id": "same", "value": "second"}],
        [],
    ) == [{"id": "same", "value": "first"}]


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


def test_merge_revalidates_untouched_inherited_phase() -> None:
    with pytest.raises(ValueError, match="credential material") as raised:
        merge_phase_results(
            {
                "story_analysis": {
                    "attempt": 1,
                    "summary": "Authorization: Bearer inherited-sibling-secret-canary",
                },
                "execution": {"attempt": 1, "status": "running"},
            },
            {"execution": {"attempt": 1, "status": "succeeded"}},
        )

    assert "inherited-sibling-secret-canary" not in str(raised.value)


@pytest.mark.parametrize("incoming_attempt", [1, 4])
def test_merge_rejects_attempt_rollback_or_jump(incoming_attempt: int) -> None:
    with pytest.raises(ValueError, match="attempt is not monotonic"):
        merge_phase_results(
            {"execution": {"attempt": 2, "status": "running"}},
            {"execution": {"attempt": incoming_attempt, "status": "running"}},
        )


@pytest.mark.parametrize("incoming_status", ["running", "failed", "aborted"])
def test_merge_does_not_reopen_or_replace_terminal_attempt(incoming_status: str) -> None:
    with pytest.raises(ValueError, match="terminal phase status is immutable"):
        merge_phase_results(
            {"execution": {"attempt": 2, "status": "succeeded"}},
            {"execution": {"attempt": 2, "status": incoming_status}},
        )


def test_merge_allows_exact_terminal_replay_to_enrich_diagnostics() -> None:
    merged = merge_phase_results(
        {"execution": {"attempt": 2, "status": "failed"}},
        {
            "execution": {
                "attempt": 2,
                "status": "failed",
                "errors": ["provider failed"],
            }
        },
    )

    assert merged["execution"]["status"] == "failed"
    assert merged["execution"]["errors"] == ["provider failed"]


def test_merge_rejects_entry_bound_to_another_phase() -> None:
    with pytest.raises(ValueError, match="bound to another phase"):
        merge_phase_results(
            {"story_analysis": {"phase": "execution", "attempt": 1}},
            {"execution": {"attempt": 1}},
        )


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


def test_phase_reducer_fails_closed_on_credential_bearing_current_diagnostics() -> None:
    with pytest.raises(ValueError, match="credential material") as raised:
        merge_phase_results(
            {
                "execution": {
                    "attempt": 1,
                    "warnings": ["Authorization: Bearer reducer-secret-canary"],
                }
            },
            {"execution": {"attempt": 1, "status": "succeeded", "warnings": []}},
        )

    assert "reducer-secret-canary" not in str(raised.value)


def test_id_bearing_reducer_entries_are_validated_and_approvals_are_bounded() -> None:
    with pytest.raises(ValueError, match="credential material") as raised:
        append_unique_by_id(
            [],
            [
                {
                    "id": "approval-1",
                    "note": "password=reducer-entry-secret-canary",
                }
            ],
        )
    assert "reducer-entry-secret-canary" not in str(raised.value)

    approvals = [{"id": f"approval-{index}"} for index in range(MAX_DURABLE_APPROVALS + 10)]
    merged = merge_phase_results(
        {},
        {"execution": {"attempt": 1, "approvals": approvals}},
    )["execution"]
    assert len(merged["approvals"]) == MAX_DURABLE_APPROVALS
    assert merged["approvals"][0]["id"] == "approval-10"


def test_phase_reducer_validates_whole_entry_before_copy_or_merge() -> None:
    oversized = {f"field-{index}": index for index in range(129)}
    with pytest.raises(ValueError, match="reducer entry is invalid"):
        merge_phase_results({}, {"execution": oversized})

    with pytest.raises(ValueError, match="credential material") as raised:
        merge_phase_results(
            {"execution": {"attempt": 1, "summary": "password=phase-secret-canary"}},
            {"execution": {"attempt": 1, "status": "succeeded"}},
        )

    assert "phase-secret-canary" not in str(raised.value)


def test_phase_reducer_rejects_unknown_or_oversized_phase_maps() -> None:
    with pytest.raises(ValueError, match="phase name is invalid"):
        merge_phase_results({}, {"attacker_phase": {"attempt": 1}})

    oversized = {
        **{
            phase: {"attempt": 1}
            for phase in (
                "story_analysis",
                "test_planning",
                "env_triage",
                "script_scenario",
                "execution",
                "reporting",
                "postmortem",
            )
        },
        "extra": {"attempt": 1},
    }
    with pytest.raises(ValueError, match="phase name is invalid"):
        merge_phase_results(oversized, {})


def test_review_reducers_reject_poisoned_current_state_before_merge() -> None:
    with pytest.raises(ValueError, match="credential material") as raised:
        merge_prompt_reviews(
            {"story_analysis": {"system": "Authorization: Bearer reducer-review-secret-canary"}},
            {"reporting": {"system": "safe"}},
        )
    assert "reducer-review-secret-canary" not in str(raised.value)

    with pytest.raises(ValueError, match="application prompt reviews are invalid"):
        merge_application_reviews(
            {"app-1": {"content": "one"}, "app-2": {"content": "two"}},
            {"app-3": {"content": "three"}},
        )

    assert (
        merge_application_reviews(
            {"app-1": {"content": "password=legacy-secret-canary"}},
            {},
        )
        == {}
    )


def test_review_reducers_reject_unknown_phase_and_oversized_entry() -> None:
    with pytest.raises(ValueError, match="prompt reviews key is invalid"):
        merge_prompt_reviews({}, {"unknown": {"system": "safe"}})

    with pytest.raises(ValueError, match="prompt review is too large"):
        merge_prompt_reviews(
            {},
            {"story_analysis": {f"field-{index}": "x" * 13_000 for index in range(31)}},
        )
