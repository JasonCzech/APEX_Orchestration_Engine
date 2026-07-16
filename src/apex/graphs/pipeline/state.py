"""Pipeline graph state: TypedDict channels + reducers.

Domain payloads are plain JSON dicts (see apex.domain.pipeline). Reducers are
idempotent so node re-execution after crash recovery cannot duplicate entries:
list channels de-dup on stable `id`s; phase_results merges per phase, replacing
wholesale when the attempt number changes (a re-run overwrites, a resume merges).
"""

import hashlib
import json
import math
from typing import Annotated, Any, TypedDict

from apex.domain.diagnostics import contains_credential_material
from apex.domain.pipeline import MAX_TOOL_CALL_RECORDS, PHASE_ORDER, PhaseStatus

JsonDict = dict[str, Any]

_BY_ID_FIELDS = ("approvals", "tool_calls")
_PLAIN_LIST_FIELDS = ("warnings", "errors", "artifact_ids")
MAX_DURABLE_ARTIFACTS = 256
MAX_DURABLE_DIALOGUE_ENTRIES = 256
MAX_DURABLE_PHASE_DIAGNOSTICS = 128
MAX_DURABLE_APPROVALS = 256
MAX_REDUCER_JSON_NODES = 4_096
MAX_DURABLE_APPLICATION_REVIEWS = 1
MAX_DURABLE_PROMPT_REVIEWS = len(PHASE_ORDER)
MAX_REVIEW_ENTRY_CHARS = 400_000
MAX_REVIEW_STRING_CHARS = 100_000

_PHASE_NAMES = frozenset(phase.value for phase in PHASE_ORDER)
_PHASE_STATUS_VALUES = frozenset(status.value for status in PhaseStatus)
_TERMINAL_PHASE_STATUS_VALUES = frozenset(
    {
        PhaseStatus.SUCCEEDED.value,
        PhaseStatus.FAILED.value,
        PhaseStatus.SKIPPED.value,
        PhaseStatus.ABORTED.value,
    }
)


def _exact_list(value: list[Any] | None, *, label: str) -> list[Any]:
    if value is None:
        return []
    if type(value) is not list:
        raise ValueError(f"checkpointed {label} must be a list")
    return value


def _bounded_exact_list(
    value: list[Any] | None,
    *,
    label: str,
    limit: int,
) -> list[Any]:
    raw = _exact_list(value, label=label)
    return raw if len(raw) <= limit else raw[-limit:]


def _phase_list_limit(field: str) -> int:
    if field == "tool_calls":
        return MAX_TOOL_CALL_RECORDS
    if field == "approvals":
        return MAX_DURABLE_APPROVALS
    if field in {"warnings", "errors"}:
        return MAX_DURABLE_PHASE_DIAGNOSTICS
    return MAX_DURABLE_ARTIFACTS


def _validate_reducer_json(value: Any, *, phase_entry: bool = False) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_REDUCER_JSON_NODES or depth > 8:
            raise ValueError("checkpointed reducer entry is too large")
        if type(current) is dict:
            if len(current) > 128 or any(type(key) is not str for key in current):
                raise ValueError("checkpointed reducer entry is invalid")
            for key, nested in current.items():
                if phase_entry and depth == 0 and key in {*_BY_ID_FIELDS, *_PLAIN_LIST_FIELDS}:
                    nested = _bounded_exact_list(
                        nested,
                        label=f"phase {key}",
                        limit=_phase_list_limit(key),
                    )
                stack.append((nested, depth + 1))
        elif type(current) is list:
            if len(current) > 256:
                raise ValueError("checkpointed reducer entry is too large")
            stack.extend((nested, depth + 1) for nested in current)
        elif type(current) is str:
            if len(current) > 50_000 or "\x00" in current:
                raise ValueError("checkpointed reducer entry is invalid")
        elif current is None or type(current) is bool:
            continue
        elif type(current) is int:
            if current.bit_length() > 256:
                raise ValueError("checkpointed reducer integer is too large")
        elif type(current) is float:
            if not math.isfinite(current):
                raise ValueError("checkpointed reducer number is invalid")
        else:
            raise ValueError("checkpointed reducer entry contains an unsupported value")


def _validate_phase_entry_json(entry: JsonDict) -> None:
    if type(entry) is not dict:
        raise ValueError("checkpointed phase entry must be an object")
    _validate_reducer_json(entry, phase_entry=True)


def _validate_phase_entry(entry: JsonDict) -> None:
    _validate_phase_entry_json(entry)
    phase = entry.get("phase")
    if phase is not None and (type(phase) is not str or phase not in _PHASE_NAMES):
        raise ValueError("checkpointed phase entry has an invalid phase")
    status = entry.get("status")
    if status is not None and (type(status) is not str or status not in _PHASE_STATUS_VALUES):
        raise ValueError("checkpointed phase entry has an invalid status")
    attempt = entry.get("attempt")
    if attempt is not None and (type(attempt) is not int or not 1 <= attempt <= 1_000_000):
        raise ValueError("checkpointed phase entry has an invalid attempt")
    scan_entry = {
        field: (
            _bounded_exact_list(
                value,
                label=f"phase {field}",
                limit=_phase_list_limit(field),
            )
            if field in {*_BY_ID_FIELDS, *_PLAIN_LIST_FIELDS}
            else value
        )
        for field, value in entry.items()
    }
    if contains_credential_material(
        scan_entry,
        max_nodes=MAX_REDUCER_JSON_NODES,
        max_total_chars=200_000,
    ):
        raise ValueError("checkpointed phase entry contains credential material")


def _validate_review_entry(entry: JsonDict) -> None:
    """Bound one prompt/application review without importing graph-node code."""

    if type(entry) is not dict:
        raise ValueError("checkpointed prompt review must be an object")
    stack: list[tuple[Any, int]] = [(entry, 0)]
    nodes = 0
    total_chars = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 128 or depth > 6:
            raise ValueError("checkpointed prompt review is too large")
        if type(current) is dict:
            if len(current) > 32:
                raise ValueError("checkpointed prompt review is too large")
            for key, nested in current.items():
                if type(key) is not str or not 1 <= len(key) <= 255 or "\x00" in key:
                    raise ValueError("checkpointed prompt review is invalid")
                total_chars += len(key)
                stack.append((nested, depth + 1))
        elif type(current) is list:
            if len(current) > 32:
                raise ValueError("checkpointed prompt review is too large")
            stack.extend((nested, depth + 1) for nested in current)
        elif type(current) is str:
            if len(current) > MAX_REVIEW_STRING_CHARS or "\x00" in current:
                raise ValueError("checkpointed prompt review is invalid")
            total_chars += len(current)
        elif current is None or type(current) is bool:
            continue
        elif type(current) is int:
            if current.bit_length() > 256:
                raise ValueError("checkpointed prompt review is invalid")
        elif type(current) is float:
            if not math.isfinite(current):
                raise ValueError("checkpointed prompt review is invalid")
        else:
            raise ValueError("checkpointed prompt review contains an unsupported value")
        if total_chars > MAX_REVIEW_ENTRY_CHARS:
            raise ValueError("checkpointed prompt review is too large")
    if contains_credential_material(
        entry,
        max_nodes=128,
        max_total_chars=MAX_REVIEW_ENTRY_CHARS,
    ):
        raise ValueError("checkpointed prompt review contains credential material")


def _validate_review_map(
    value: dict[str, JsonDict] | None,
    *,
    label: str,
    max_entries: int,
    allowed_keys: frozenset[str] | None = None,
) -> dict[str, JsonDict]:
    if value is None:
        return {}
    if type(value) is not dict or len(value) > max_entries:
        raise ValueError(f"checkpointed {label} are invalid")
    for key, review in value.items():
        if (
            type(key) is not str
            or not 1 <= len(key) <= 255
            or key != key.strip()
            or "\x00" in key
            or contains_credential_material(key)
            or (allowed_keys is not None and key not in allowed_keys)
        ):
            raise ValueError(f"checkpointed {label} key is invalid")
        _validate_review_entry(review)
    return value


def _merge_lists_by_id(current: list[JsonDict], incoming: list[JsonDict]) -> list[JsonDict]:
    def stable_entry(entry: JsonDict) -> JsonDict:
        if type(entry) is not dict:
            raise ValueError("checkpointed reducer entry must be an object")
        _validate_reducer_json(entry)
        if contains_credential_material(entry, max_nodes=512, max_total_chars=200_000):
            raise ValueError("checkpointed reducer entry contains credential material")
        entry_id = entry.get("id")
        if entry_id is not None:
            if type(entry_id) is not str or not 1 <= len(entry_id) <= 256 or "\x00" in entry_id:
                raise ValueError("checkpointed reducer entry id is invalid")
            return dict(entry)
        # Missing and explicit-null ids are the same legacy representation. Hash
        # the payload without ``id`` so replay cannot preserve two copies of the
        # same logical entry merely because one serializer emitted ``null``.
        idless_entry = {key: value for key, value in entry.items() if key != "id"}
        canonical = json.dumps(idless_entry, sort_keys=True, separators=(",", ":"))
        # An explicit JSON null has the same legacy meaning as a missing id. Put
        # the derived value last so ``{"id": null}`` cannot overwrite it and
        # poison channels whose consumers require stable string identities.
        return {
            **idless_entry,
            "id": f"derived-{hashlib.sha256(canonical.encode()).hexdigest()[:24]}",
        }

    if type(current) is not list or type(incoming) is not list:
        raise ValueError("checkpointed reducer values must be lists")
    merged: list[JsonDict] = []
    seen: set[str] = set()
    # Revalidate and canonicalize the inherited side too. Older or malformed
    # checkpoints may already contain duplicate ids; retaining them would make
    # the supposedly append-unique channel permanently non-idempotent.
    for raw_entry in current:
        entry = stable_entry(raw_entry)
        if entry["id"] not in seen:
            merged.append(entry)
            seen.add(entry["id"])
    for raw_entry in incoming:
        entry = stable_entry(raw_entry)
        if entry["id"] not in seen:
            merged.append(entry)
            seen.add(entry["id"])
    return merged


def _merge_unique(current: list[Any], incoming: list[Any]) -> list[Any]:
    if any(type(item) is not str for item in (*current, *incoming)):
        raise ValueError("checkpointed phase list values must be strings")
    if contains_credential_material([*current, *incoming], max_nodes=512):
        raise ValueError("checkpointed phase list contains credential material")
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
    _validate_phase_entry(entry)
    bounded = dict(entry)
    for field in _BY_ID_FIELDS:
        if field in bounded:
            limit = MAX_TOOL_CALL_RECORDS if field == "tool_calls" else MAX_DURABLE_APPROVALS
            bounded[field] = _merge_lists_by_id(
                [],
                _bounded_exact_list(
                    bounded.get(field),
                    label=f"phase {field}",
                    limit=limit,
                ),
            )
    for field in _PLAIN_LIST_FIELDS:
        if field in bounded:
            limit = (
                MAX_DURABLE_PHASE_DIAGNOSTICS
                if field in {"warnings", "errors"}
                else MAX_DURABLE_ARTIFACTS
            )
            values = _bounded_exact_list(
                bounded.get(field),
                label=f"phase {field}",
                limit=limit,
            )
            if any(type(item) is not str for item in values):
                raise ValueError(f"checkpointed phase {field} values must be strings")
            if contains_credential_material(values, max_nodes=limit + 1):
                raise ValueError(f"checkpointed phase {field} contains credential material")
            bounded[field] = _cap_plain_phase_list(
                field,
                values,
            )
    return bounded


def _merge_phase_entry(current: JsonDict, incoming: JsonDict) -> JsonDict:
    # A new attempt replaces the entry wholesale (re-run semantics, ADR-0004);
    # within the same attempt, scalars are last-write-wins and lists union.
    _validate_phase_entry_json(current)
    _validate_phase_entry_json(incoming)
    current_attempt = current.get("attempt")
    incoming_attempt = incoming.get("attempt")
    if current_attempt is not None and (
        type(current_attempt) is not int or not 1 <= current_attempt <= 1_000_000
    ):
        raise ValueError("checkpointed phase attempt is invalid")
    if incoming_attempt is not None and (
        type(incoming_attempt) is not int or not 1 <= incoming_attempt <= 1_000_000
    ):
        raise ValueError("checkpointed phase attempt is invalid")
    if incoming_attempt != current_attempt:
        # Every legitimate rerun advances exactly one attempt. A missing, stale,
        # or jumping attempt can otherwise roll a newer provider lease back to a
        # superseded handle/idempotency key or skip the finite attempt budget.
        if current_attempt is not None and (
            incoming_attempt is None or incoming_attempt != current_attempt + 1
        ):
            raise ValueError("checkpointed phase attempt is not monotonic")
        return _bounded_phase_entry(incoming)
    current_status = current.get("status")
    incoming_status = incoming.get("status", current_status)
    if (
        type(current_status) is str
        and current_status in _TERMINAL_PHASE_STATUS_VALUES
        and incoming_status != current_status
    ):
        # A rerun gets a new attempt. Within one attempt the first terminal
        # result is immutable, so a stale node/replay cannot reopen the phase or
        # replace one terminal outcome with another.
        raise ValueError("checkpointed terminal phase status is immutable")
    merged = {**current, **incoming}
    for field in _BY_ID_FIELDS:
        limit = MAX_TOOL_CALL_RECORDS if field == "tool_calls" else MAX_DURABLE_APPROVALS
        merged[field] = _merge_lists_by_id(
            _bounded_exact_list(
                current.get(field),
                label=f"phase {field}",
                limit=limit,
            ),
            _bounded_exact_list(
                incoming.get(field),
                label=f"phase {field}",
                limit=limit,
            ),
        )
        merged[field] = merged[field][-limit:]
    for field in _PLAIN_LIST_FIELDS:
        limit = (
            MAX_DURABLE_PHASE_DIAGNOSTICS
            if field in {"warnings", "errors"}
            else MAX_DURABLE_ARTIFACTS
        )
        current_values = _bounded_exact_list(
            current.get(field),
            label=f"phase {field}",
            limit=limit,
        )
        incoming_values = _bounded_exact_list(
            incoming.get(field),
            label=f"phase {field}",
            limit=limit,
        )
        merged[field] = _cap_plain_phase_list(
            field,
            _merge_unique(current_values, incoming_values),
        )
    _validate_phase_entry(merged)
    return merged


def merge_phase_results(
    left: dict[str, JsonDict] | None, right: dict[str, JsonDict] | None
) -> dict[str, JsonDict]:
    if left is not None and type(left) is not dict:
        raise ValueError("checkpointed phase results must be an object")
    if right is not None and type(right) is not dict:
        raise ValueError("checkpointed phase results must be an object")
    for source in (left, right):
        if source is not None and (
            len(source) > len(_PHASE_NAMES)
            or any(type(phase) is not str or phase not in _PHASE_NAMES for phase in source)
        ):
            raise ValueError("checkpointed phase name is invalid")

    def validated_source(source: dict[str, JsonDict] | None) -> dict[str, JsonDict]:
        result: dict[str, JsonDict] = {}
        for phase, entry in (source or {}).items():
            if type(phase) is not str or type(entry) is not dict:
                raise ValueError("checkpointed phase result is invalid")
            bounded = _bounded_phase_entry(entry)
            recorded_phase = bounded.get("phase")
            if recorded_phase is not None and recorded_phase != phase:
                raise ValueError("checkpointed phase result is bound to another phase")
            result[phase] = bounded
        return result

    # Every incoming entry and every untouched inherited entry is fully
    # revalidated.  An overlapping inherited entry is structurally checked and
    # then validated *after* merge so a node can deliberately scrub a poisoned
    # scalar from an old checkpoint (for example, a credential-bearing summary or
    # connection id).  Unsafe inherited list members cannot be scrubbed this way
    # because same-attempt list semantics are additive and the final merged entry
    # still rejects them.
    safe_right = validated_source(right)
    if not left:
        return safe_right
    if not safe_right:
        return validated_source(left)
    merged: dict[str, JsonDict] = {}
    for phase, current in left.items():
        if type(current) is not dict:
            raise ValueError("checkpointed phase result is invalid")
        recorded_phase = current.get("phase")
        if recorded_phase is not None and recorded_phase != phase:
            raise ValueError("checkpointed phase result is bound to another phase")
        incoming = safe_right.get(phase)
        if incoming is None:
            merged[phase] = _bounded_phase_entry(current)
        else:
            merged[phase] = _merge_phase_entry(current, incoming)
    for phase, incoming in safe_right.items():
        if phase not in merged:
            merged[phase] = incoming
    return merged


def append_unique_by_id(
    left: list[JsonDict] | None, right: list[JsonDict] | None
) -> list[JsonDict]:
    return _merge_lists_by_id(
        _bounded_exact_list(left, label="state channel", limit=MAX_DURABLE_ARTIFACTS),
        _bounded_exact_list(right, label="state channel", limit=MAX_DURABLE_ARTIFACTS),
    )


def merge_artifacts(left: list[JsonDict] | None, right: list[JsonDict] | None) -> list[JsonDict]:
    """Retain a bounded recent window; durable ownership lives outside checkpoints."""

    return append_unique_by_id(left, right)[-MAX_DURABLE_ARTIFACTS:]


def _dialogue_attempt(entry: JsonDict) -> int:
    raw_attempt = entry.get("attempt", 1)
    if type(raw_attempt) is not int or not 1 <= raw_attempt <= 1_000_000:
        raise ValueError("checkpointed dialogue attempt is invalid")
    return raw_attempt


def merge_dialogue(left: list[JsonDict] | None, right: list[JsonDict] | None) -> list[JsonDict]:
    """Keep only each phase's latest attempt and a bounded global tail."""

    merged = append_unique_by_id(left, right)
    latest_by_phase: dict[str, int] = {}
    for entry in merged:
        phase = entry.get("phase")
        if type(phase) is not str:
            raise ValueError("checkpointed dialogue phase is invalid")
        latest_by_phase[phase] = max(latest_by_phase.get(phase, 1), _dialogue_attempt(entry))
    current = [
        entry
        for entry in merged
        if _dialogue_attempt(entry) == latest_by_phase.get(entry["phase"], 1)
    ]
    return current[-MAX_DURABLE_DIALOGUE_ENTRIES:]


def merge_latest_by_id(left: list[JsonDict] | None, right: list[JsonDict] | None) -> list[JsonDict]:
    """Idempotently append new ids and replace existing ids in place."""

    merged = _merge_lists_by_id(
        [],
        _bounded_exact_list(left, label="state channel", limit=MAX_DURABLE_ARTIFACTS),
    )
    positions = {entry.get("id"): index for index, entry in enumerate(merged)}
    for incoming in _merge_lists_by_id(
        [],
        _bounded_exact_list(right, label="state channel", limit=MAX_DURABLE_ARTIFACTS),
    ):
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
    safe_left = _validate_review_map(
        left,
        label="prompt reviews",
        max_entries=MAX_DURABLE_PROMPT_REVIEWS,
        allowed_keys=_PHASE_NAMES,
    )
    safe_right = _validate_review_map(
        right,
        label="prompt reviews",
        max_entries=MAX_DURABLE_PROMPT_REVIEWS,
        allowed_keys=_PHASE_NAMES,
    )
    merged = {**safe_left, **safe_right}
    return dict(
        _validate_review_map(
            merged,
            label="prompt reviews",
            max_entries=MAX_DURABLE_PROMPT_REVIEWS,
            allowed_keys=_PHASE_NAMES,
        )
    )


def merge_application_reviews(
    left: dict[str, JsonDict] | None, right: dict[str, JsonDict] | None
) -> dict[str, JsonDict]:
    # Application prompt edits are scoped to one *run*, not the whole thread.
    # plan_resolver deliberately emits an explicit empty update at every new run
    # boundary. Treat that as a reset without traversing legacy content: this is
    # also the safe recovery path for a credential-bearing or oversized old value.
    if right is not None and type(right) is dict and len(right) == 0:
        if left is not None and type(left) is not dict:
            raise ValueError("checkpointed application prompt reviews are invalid")
        return {}
    safe_left = _validate_review_map(
        left,
        label="application prompt reviews",
        max_entries=MAX_DURABLE_APPLICATION_REVIEWS,
    )
    safe_right = _validate_review_map(
        right,
        label="application prompt reviews",
        max_entries=MAX_DURABLE_APPLICATION_REVIEWS,
    )
    merged = {**safe_left, **safe_right}
    return dict(
        _validate_review_map(
            merged,
            label="application prompt reviews",
            max_entries=MAX_DURABLE_APPLICATION_REVIEWS,
        )
    )


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
    application_reviews: Annotated[dict[str, JsonDict], merge_application_reviews]
    artifacts: Annotated[list[JsonDict], merge_artifacts]
    dialogue: Annotated[list[JsonDict], merge_dialogue]
    context_packets: Annotated[list[JsonDict], merge_context_packets]
    engine_handle: JsonDict | None
