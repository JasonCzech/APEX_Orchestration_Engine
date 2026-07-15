"""Unit tests for gate payload builders, decision parsing, and approval attribution."""

from apex.domain.pipeline import Phase
from apex.graphs.pipeline.gates import (
    PHASE_REVIEW_ACTIONS,
    PROMPT_REVIEW_ACTIONS,
    build_phase_review_payload,
    build_prompt_review_payload,
    make_approval,
    parse_gate_decision,
    resolve_actor,
)


def test_prompt_review_payload_shape() -> None:
    payload = build_prompt_review_payload(
        Phase.STORY_ANALYSIS,
        {"system": "s", "user": "u"},
        {"origin": "catalog", "ref": "phase/story_analysis@stub", "editor": None},
        [{"id": "p1", "source": "stub", "title": "T", "summary": "S", "ref": "dropped"}],
        ["story_analysis.stub_lookup"],
    )
    assert payload["schema_version"] == 1
    assert payload["kind"] == "prompt_review"
    assert payload["phase"] == "story_analysis"
    assert payload["prompt"] == {
        "system": "s",
        "user": "u",
        "application": None,
        "source": {"origin": "catalog", "ref": "phase/story_analysis@stub"},
    }
    assert payload["context_packets"] == [
        {"id": "p1", "source": "stub", "title": "T", "summary": "S"}
    ]
    assert payload["tools"] == ["story_analysis.stub_lookup"]
    assert payload["editable"] is True
    assert payload["actions"] == list(PROMPT_REVIEW_ACTIONS)
    assert "error" not in payload


def test_prompt_review_payload_carries_error() -> None:
    payload = build_prompt_review_payload(Phase.EXECUTION, {}, {}, [], [], error="bad action")
    assert payload["error"] == "bad action"


def test_phase_review_payload_shape() -> None:
    payload = build_phase_review_payload(
        Phase.TEST_PLANNING,
        "the summary",
        {"summary": "the summary", "reasoning_digest": "digest"},
        [{"id": "a1", "kind": "transcript", "name": "n"}],
        ["a warning"],
        [{"role": "operator", "content": "hi"}],
    )
    assert payload["schema_version"] == 1
    assert payload["kind"] == "phase_review"
    assert payload["phase"] == "test_planning"
    assert payload["summary"] == "the summary"
    assert payload["result_preview"]["reasoning_digest"] == "digest"
    assert payload["artifacts"] == [{"id": "a1", "kind": "transcript", "name": "n"}]
    assert payload["warnings"] == ["a warning"]
    assert payload["dialogue_tail"] == [{"role": "operator", "content": "hi"}]
    assert payload["actions"] == list(PHASE_REVIEW_ACTIONS)
    assert "error" not in payload


def test_parse_gate_decision_passes_known_action_through() -> None:
    decision = {"action": "modify", "prompt": {"system": "x"}}
    assert parse_gate_decision(decision, PROMPT_REVIEW_ACTIONS) == decision


def test_parse_gate_decision_tolerates_plain_string() -> None:
    assert parse_gate_decision("approve", PROMPT_REVIEW_ACTIONS)["action"] == "approve"


def test_parse_gate_decision_unknown_action() -> None:
    parsed = parse_gate_decision({"action": "yolo", "note": "n"}, PROMPT_REVIEW_ACTIONS)
    assert parsed["action"] is None
    assert parsed["error"] == (
        "unknown action; expected one of ['abort', 'approve', 'modify', 'skip_phase']"
    )
    assert "yolo" not in parsed["error"]
    assert parsed["note"] == "n"  # original fields preserved for diagnostics


def test_parse_gate_decision_non_mapping() -> None:
    parsed = parse_gate_decision(42, PHASE_REVIEW_ACTIONS)
    assert parsed["action"] is None
    assert "mapping" in parsed["error"]


def test_resolve_actor_variants() -> None:
    assert resolve_actor(None) == "unknown"
    assert resolve_actor({"configurable": {}}) == "unknown"
    assert (
        resolve_actor({"configurable": {"langgraph_auth_user": {"identity": "ops@apex"}}})
        == "ops@apex"
    )

    class User:
        identity = "obj@apex"

    assert resolve_actor({"configurable": {"langgraph_auth_user": User()}}) == "obj@apex"


def test_make_approval_records_actor_and_note() -> None:
    approval = make_approval(
        Phase.EXECUTION,
        2,
        "phase_review",
        "revise",
        {"configurable": {"langgraph_auth_user": {"identity": "op"}}},
        note="tighten SLAs",
    )
    assert approval["id"] == "execution-a2-phase_review-revise"
    assert approval["gate"] == "phase_review"
    assert approval["action"] == "revise"
    assert approval["actor"] == "op"
    assert approval["note"] == "tighten SLAs"
    assert approval["at"]
