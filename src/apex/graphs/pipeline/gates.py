"""HITL gate contracts: interrupt payload builders + resume-decision parsing.

Payloads carry schema_version for dashboard contract evolution (plan risk: interrupt
payloads and custom events are versioned). Decisions arrive as Command(resume={...})
dicts; parse_gate_decision normalizes them so gate nodes never branch on raw input.
"""

from collections.abc import Sequence
from inspect import getattr_static
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig

from apex.auth.identity import RUNTIME_ACTOR_ID_KEY
from apex.domain.diagnostics import bounded_diagnostic, contains_credential_material
from apex.domain.input_limits import validate_json_object
from apex.domain.pipeline import ApprovalRecord, Phase
from apex.services.pipeline_public import (
    MAX_PUBLIC_GATE_BYTES,
    MAX_PUBLIC_GATE_NODES,
    public_phase_evidence,
)
from apex.services.run_validation import validate_gate_payload

GATE_SCHEMA_VERSION = 1

PROMPT_REVIEW_ACTIONS: tuple[str, ...] = ("approve", "modify", "skip_phase", "abort")
PHASE_REVIEW_ACTIONS: tuple[str, ...] = ("approve", "revise", "discuss", "abort")

JsonDict = dict[str, Any]


def resolve_actor(config: RunnableConfig | None) -> str:
    """Attribution identity for gate decisions, from the LangGraph auth context."""
    if type(config) is not dict:
        return "unknown"
    configurable = config.get("configurable")
    if configurable is None:
        configurable = {}
    if type(configurable) is not dict:
        return "unknown"
    for key in (RUNTIME_ACTOR_ID_KEY, "langgraph_auth_user_id"):
        runtime_identity = configurable.get(key)
        if _safe_actor(runtime_identity):
            return runtime_identity
    user = configurable.get("langgraph_auth_user")
    if user is None:
        return "unknown"
    if type(user) is dict:
        identity = user.get("identity")
    else:
        # Static inspection supports LangGraph proxy/test objects whose identity is
        # a plain instance/class attribute without executing descriptors or custom
        # ``__getattr__`` hooks supplied through a native runnable config.
        try:
            identity = getattr_static(user, "identity", None)
        except Exception:
            return "unknown"
    return identity if _safe_actor(identity) else "unknown"


def _safe_actor(value: Any) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 255
        and "\x00" not in value
        and not contains_credential_material(value)
    )


def make_approval(
    phase: Phase,
    attempt: int,
    gate: Literal["prompt_review", "phase_review", "human_completion"],
    action: str,
    config: RunnableConfig | None,
    note: str | None = None,
    sequence: int = 0,
) -> JsonDict:
    suffix = f"-s{sequence}" if sequence else ""
    return ApprovalRecord(
        id=f"{phase.value}-a{attempt}-{gate}-{action}{suffix}",
        gate=gate,
        action=action,
        actor=resolve_actor(config),
        note=note,
    ).model_dump(mode="json")


def build_prompt_review_payload(
    phase: Phase,
    prompt: JsonDict,
    source: JsonDict,
    context_packets: Sequence[JsonDict],
    tools: Sequence[str],
    error: str | None = None,
    additional_context: str = "",
) -> JsonDict:
    payload: JsonDict = {
        "schema_version": GATE_SCHEMA_VERSION,
        "kind": "prompt_review",
        "phase": phase.value,
        "prompt": {
            "system": prompt.get("system"),
            "user": prompt.get("user"),
            "application": prompt.get("application"),
            **{
                key: prompt[key]
                for key in ("system_locked", "contexts", "selections")
                if key in prompt
            },
            "source": {
                "origin": source.get("origin"),
                "ref": source.get("ref"),
                **({"editor": source["editor"]} if _safe_actor(source.get("editor")) else {}),
            },
        },
        "additional_context": additional_context,
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
    evidence: JsonDict | None = None,
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
    payload = _fit_phase_review_preview(payload)
    payload["evidence"] = public_phase_evidence(phase.value, evidence, gate_payload=payload)
    return payload


def _fits_phase_review_preview(payload: JsonDict) -> bool:
    try:
        validate_json_object(
            payload,
            label="phase review preview",
            max_bytes=MAX_PUBLIC_GATE_BYTES - 2_048,
            max_nodes=MAX_PUBLIC_GATE_NODES - 8,
            max_depth=24,
        )
    except (OverflowError, RecursionError, TypeError, ValueError):
        return False
    return True


def _fit_phase_review_preview(payload: JsonDict) -> JsonDict:
    """Keep legal generated results reviewable without enlarging the public boundary.

    Output summaries, artifacts and diagnostics each have independent limits, so
    their combined preview can still exceed the gate budget. Keep whole fields
    and rows where possible, mark every omission, and leave durable state alone.
    Discussion previews have a smaller public text limit than durable messages.
    """
    for entry in payload["dialogue_tail"]:
        content = entry.get("content")
        if type(content) is str and len(content) > 20_000:
            entry["content"] = bounded_diagnostic(content, max_chars=19_999) + "…"
            payload["projection_truncated"] = True
    if _fits_phase_review_preview(payload):
        return payload

    result: JsonDict = {
        **payload,
        "summary": None,
        "result_preview": {"summary": None, "reasoning_digest": None},
        "artifacts": [],
        "warnings": [],
        "dialogue_tail": [],
        "projection_truncated": True,
    }
    summary = payload["summary"]
    if summary is not None:
        result["summary"] = summary
        if not _fits_phase_review_preview(result):
            result["summary"] = None
    # If only the reasoning digest contains meaningful output, reserve it before
    # lists of blank legacy diagnostics can consume the remaining byte budget.
    if not (type(summary) is str and summary.strip()):
        for field in ("reasoning_digest", "summary"):
            value = payload["result_preview"].get(field)
            if type(value) is str and value.strip():
                result["result_preview"][field] = value
                if _fits_phase_review_preview(result):
                    break
                result["result_preview"][field] = None
    # Preserve safety diagnostics and artifact references before supplementary
    # reasoning text. A visible marker tells the reviewer the preview is partial.
    for field in ("warnings", "artifacts", "dialogue_tail"):
        entries = payload[field]
        for entry in reversed(entries) if field == "dialogue_tail" else entries:
            if field == "warnings" and (type(entry) is not str or not entry.strip()):
                continue
            if field == "dialogue_tail" and (
                type(entry) is not dict
                or type(entry.get("content")) is not str
                or not entry["content"].strip()
            ):
                continue
            if field == "artifacts" and (
                type(entry) is not dict
                or not all(
                    type(entry.get(key)) is str and entry[key].strip()
                    for key in ("id", "kind", "name")
                )
            ):
                continue
            retained = result[field]
            result[field] = [entry, *retained] if field == "dialogue_tail" else [*retained, entry]
            if not _fits_phase_review_preview(result):
                result[field] = retained
    for field in ("reasoning_digest", "summary"):
        value = payload["result_preview"].get(field)
        if value is not None:
            result["result_preview"][field] = value
            if not _fits_phase_review_preview(result):
                result["result_preview"][field] = None
    return result


def parse_gate_decision(decision: Any, allowed: Sequence[str]) -> JsonDict:
    """Normalize a resume value into {"action": <allowed>|None, ...}.

    Unknown/malformed input never raises: action is None and "error" explains why,
    so gate nodes can re-interrupt with the error surfaced in the payload.
    """
    try:
        validate_gate_payload(decision)
    except ValueError as exc:
        return {"action": None, "error": _safe_gate_validation_error(exc)}
    if contains_credential_material(decision):
        return {
            "action": None,
            "error": "gate decision must not contain credential material",
        }
    if type(decision) is str:
        decision = {"action": decision}
    if type(decision) is not dict:
        return {
            "action": None,
            "error": "expected a mapping with an 'action' key",
        }
    if any(key not in {"action", "prompt", "instructions", "message", "note"} for key in decision):
        return {"action": None, "error": "gate decision contains unsupported fields"}
    for field in ("instructions", "message", "note"):
        value = decision.get(field)
        if value is not None and type(value) is not str:
            return {"action": None, "error": f"gate decision {field} must be a string"}
    edit = decision.get("prompt")
    if edit is not None:
        if type(edit) is not dict or any(
            key not in {"system", "user", "application", "context_edits"} for key in edit
        ):
            return {"action": None, "error": "gate decision prompt is invalid"}
        if "context_edits" in edit:
            from apex.domain.prompt_selection import PromptContextEdit

            entries = edit["context_edits"]
            if type(entries) is not list or len(entries) > 64:
                return {"action": None, "error": "Invalid context prompt edits"}
            try:
                for item in entries:
                    PromptContextEdit.model_validate(item)
            except ValueError:
                return {"action": None, "error": "Invalid context prompt edits"}
        if any(
            value is not None and type(value) is not str
            for key, value in edit.items()
            if key != "context_edits"
        ):
            return {"action": None, "error": "gate decision prompt values must be strings"}
    action = decision.get("action")
    if type(action) is not str or action not in allowed:
        return {
            **{k: v for k, v in decision.items() if k != "action"},
            "action": None,
            "error": f"unknown action; expected one of {sorted(allowed)}",
        }
    return dict(decision)


def _safe_gate_validation_error(exc: ValueError) -> str:
    """Preserve only the fixed validation messages produced by this boundary."""

    if type(exc) is ValueError:
        try:
            arguments = BaseException.__dict__["args"].__get__(exc, ValueError)
        except Exception:
            arguments = ()
        if (
            type(arguments) is tuple
            and len(arguments) == 1
            and type(arguments[0]) is str
            and 1 <= len(arguments[0]) <= 512
            and arguments[0].startswith("gate payload ")
        ):
            return arguments[0]
    return "gate decision is invalid"
