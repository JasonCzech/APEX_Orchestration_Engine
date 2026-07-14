"""Pipeline graph state: TypedDict channels + reducers.

Domain payloads are plain JSON dicts (see apex.domain.pipeline). Reducers are
idempotent so node re-execution after crash recovery cannot duplicate entries:
list channels de-dup on stable `id`s; phase_results merges per phase, replacing
wholesale when the attempt number changes (a re-run overwrites, a resume merges).
"""

from typing import Annotated, Any, TypedDict

JsonDict = dict[str, Any]

_BY_ID_FIELDS = ("approvals", "tool_calls")
_PLAIN_LIST_FIELDS = ("warnings", "errors", "artifact_ids")


def _merge_lists_by_id(current: list[JsonDict], incoming: list[JsonDict]) -> list[JsonDict]:
    seen = {entry.get("id") for entry in current}
    merged = list(current)
    for entry in incoming:
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


def _merge_phase_entry(current: JsonDict, incoming: JsonDict) -> JsonDict:
    # A new attempt replaces the entry wholesale (re-run semantics, ADR-0004);
    # within the same attempt, scalars are last-write-wins and lists union.
    if incoming.get("attempt") != current.get("attempt"):
        return dict(incoming)
    merged = {**current, **incoming}
    for field in _BY_ID_FIELDS:
        merged[field] = _merge_lists_by_id(current.get(field) or [], incoming.get(field) or [])
    for field in _PLAIN_LIST_FIELDS:
        merged[field] = _merge_unique(current.get(field) or [], incoming.get(field) or [])
    return merged


def merge_phase_results(
    left: dict[str, JsonDict] | None, right: dict[str, JsonDict] | None
) -> dict[str, JsonDict]:
    if not left:
        return dict(right or {})
    if not right:
        return dict(left)
    merged = dict(left)
    for phase, incoming in right.items():
        current = merged.get(phase)
        merged[phase] = _merge_phase_entry(current, incoming) if current else incoming
    return merged


def append_unique_by_id(
    left: list[JsonDict] | None, right: list[JsonDict] | None
) -> list[JsonDict]:
    return _merge_lists_by_id(list(left or []), list(right or []))


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
    external_results: JsonDict
    context_packets: list[JsonDict]


class PipelineState(TypedDict, total=False):
    # Run intent (input)
    title: str
    request: str
    # Optional externally-produced results (ExternalResults shape) supplied as run
    # input. When present, plan_resolver seeds a succeeded execution result from it so
    # analysis-only runs (reporting/postmortem) satisfy the execution prerequisite.
    external_results: JsonDict

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
    artifacts: Annotated[list[JsonDict], append_unique_by_id]
    dialogue: Annotated[list[JsonDict], append_unique_by_id]
    context_packets: Annotated[list[JsonDict], append_unique_by_id]
    engine_handle: JsonDict | None
