"""Pipeline graph state: TypedDict channels + reducers.

Domain payloads are plain JSON dicts (see apex.domain.pipeline). Reducers are
idempotent so node re-execution after crash recovery cannot duplicate entries:
list channels de-dup on stable `id`s; phase_results merges per phase, replacing
wholesale when the attempt number changes (a re-run overwrites, a resume merges).
"""

import hashlib
import json
from typing import Annotated, Any, TypedDict

from apex.domain.pipeline import MAX_TOOL_CALL_RECORDS

JsonDict = dict[str, Any]

_BY_ID_FIELDS = ("approvals", "tool_calls")
_PLAIN_LIST_FIELDS = ("warnings", "errors", "artifact_ids")
MAX_DURABLE_ARTIFACTS = 256
MAX_DURABLE_DIALOGUE_ENTRIES = 256
MAX_DURABLE_PHASE_DIAGNOSTICS = 128


def _merge_lists_by_id(current: list[JsonDict], incoming: list[JsonDict]) -> list[JsonDict]:
    def stable_entry(entry: JsonDict) -> JsonDict:
        if entry.get("id"):
            return entry
        canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
        return {"id": f"derived-{hashlib.sha256(canonical.encode()).hexdigest()[:24]}", **entry}

    merged = [stable_entry(entry) for entry in current]
    seen = {entry.get("id") for entry in merged}
    for raw_entry in incoming:
        entry = stable_entry(raw_entry)
        if entry.get("id") not in seen:
            merged.append(entry)
            seen.add(entry.get("id"))
    return merged


def _merge_unique(current: list[Any], incoming: list[Any]) -> list[Any]:
    merged = list(current)
    for item in incoming:
        if item not in merged:
            merged.append(item)
    return merged


def _cap_plain_phase_list(field: str, values: list[Any]) -> list[Any]:
    limit = (
        MAX_DURABLE_PHASE_DIAGNOSTICS if field in {"warnings", "errors"} else MAX_DURABLE_ARTIFACTS
    )
    return values[-limit:]


def _bounded_phase_entry(entry: JsonDict) -> JsonDict:
    bounded = dict(entry)
    for field in _PLAIN_LIST_FIELDS:
        if field in bounded:
            bounded[field] = _cap_plain_phase_list(field, list(bounded.get(field) or []))
    return bounded


def _merge_phase_entry(current: JsonDict, incoming: JsonDict) -> JsonDict:
    # A new attempt replaces the entry wholesale (re-run semantics, ADR-0004);
    # within the same attempt, scalars are last-write-wins and lists union.
    if incoming.get("attempt") != current.get("attempt"):
        return _bounded_phase_entry(incoming)
    merged = {**current, **incoming}
    for field in _BY_ID_FIELDS:
        merged[field] = _merge_lists_by_id(current.get(field) or [], incoming.get(field) or [])
        if field == "tool_calls":
            merged[field] = merged[field][-MAX_TOOL_CALL_RECORDS:]
    for field in _PLAIN_LIST_FIELDS:
        current_values = _cap_plain_phase_list(field, list(current.get(field) or []))
        incoming_values = _cap_plain_phase_list(field, list(incoming.get(field) or []))
        merged[field] = _cap_plain_phase_list(
            field,
            _merge_unique(current_values, incoming_values),
        )
    return merged


def merge_phase_results(
    left: dict[str, JsonDict] | None, right: dict[str, JsonDict] | None
) -> dict[str, JsonDict]:
    if not left:
        return {phase: _bounded_phase_entry(entry) for phase, entry in (right or {}).items()}
    if not right:
        return {phase: _bounded_phase_entry(entry) for phase, entry in left.items()}
    merged = dict(left)
    for phase, incoming in right.items():
        current = merged.get(phase)
        merged[phase] = (
            _merge_phase_entry(current, incoming) if current else _bounded_phase_entry(incoming)
        )
    return merged


def append_unique_by_id(
    left: list[JsonDict] | None, right: list[JsonDict] | None
) -> list[JsonDict]:
    return _merge_lists_by_id(list(left or []), list(right or []))


def merge_artifacts(left: list[JsonDict] | None, right: list[JsonDict] | None) -> list[JsonDict]:
    """Retain a bounded recent window; durable ownership lives outside checkpoints."""

    return append_unique_by_id(left, right)[-MAX_DURABLE_ARTIFACTS:]


def _dialogue_attempt(entry: JsonDict) -> int:
    raw_attempt = entry.get("attempt", 1)
    try:
        attempt = int(raw_attempt)
    except (TypeError, ValueError):
        return 1
    return max(1, attempt)


def merge_dialogue(left: list[JsonDict] | None, right: list[JsonDict] | None) -> list[JsonDict]:
    """Keep only each phase's latest attempt and a bounded global tail."""

    merged = append_unique_by_id(left, right)
    latest_by_phase: dict[str, int] = {}
    for entry in merged:
        phase = str(entry.get("phase") or "")
        latest_by_phase[phase] = max(latest_by_phase.get(phase, 1), _dialogue_attempt(entry))
    current = [
        entry
        for entry in merged
        if _dialogue_attempt(entry) == latest_by_phase.get(str(entry.get("phase") or ""), 1)
    ]
    return current[-MAX_DURABLE_DIALOGUE_ENTRIES:]


def merge_latest_by_id(left: list[JsonDict] | None, right: list[JsonDict] | None) -> list[JsonDict]:
    """Idempotently append new ids and replace existing ids in place."""

    merged = _merge_lists_by_id([], list(left or []))
    positions = {entry.get("id"): index for index, entry in enumerate(merged)}
    for incoming in _merge_lists_by_id([], list(right or [])):
        position = positions.get(incoming.get("id"))
        if position is None:
            positions[incoming.get("id")] = len(merged)
            merged.append(incoming)
        else:
            merged[position] = incoming
    return merged


def merge_context_packets(
    left: list[JsonDict] | None, right: list[JsonDict] | None
) -> list[JsonDict]:
    """Latest-by-id merge with deployment count and serialized-size budgets."""

    from apex.services.run_validation import validate_context_packets

    return validate_context_packets(merge_latest_by_id(left, right))


def merge_prompt_reviews(
    left: dict[str, JsonDict] | None, right: dict[str, JsonDict] | None
) -> dict[str, JsonDict]:
    return {**dict(left or {}), **dict(right or {})}


class PipelineInput(TypedDict, total=False):
    """Caller-owned input channels accepted when starting a pipeline run.

    Keeping this distinct from ``PipelineState`` prevents LangGraph itself from
    merging caller-supplied phase results, reviews, artifacts, or engine handles
    into a new checkpoint, even if an upstream auth hook is misconfigured.
    """

    title: str
    request: str
    external_results: JsonDict | None
    context_packets: list[JsonDict]


class PipelineState(TypedDict, total=False):
    # Run intent (input)
    title: str
    request: str
    # Optional externally-produced results (ExternalResults shape) supplied as run
    # input. When present, plan_resolver seeds a succeeded execution result from it so
    # analysis-only runs (reporting/postmortem) satisfy the execution prerequisite.
    external_results: JsonDict | None

    # Per-run cursor (overwritten by each run's plan resolver)
    phases_plan: list[str]
    current_phase: str | None
    run_aborted: bool
    # Complete run configuration snapshot. LangGraph checkpoints do not retain
    # arbitrary RunnableConfig values, so every gate resume restores this before
    # executing another node. ``limits`` remains for backward compatibility with
    # threads created before run_config was introduced.
    run_config: JsonDict
    limits: JsonDict

    # Accumulated thread state
    phase_results: Annotated[dict[str, JsonDict], merge_phase_results]
    prompt_reviews: Annotated[dict[str, JsonDict], merge_prompt_reviews]
    # Run-scoped application prompt override, keyed by app_id. App-wide for the
    # run: every phase resolves its application prompt from this single entry, so
    # an operator edit on one phase propagates to all phases of the run.
    application_reviews: Annotated[dict[str, JsonDict], merge_prompt_reviews]
    artifacts: Annotated[list[JsonDict], merge_artifacts]
    dialogue: Annotated[list[JsonDict], merge_dialogue]
    context_packets: Annotated[list[JsonDict], merge_context_packets]
    engine_handle: JsonDict | None
