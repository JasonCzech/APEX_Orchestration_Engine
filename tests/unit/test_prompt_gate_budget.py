"""Editable prompt bodies remain complete within their separate finite budget."""

import json
from typing import Any

import pytest

from apex.domain.input_limits import validate_json_object
from apex.domain.pipeline import Phase
from apex.graphs.pipeline.gates import build_prompt_review_payload
from apex.services.pipeline_public import (
    MAX_PUBLIC_GATE_BYTES,
    MAX_PUBLIC_GATE_NODES,
    MAX_PUBLIC_PROMPT_BODY_BYTES,
    gate_budget_envelope,
    public_gate,
)
from apex.services.run_validation import validate_prompt_parts


def prompt_gate(
    system: str = "System", user: str = "User", application: str | None = None, context: str = ""
) -> dict[str, Any]:
    return {
        "interrupt_id": "prompt-1",
        "kind": "prompt_review",
        "phase": "story_analysis",
        "payload": build_prompt_review_payload(
            Phase.STORY_ANALYSIS,
            {"system": system, "user": user, "application": application},
            {"origin": "catalog", "ref": "phase/story_analysis/system@v1"},
            [],
            [],
            additional_context=context,
        ),
    }


def test_deployment_valid_unicode_prompt_is_complete_in_summary_and_detail() -> None:
    system, user = "测" * 45_000, "验" * 45_000
    validate_prompt_parts(system=system, user=user)
    gate = prompt_gate(system, user)
    assert public_gate(gate, include_payload=True) == gate
    assert public_gate(gate, include_payload=False) == {
        key: value for key, value in gate.items() if key != "payload"
    }


@pytest.mark.parametrize("character", ["😀", "\x01"])
def test_maximum_public_prompt_parts_keep_complete_unicode_and_escaped_json(character: str) -> None:
    gate = prompt_gate(
        character * 100_000, character * 100_000, character * 100_000, character * 50_000
    )
    assert public_gate(gate, include_payload=True) == gate
    assert public_gate(gate, include_payload=False) is not None
    validate_json_object(
        gate,
        label="complete prompt gate",
        max_bytes=MAX_PUBLIC_GATE_BYTES + MAX_PUBLIC_PROMPT_BODY_BYTES,
        max_nodes=MAX_PUBLIC_GATE_NODES,
    )
    envelope = gate_budget_envelope(gate)
    assert envelope is not None
    validate_json_object(
        envelope,
        label="prompt envelope",
        max_bytes=MAX_PUBLIC_GATE_BYTES,
        max_nodes=MAX_PUBLIC_GATE_NODES,
    )


@pytest.mark.parametrize("field", ["system", "user", "application", "additional_context"])
def test_prompt_allowance_does_not_raise_any_field_limit(field: str) -> None:
    gate = prompt_gate()
    target = gate["payload"] if field == "additional_context" else gate["payload"]["prompt"]
    target[field] = "x" * (50_001 if field == "additional_context" else 100_001)
    assert public_gate(gate, include_payload=True) is None
    assert public_gate(gate, include_payload=False) is None


@pytest.mark.parametrize("location", ["payload", "prompt", "source"])
def test_unrecognized_fields_cannot_borrow_the_prompt_body_allowance(location: str) -> None:
    gate = prompt_gate("😀" * 100_000)
    targets = {
        "payload": gate["payload"],
        "prompt": gate["payload"]["prompt"],
        "source": gate["payload"]["prompt"]["source"],
    }
    targets[location]["unrecognized"] = {"system": "x" * MAX_PUBLIC_GATE_BYTES}
    assert public_gate(gate, include_payload=True) is None
    assert public_gate(gate, include_payload=False) is None


def test_irrelevant_outer_fields_are_omitted_without_consuming_prompt_budget() -> None:
    gate = prompt_gate("😀" * 100_000)
    projected = public_gate(gate, include_payload=True)
    gate["unrecognized"] = {"system": "x" * (MAX_PUBLIC_GATE_BYTES + MAX_PUBLIC_PROMPT_BODY_BYTES)}
    assert public_gate(gate, include_payload=True) == projected


def test_prompt_summary_and_detail_agree_at_the_outer_envelope_boundary() -> None:
    gate = prompt_gate("😀" * 100_000)
    gate["interrupt_id"] = "测" * 256
    packets = [
        {"id": None, "source": None, "title": None, "summary": "x" * 4_000} for _ in range(62)
    ]
    packets.append({"id": None, "source": None, "title": None, "summary": ""})
    gate["payload"]["context_packets"] = packets
    envelope = gate_budget_envelope(gate)
    assert envelope is not None
    used = len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())
    remaining = MAX_PUBLIC_GATE_BYTES - used
    assert 0 <= remaining < 4_000
    packets[-1]["summary"] = "x" * remaining
    assert public_gate(gate, include_payload=True) == gate
    assert public_gate(gate, include_payload=False) is not None
    packets[-1]["summary"] += "x"
    assert public_gate(gate, include_payload=True) is None
    assert public_gate(gate, include_payload=False) is None


@pytest.mark.parametrize("field", ["system", "application", "additional_context"])
@pytest.mark.parametrize("value", [[], {"system": "x"}, "\x00", "\ud800"])
def test_malformed_prompt_bodies_still_fail_closed(field: str, value: Any) -> None:
    gate = prompt_gate()
    target = gate["payload"] if field == "additional_context" else gate["payload"]["prompt"]
    target[field] = value
    assert public_gate(gate, include_payload=True) is None


def test_malformed_payload_kind_does_not_invoke_equality_hooks() -> None:
    class HostileKind:
        def __eq__(self, other: object) -> bool:
            raise AssertionError("untrusted equality hook ran")

        def __ne__(self, other: object) -> bool:
            raise AssertionError("untrusted inequality hook ran")

    gate = prompt_gate()
    gate["payload"]["kind"] = HostileKind()
    assert public_gate(gate, include_payload=True) is None
    assert public_gate(gate, include_payload=False) is None


@pytest.mark.parametrize("location", ["payload", "prompt"])
def test_malformed_prompt_mapping_keys_do_not_invoke_equality_hooks(location: str) -> None:
    field = "kind" if location == "payload" else "system"

    class HostileKey:
        def __hash__(self) -> int:
            return hash(field)

        def __eq__(self, other: object) -> bool:
            raise AssertionError("untrusted mapping key equality hook ran")

    gate = prompt_gate()
    target = gate["payload"] if location == "payload" else gate["payload"]["prompt"]
    target.pop(field)
    target[HostileKey()] = "untrusted"
    assert public_gate(gate, include_payload=True) is None


def test_large_prompt_still_rejects_credential_material() -> None:
    gate = prompt_gate("测" * 45_000, "验" * 45_000 + "\npassword=private-canary")
    assert public_gate(gate, include_payload=True) is None
    assert public_gate(gate, include_payload=False) is None
