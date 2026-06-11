"""HITL gate contracts: interrupt payload builders + resume-decision parsing.

Payloads carry schema_version for dashboard contract evolution (plan risk: interrupt
payloads and custom events are versioned). Decisions arrive as Command(resume={...})
dicts; parse_gate_decision normalizes them so gate nodes never branch on raw input.
"""

from collections.abc import Sequence
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig

from apex.domain.pipeline import ApprovalRecord, Phase

GATE_SCHEMA_VERSION = 1

PROMPT_REVIEW_ACTIONS: tuple[str, ...] = ("approve", "modify", "skip_phase", "abort")
PHASE_REVIEW_ACTIONS: tuple[str, ...] = ("approve", "revise", "discuss", "abort")

JsonDict = dict[str, Any]


def resolve_actor(config: RunnableConfig | None) -> str:
    """Attribution identity for gate decisions, from the LangGraph auth context."""
    configurable = (config or {}).get("configurable") or {}
    user = configurable.get("langgraph_auth_user")
    if user is None:
        return "unknown"
    identity = user.get("identity") if isinstance(user, dict) else getattr(user, "identity", None)
    return str(identity) if identity else "unknown"


def make_approval(
    gate: Literal["prompt_review", "phase_review"],
    action: str,
    config: RunnableConfig | None,
    note: str | None = None,
) -> JsonDict:
    return ApprovalRecord(
        gate=gate, action=action, actor=resolve_actor(config), note=note
    ).model_dump(mode="json")


def build_prompt_review_payload(
    phase: Phase,
    prompt: JsonDict,
    source: JsonDict,
    context_packets: Sequence[JsonDict],
    tools: Sequence[str],
    error: str | None = None,
) -> JsonDict:
    payload: JsonDict = {
        "schema_version": GATE_SCHEMA_VERSION,
        "kind": "prompt_review",
        "phase": phase.value,
        "prompt": {
            "system": prompt.get("system"),
            "user": prompt.get("user"),
            "source": {"origin": source.get("origin"), "ref": source.get("ref")},
        },
        "context_packets": [
            {
                "id": packet.get("id"),
                "source": packet.get("source"),
                "title": packet.get("title"),
                "summary": packet.get("summary"),
            }
            for packet in context_packets
        ],
        "tools": list(tools),
        "editable": True,
        "actions": list(PROMPT_REVIEW_ACTIONS),
    }
    if error:
        payload["error"] = error
    return payload


def build_phase_review_payload(
    phase: Phase,
    summary: str | None,
    result_preview: JsonDict,
    artifacts: Sequence[JsonDict],
    warnings: Sequence[str],
    dialogue_tail: Sequence[JsonDict],
    error: str | None = None,
) -> JsonDict:
    payload: JsonDict = {
        "schema_version": GATE_SCHEMA_VERSION,
        "kind": "phase_review",
        "phase": phase.value,
        "summary": summary,
        "result_preview": dict(result_preview),
        "artifacts": [dict(artifact) for artifact in artifacts],
        "warnings": list(warnings),
        "dialogue_tail": [dict(entry) for entry in dialogue_tail],
        "actions": list(PHASE_REVIEW_ACTIONS),
    }
    if error:
        payload["error"] = error
    return payload


def parse_gate_decision(decision: Any, allowed: Sequence[str]) -> JsonDict:
    """Normalize a resume value into {"action": <allowed>|None, ...}.

    Unknown/malformed input never raises: action is None and "error" explains why,
    so gate nodes can re-interrupt with the error surfaced in the payload.
    """
    if isinstance(decision, str):
        decision = {"action": decision}
    if not isinstance(decision, dict):
        return {
            "action": None,
            "error": f"expected a mapping with an 'action' key, got {type(decision).__name__}",
        }
    action = decision.get("action")
    if action not in allowed:
        return {
            **{k: v for k, v in decision.items() if k != "action"},
            "action": None,
            "error": f"unknown action {action!r}; expected one of {sorted(allowed)}",
        }
    return dict(decision)
