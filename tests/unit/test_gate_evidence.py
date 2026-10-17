"""Pre-acceptance evidence uses the same bounded public projection as history."""

from typing import Any

import pytest

from apex.domain.input_limits import validate_json_object
from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.graphs.pipeline.gates import build_phase_review_payload, make_approval
from apex.graphs.pipeline.state import merge_phase_results
from apex.services.pipeline_public import (
    MAX_PUBLIC_GATE_BYTES,
    MAX_PUBLIC_GATE_NODES,
    MAX_PUBLIC_PIPELINE_STATE_BYTES,
    MAX_PUBLIC_PIPELINE_STATE_NODES,
    public_gate,
    public_phase_evidence,
    public_pipeline_state,
)


def test_review_evidence_preserves_actual_model_and_omits_internal_fields():
    evidence = public_phase_evidence(
        "story_analysis",
        {
            "model": "claude-sonnet",
            "provenance": "llm",
            "attempt": 1,
            "reasoning_digest": "Checked the requirements",
            "internal": "private",
        },
    )
    assert evidence == {
        "phase": "story_analysis",
        "attempt": 1,
        "model": "claude-sonnet",
        "provenance": "llm",
        "reasoning_digest": "Checked the requirements",
    }


def test_absent_optional_evidence_does_not_claim_that_evidence_was_omitted():
    payload = build_phase_review_payload(
        Phase.STORY_ANALYSIS, "Ready", {"summary": "Ready", "reasoning_digest": None}, [], [], []
    )
    assert payload["evidence"] == {}
    assert public_phase_evidence("story_analysis", None) == {}
    assert public_phase_evidence("story_analysis", []) == {"projection_truncated": True}


@pytest.mark.parametrize("model", [None, [], "x" * 201, "password=private-secret-canary"])
def test_review_evidence_rejects_invalid_or_credential_model(model):
    assert "model" not in public_phase_evidence("story_analysis", {"model": model})


def phase_gate(evidence: dict[str, Any]) -> dict[str, Any]:
    payload = build_phase_review_payload(
        Phase.STORY_ANALYSIS,
        "Ready for review",
        {"summary": "Ready for review", "reasoning_digest": None},
        [],
        [],
        [],
        evidence=evidence,
    )
    return {
        "interrupt_id": "review-1",
        "kind": "phase_review",
        "phase": "story_analysis",
        "payload": payload,
    }


def test_phase_payload_rejects_malformed_keys_before_optional_evidence_filtering():
    class HostileKey:
        def __hash__(self) -> int:
            return 123

        def __ne__(self, other: object) -> bool:
            raise AssertionError("untrusted mapping key inequality hook ran")

    gate = phase_gate({})
    gate["payload"][HostileKey()] = "untrusted"
    assert public_gate(gate, include_payload=True) is None
    assert public_gate(gate, include_payload=False) is None


@pytest.mark.parametrize("location", ["evidence", "prompt", "source", "tool"])
def test_optional_evidence_rejects_nested_mapping_hooks_without_hiding_the_gate(location):
    field = {"evidence": "provenance", "prompt": "system", "source": "origin", "tool": "id"}[
        location
    ]

    class HostileKey:
        def __hash__(self) -> int:
            return hash(field)

        def __eq__(self, other: object) -> bool:
            raise AssertionError("untrusted optional evidence mapping hook ran")

    malformed = {HostileKey(): "untrusted"}
    evidence = {
        "evidence": malformed,
        "prompt": {"resolved_prompt": malformed},
        "source": {"resolved_prompt_source": malformed},
        "tool": {"tool_calls": [malformed]},
    }[location]
    gate = phase_gate({})
    gate["payload"]["evidence"] = evidence
    projected = public_gate(gate, include_payload=True)
    assert projected is not None
    assert projected["payload"]["evidence"] == {"projection_truncated": True}
    assert public_gate(gate, include_payload=False) is not None


@pytest.mark.parametrize("malformation", ["nodes", "depth", "cycle", "object"])
def test_optional_evidence_shape_validation_is_bounded_and_preserves_the_gate(malformation):
    evidence: dict[str, Any] = {}
    if malformation == "nodes":
        evidence["unrecognized"] = [None] * MAX_PUBLIC_GATE_NODES
    elif malformation == "depth":
        nested = evidence
        for _ in range(25):
            child: dict[str, Any] = {}
            nested["unrecognized"] = child
            nested = child
    elif malformation == "cycle":
        evidence["unrecognized"] = evidence
    else:
        evidence["unrecognized"] = object()
    gate = phase_gate({})
    gate["payload"]["evidence"] = evidence
    projected = public_gate(gate, include_payload=True)
    assert projected is not None
    assert projected["payload"]["evidence"] == {"projection_truncated": True}


def test_large_unicode_prompt_never_hides_an_otherwise_valid_phase_review():
    entry = {
        "phase": "story_analysis",
        "attempt": 1,
        "status": "awaiting_output_review",
        "summary": "Ready for review",
        "resolved_prompt_source": {"origin": "run_override"},
        "resolved_prompt": {"system": "测" * 45_000, "user": "验" * 45_000},
    }
    # Both prompt parts fit the deployment default and the durable reducer.
    merge_phase_results({}, {"story_analysis": entry})
    gate = phase_gate(entry)
    assert gate["payload"]["evidence"]["projection_truncated"] is True
    assert "resolved_prompt" not in gate["payload"]["evidence"]
    assert gate["payload"]["evidence"]["resolved_prompt_source"] == {"origin": "run_override"}
    projected = public_gate(gate, include_payload=True)
    assert projected is not None
    assert projected["payload"]["evidence"]["projection_truncated"] is True
    assert projected["payload"]["actions"] == ["approve", "revise", "discuss", "abort"]
    validate_json_object(
        projected,
        label="gate",
        max_bytes=MAX_PUBLIC_GATE_BYTES,
        max_nodes=MAX_PUBLIC_GATE_NODES,
    )
    # Repair reads of checkpoints written before producers fitted optional evidence.
    gate["payload"]["evidence"] = entry
    assert public_gate(gate, include_payload=True) == projected
    assert public_gate(gate, include_payload=False) is not None


def test_optional_evidence_respects_the_remaining_gate_budget_and_redaction():
    gate = phase_gate({})
    gate["payload"]["warnings"] = ["w" * 4_000] * 63
    gate["payload"]["evidence"] = {
        "model": "password=private-secret-canary",
        "resolved_prompt": {"system": "s" * 10_000},
        "tool_calls": [{"id": "tool-1", "tool": "lookup", "status": "ok", "args": "secret"}],
    }
    projected = public_gate(gate, include_payload=True)
    assert projected is not None
    evidence = projected["payload"]["evidence"]
    assert evidence["projection_truncated"] is True
    assert "model" not in evidence and "resolved_prompt" not in evidence
    assert evidence["tool_calls"] == [{"id": "tool-1", "tool": "lookup", "status": "ok"}]
    # Required oversized content still fails closed; optional evidence is no bypass.
    gate["payload"]["warnings"].append("w" * 4_000)
    assert public_gate(gate, include_payload=True) is None


def test_optional_evidence_cannot_hide_a_gate_with_no_room_for_an_omission_marker():
    gate = phase_gate({})
    gate["payload"].pop("evidence")
    gate["payload"]["warnings"] = ["w" * 4_000] * 63 + [""]
    lower, upper = 0, 4_000
    while lower < upper:
        size = (lower + upper + 1) // 2
        gate["payload"]["warnings"][-1] = "w" * size
        if public_gate(gate, include_payload=True) is not None:
            lower = size
        else:
            upper = size - 1
    gate["payload"]["warnings"][-1] = "w" * lower
    baseline = public_gate(gate, include_payload=True)
    assert baseline is not None
    gate["payload"]["evidence"] = {"resolved_prompt": {"system": "s" * 10_000}}
    assert public_gate(gate, include_payload=True) == baseline


def test_legacy_phase_review_summary_and_detail_agree_at_the_envelope_boundary():
    gate = phase_gate({})
    gate["interrupt_id"] = "😀" * 256
    gate["payload"].pop("evidence")
    gate["payload"]["warnings"] = ["w" * 4_000] * 63 + [""]
    lower, upper = 0, 4_000
    while lower < upper:
        size = (lower + upper + 1) // 2
        gate["payload"]["warnings"][-1] = "w" * size
        if public_gate(gate, include_payload=True) is not None:
            lower = size
        else:
            upper = size - 1
    gate["payload"]["warnings"][-1] = "w" * lower
    assert public_gate(gate, include_payload=True) is not None
    assert public_gate(gate, include_payload=False) is not None
    gate["payload"]["warnings"][-1] += "w"
    assert public_gate(gate, include_payload=True) is None
    assert public_gate(gate, include_payload=False) is None


@pytest.mark.parametrize("count", [101, 256])
def test_public_history_preserves_the_durable_approval_and_dialogue_limits(count):
    approvals = [
        make_approval(
            Phase.STORY_ANALYSIS,
            1,
            "phase_review",
            "discuss" if index < count - 1 else "approve",
            {"configurable": {"langgraph_auth_user_id": "operator"}},
            sequence=index + 1,
        )
        for index in range(count)
    ]
    phase_results = merge_phase_results(
        {}, {"story_analysis": {"status": "succeeded", "approvals": approvals}}
    )
    dialogue = [
        {"id": str(index), "phase": "story_analysis", "role": "operator", "content": "Why?"}
        for index in range(count)
    ]
    projected = public_pipeline_state({"phase_results": phase_results, "dialogue": dialogue})
    assert len(projected["phase_results"]["story_analysis"]["approvals"]) == count
    assert projected["phase_results"]["story_analysis"]["approvals"][-1]["action"] == "approve"
    assert projected["dialogue"] == dialogue
    assert "projection_truncated" not in projected


@pytest.mark.parametrize("with_phase", [False, True])
def test_large_dialogue_retains_approval_history_and_marks_incomplete_projection(with_phase):
    approval = make_approval(
        Phase.STORY_ANALYSIS,
        1,
        "phase_review",
        "approve",
        {"configurable": {"langgraph_auth_user_id": "reviewer"}},
    )
    dialogue = [
        {"id": str(index), "phase": "story_analysis", "role": "operator", "content": "测" * 20_000}
        for index in range(100)
    ]
    projected = public_pipeline_state(
        {
            **(
                {
                    "phase_results": {
                        "story_analysis": {"status": "succeeded", "approvals": [approval]}
                    }
                }
                if with_phase
                else {}
            ),
            "dialogue": dialogue,
        }
    )
    assert projected["projection_truncated"] is True
    if with_phase:
        assert projected["phase_results"]["story_analysis"]["approvals"][0]["actor"] == "reviewer"
    assert 0 < len(projected["dialogue"]) < len(dialogue)
    assert projected["dialogue"][-1] == dialogue[-1]
    validate_json_object(
        projected,
        label="state",
        max_bytes=MAX_PUBLIC_PIPELINE_STATE_BYTES,
        max_nodes=MAX_PUBLIC_PIPELINE_STATE_NODES,
    )


def test_legal_large_output_produces_a_readable_explicitly_partial_gate():
    entry = {
        "phase": "story_analysis",
        "status": "awaiting_output_review",
        "summary": "😀" * 20_000,
        "reasoning_digest": "🧪" * 20_000,
        "warnings": ["⚠️" * 2_000] * 5,
    }
    merge_phase_results({}, {"story_analysis": entry})
    payload = build_phase_review_payload(
        Phase.STORY_ANALYSIS,
        entry["summary"],
        {"summary": entry["summary"], "reasoning_digest": entry["reasoning_digest"]},
        [],
        entry["warnings"],
        [],
    )
    assert payload["projection_truncated"] is True
    assert payload["summary"] == entry["summary"]
    gate = phase_gate({})
    gate["payload"] = payload
    projected = public_gate(gate, include_payload=True)
    assert projected is not None
    assert projected["payload"]["projection_truncated"] is True
    validate_json_object(
        projected,
        label="gate",
        max_bytes=MAX_PUBLIC_GATE_BYTES,
        max_nodes=MAX_PUBLIC_GATE_NODES,
    )


def test_preview_keeps_meaningful_output_when_large_blank_diagnostics_are_omitted():
    entry = {
        "phase": "story_analysis",
        "summary": None,
        "reasoning_digest": "Inspected the requirements",
        "warnings": ["\u3000" * 4_000] * 40,
    }
    merge_phase_results({}, {"story_analysis": entry})
    gate = phase_gate({})
    gate["payload"] = build_phase_review_payload(
        Phase.STORY_ANALYSIS,
        None,
        {"summary": None, "reasoning_digest": entry["reasoning_digest"]},
        [],
        entry["warnings"],
        [],
    )
    assert public_gate(gate, include_payload=True) is not None
    assert gate["payload"]["result_preview"]["reasoning_digest"] == entry["reasoning_digest"]


def test_large_discussion_preview_and_history_are_explicit_without_mutating_durable_messages():
    message = "m" * 20_000 + " agent acknowledgment"
    dialogue = [{"id": "d-1", "phase": "story_analysis", "role": "agent", "content": message}]
    payload = build_phase_review_payload(
        Phase.STORY_ANALYSIS,
        None,
        {"summary": None, "reasoning_digest": None},
        [],
        [],
        dialogue,
    )
    assert dialogue[0]["content"] == message
    assert payload["projection_truncated"] is True
    assert payload["dialogue_tail"][0]["content"] == message[:19_999] + "…"
    gate = phase_gate({})
    gate["payload"] = payload
    assert public_gate(gate, include_payload=True) is not None
    projected = public_pipeline_state({"dialogue": dialogue})
    assert projected["projection_truncated"] is True
    assert projected["dialogue"][0]["projection_truncated"] is True
    assert projected["dialogue"][0]["content"] == payload["dialogue_tail"][0]["content"]
    # A raw oversized gate remains invalid; only its trusted producer may fit it.
    payload["dialogue_tail"] = dialogue
    assert public_gate(gate, include_payload=True) is None


def test_shortening_long_history_redacts_credentials_before_the_truncation_boundary():
    message = "m" * 19_980 + " password=private-secret-canary " + "x" * 1_000
    projected = public_pipeline_state(
        {
            "dialogue": [
                {"id": "d-1", "phase": "story_analysis", "role": "operator", "content": message},
            ]
        }
    )
    assert projected["projection_truncated"] is True
    assert "private-secret" not in projected["dialogue"][0]["content"]
    assert "password=[REDACTED" in projected["dialogue"][0]["content"]


def test_oversized_audit_keeps_every_phases_latest_decision_within_the_existing_budget():
    phase_results = {
        phase.value: {
            "status": "succeeded",
            "approvals": [
                {
                    "id": f"{phase.value}-{index}-" + "x" * 200,
                    "gate": "phase_review",
                    "action": "discuss" if index < 255 else "approve",
                    "actor": "operator-" + "o" * 240,
                }
                for index in range(256)
            ],
        }
        for phase in PHASE_ORDER
    }
    projected = public_pipeline_state({"phase_results": phase_results})
    assert projected["projection_truncated"] is True
    kept = projected["phase_results"]
    assert all(entry["approvals"][-1]["action"] == "approve" for entry in kept.values())
    assert any(len(entry["approvals"]) < 256 for entry in kept.values())
    validate_json_object(
        projected,
        label="state",
        max_bytes=MAX_PUBLIC_PIPELINE_STATE_BYTES,
        max_nodes=MAX_PUBLIC_PIPELINE_STATE_NODES,
    )
