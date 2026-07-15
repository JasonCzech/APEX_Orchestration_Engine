"""Shared resource-budget validation for pipeline creation, auth, and execution.

The same helpers are intentionally called at all three boundaries. The API catches
ordinary callers early, the LangGraph auth hook covers direct SDK calls, and graph
nodes revalidate assistant-inherited and checkpointed values before spending money.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from string import Formatter
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError

from apex.domain.input_limits import ScopeId, validation_error_summary
from apex.domain.pipeline import (
    MAX_CONTEXT_ID_CHARS,
    MAX_CONTEXT_REF_CHARS,
    MAX_CONTEXT_SOURCE_CHARS,
    MAX_CONTEXT_SUMMARY_CHARS,
    MAX_CONTEXT_TEXT_CHARS,
    MAX_CONTEXT_TITLE_CHARS,
    ContextPacket,
    ExternalResults,
)
from apex.settings import ApexSettings, get_settings

MAX_MODEL_NAME_CHARS = 200
MAX_PROMPT_PART_CHARS_HARD = 100_000
MAX_GATE_STRING_CHARS_HARD = 50_000
MAX_STATELESS_SUBJECT_CHARS = 2_000
MAX_WORK_ITEM_KEY_CHARS = 256
MAX_WORK_ITEM_KEYS_HARD = 100
MAX_STATELESS_MAPPING_KEY_CHARS = 256

CONTEXT_RUN_INPUT_KEYS = frozenset(
    {"app_id", "document_packets", "project_id", "subject", "work_item_keys"}
)
PLAYGROUND_RUN_INPUT_KEYS = frozenset({"app_id", "project_id", "prompt", "sample_input"})
_FORMAT_NUMBER_RE = re.compile(r"\d+")
_SAFE_SERVER_OWNED_RUN_FIELDS = frozenset(
    {
        "approvals",
        "artifacts",
        "dialogue",
        "engine_handle",
        "events",
        "phase_results",
        "prompt_reviews",
        "run_aborted",
        "run_config",
    }
)
_SAFE_GATE_PATH_FIELDS = frozenset(
    {
        "action",
        "additional_context",
        "application",
        "message",
        "prompt",
        "system",
        "user",
    }
)

_Subject = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_STATELESS_SUBJECT_CHARS,
    ),
]
_WorkItemKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_WORK_ITEM_KEY_CHARS),
]
_SCOPE_ID_ADAPTER = TypeAdapter(ScopeId)


class _StrictRunInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ContextDocumentPacket(_StrictRunInputModel):
    """Exact packet shape accepted by the stateless context graph."""

    id: str = Field(min_length=1, max_length=MAX_CONTEXT_ID_CHARS)
    source: str = Field(min_length=1, max_length=MAX_CONTEXT_SOURCE_CHARS)
    title: str = Field(min_length=1, max_length=MAX_CONTEXT_TITLE_CHARS)
    summary: str | None = Field(default=None, max_length=MAX_CONTEXT_SUMMARY_CHARS)
    ref: str | None = Field(default=None, max_length=MAX_CONTEXT_REF_CHARS)
    text: str | None = Field(default=None, max_length=MAX_CONTEXT_TEXT_CHARS)


class ContextRunInput(_StrictRunInputModel):
    subject: _Subject
    work_item_keys: list[_WorkItemKey] = Field(
        default_factory=list, max_length=MAX_WORK_ITEM_KEYS_HARD
    )
    document_packets: list[ContextDocumentPacket] = Field(default_factory=list, max_length=64)
    project_id: ScopeId | None = None
    app_id: ScopeId | None = None


class PlaygroundPrompt(_StrictRunInputModel):
    system: str = Field(default="", max_length=MAX_PROMPT_PART_CHARS_HARD)
    user: str = Field(default="", max_length=MAX_PROMPT_PART_CHARS_HARD)


class PlaygroundRunInput(_StrictRunInputModel):
    prompt: PlaygroundPrompt = Field(default_factory=PlaygroundPrompt)
    sample_input: dict[str, Any] = Field(default_factory=dict)
    project_id: ScopeId | None = None
    app_id: ScopeId | None = None


# LangGraph run input is merged into graph state. Only these caller-owned keys
# may cross the public run-creation boundary; every other PipelineState field is
# produced by graph nodes or restored from a server-owned checkpoint.
PUBLIC_RUN_INPUT_KEYS = frozenset(
    {
        "app_id",
        "context_packets",
        "document_packets",
        "external_results",
        "project_id",
        "prompt",
        "request",
        "sample_input",
        "subject",
        "title",
        "work_item_keys",
    }
)


def validate_model_by_phase(
    model_by_phase: Mapping[Any, str], *, settings: ApexSettings | None = None
) -> None:
    """Reject model overrides outside the deployment-owned exact allow-list."""

    selected = {str(model).strip() for model in model_by_phase.values()}
    if any(not model or len(model) > MAX_MODEL_NAME_CHARS or "\x00" in model for model in selected):
        raise ValueError("model_by_phase values must be 1-200 character model names")
    allowed = set((settings or get_settings()).llm.allowed_models)
    denied = sorted(selected - allowed)
    if denied:
        raise ValueError(
            "model_by_phase contains a model not allowed by APEX_LLM__ALLOWED_MODELS"
        )


def validate_context_packets(
    packets: Sequence[Any] | None, *, settings: ApexSettings | None = None
) -> list[dict[str, Any]]:
    """Validate packet fields, unique ids, count, and complete serialized size."""

    if packets is None:
        return []
    if isinstance(packets, str | bytes | bytearray) or not isinstance(packets, Sequence):
        raise ValueError("context_packets must be a list")
    limits = (settings or get_settings()).runs
    if len(packets) > limits.max_context_packets:
        raise ValueError(
            f"context_packets exceeds the deployment limit ({limits.max_context_packets})"
        )

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    rendered_chars = 2  # JSON list brackets
    for index, raw in enumerate(packets):
        try:
            packet = raw if isinstance(raw, ContextPacket) else ContextPacket.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(
                f"context_packets[{index}] is invalid: {validation_error_summary(exc)}"
            ) from exc
        if packet.id in seen:
            raise ValueError("context_packets contains a duplicate id")
        seen.add(packet.id)
        payload = packet.model_dump(mode="json", exclude_none=True)
        # This counts every accepted field, including id/source/title/summary/ref/text,
        # plus JSON keys and separators. It is stricter than counting only body text.
        rendered_chars += len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) + (
            1 if normalized else 0
        )
        normalized.append(payload)
    if rendered_chars > limits.max_context_chars_total:
        raise ValueError(
            "context_packets rendered payload exceeds the deployment limit "
            f"({limits.max_context_chars_total} characters)"
        )
    return normalized


def validate_pipeline_input(input_payload: Mapping[str, Any]) -> None:
    """Validate run input that can contribute directly to model context/state."""

    if "title" in input_payload:
        title = input_payload.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 500 or "\x00" in title:
            raise ValueError("run input title must be a non-empty string of at most 500 characters")
    if "request" in input_payload:
        request = input_payload.get("request")
        if not isinstance(request, str) or len(request) > 20_000 or "\x00" in request:
            raise ValueError("run input request must be a string of at most 20000 characters")
    for field_name in ("project_id", "app_id"):
        value = input_payload.get(field_name)
        if value is not None:
            try:
                _SCOPE_ID_ADAPTER.validate_python(value, strict=True)
            except ValidationError as exc:
                raise ValueError(
                    f"run input {field_name} is invalid: {validation_error_summary(exc)}"
                ) from exc
    if input_payload.get("external_results") is not None:
        ExternalResults.model_validate(input_payload["external_results"])
    if input_payload.get("context_packets") is not None:
        validate_context_packets(input_payload["context_packets"])


def _validate_json_tree(
    payload: Any,
    *,
    label: str,
    settings: ApexSettings,
) -> None:
    """Iteratively validate a strict JSON tree before any recursive parser sees it."""

    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")

    limits = settings.runs
    stack: list[tuple[Any, int]] = [(payload, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_stateless_payload_nodes:
            raise ValueError(
                f"{label} exceeds the node limit ({limits.max_stateless_payload_nodes})"
            )
        if depth > limits.max_stateless_payload_depth:
            raise ValueError(f"{label} nesting exceeds {limits.max_stateless_payload_depth} levels")

        if isinstance(value, dict):
            remaining = limits.max_stateless_payload_nodes - nodes - len(stack)
            if len(value) > remaining:
                raise ValueError(
                    f"{label} exceeds the node limit ({limits.max_stateless_payload_nodes})"
                )
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} mapping keys must be strings")
                if len(key) > MAX_STATELESS_MAPPING_KEY_CHARS:
                    raise ValueError(
                        f"{label} mapping keys must not exceed "
                        f"{MAX_STATELESS_MAPPING_KEY_CHARS} characters"
                    )
                if "\x00" in key:
                    raise ValueError(f"{label} mapping keys must not contain U+0000")
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            remaining = limits.max_stateless_payload_nodes - nodes - len(stack)
            if len(value) > remaining:
                raise ValueError(
                    f"{label} exceeds the node limit ({limits.max_stateless_payload_nodes})"
                )
            for item in value:
                stack.append((item, depth + 1))
        elif isinstance(value, str):
            if "\x00" in value:
                raise ValueError(f"{label} strings must not contain U+0000")
        elif value is None or isinstance(value, bool | int):
            continue
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{label} numbers must be finite")
        else:
            raise ValueError(f"{label} contains unsupported {type(value).__name__} value")

    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be serializable as finite JSON") from exc
    if len(serialized) > limits.max_stateless_payload_bytes:
        raise ValueError(
            f"{label} serialized payload exceeds the deployment limit "
            f"({limits.max_stateless_payload_bytes} bytes)"
        )


def validate_context_run_input(
    input_payload: Mapping[str, Any], *, settings: ApexSettings | None = None
) -> ContextRunInput:
    """Validate and normalize all caller-controlled context-graph input."""

    active_settings = settings or get_settings()
    payload = dict(input_payload)
    _validate_json_tree(payload, label="context run input", settings=active_settings)
    try:
        validated = ContextRunInput.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise ValueError(
            f"context run input is invalid: {validation_error_summary(exc)}"
        ) from exc

    limits = active_settings.runs
    if len(validated.work_item_keys) > limits.max_work_item_keys:
        raise ValueError(
            f"work_item_keys exceeds the deployment limit ({limits.max_work_item_keys})"
        )
    if len(set(validated.work_item_keys)) != len(validated.work_item_keys):
        raise ValueError("work_item_keys must not contain duplicates")

    # Reuse the pipeline packet validator for its deployment-owned packet count,
    # duplicate-id, per-field, and complete serialized-character budgets.
    validate_context_packets(
        [
            packet.model_dump(mode="json", exclude_none=True)
            for packet in validated.document_packets
        ],
        settings=active_settings,
    )
    return validated


def validate_playground_run_input(
    input_payload: Mapping[str, Any], *, settings: ApexSettings | None = None
) -> PlaygroundRunInput:
    """Validate and normalize prompt-playground input as a bounded JSON tree."""

    active_settings = settings or get_settings()
    payload = dict(input_payload)
    _validate_json_tree(payload, label="playground run input", settings=active_settings)
    try:
        validated = PlaygroundRunInput.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise ValueError(
            f"playground run input is invalid: {validation_error_summary(exc)}"
        ) from exc
    validate_prompt_parts(
        system=validated.prompt.system,
        user=validated.prompt.user,
        settings=active_settings,
    )
    validate_playground_render_budget(
        validated.prompt,
        validated.sample_input,
        settings=active_settings,
    )
    return validated


def validate_playground_render_budget(
    prompt: PlaygroundPrompt,
    sample_input: Mapping[str, Any],
    *,
    settings: ApexSettings | None = None,
) -> None:
    """Conservatively reject format expansion before ``str.format_map`` runs.

    Prompt size alone does not bound output: a short placeholder can repeat a
    large sample value thousands of times, and a format width/precision can ask
    Python to allocate a huge string.  This preflight intentionally overestimates
    scalar conversion size, then the graph enforces the exact rendered limit too.
    """

    limit = (settings or get_settings()).runs.max_model_input_chars
    estimated_chars = 0
    for template in (prompt.system, prompt.user):
        try:
            parts = list(Formatter().parse(template))
        except ValueError:
            # ``render_template`` preserves malformed/literal-brace templates.
            estimated_chars += len(template)
            continue
        for literal, field_name, format_spec, conversion in parts:
            estimated_chars += len(literal)
            if field_name is None:
                continue
            format_spec = format_spec or ""
            if (
                not field_name
                or any(marker in field_name for marker in (".", "[", "]"))
                or "{" in format_spec
                or "}" in format_spec
            ):
                raise ValueError(
                    "playground placeholders must use top-level sample_input keys "
                    "and static format specifications"
                )
            if conversion not in {None, "s", "r", "a"}:
                raise ValueError("playground prompt contains an unsupported conversion")
            widths = [int(value) for value in _FORMAT_NUMBER_RE.findall(format_spec)]
            if any(width > limit for width in widths):
                raise ValueError(
                    f"playground format width/precision exceeds the rendered limit ({limit})"
                )
            raw_value = sample_input.get(field_name, "{" + field_name + "}")
            try:
                # repr/ascii escaping and non-decimal integer formatting can be
                # wider than str(); eight times is a deliberately conservative cap.
                converted_bound = len(str(raw_value)) * 8 + 2
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("playground sample value cannot be rendered safely") from exc
            estimated_chars += max(converted_bound, *(widths or [0]))
            if estimated_chars > limit:
                raise ValueError(
                    f"rendered model input exceeds the deployment limit ({limit} characters)"
                )
    if estimated_chars > limit:
        raise ValueError(f"rendered model input exceeds the deployment limit ({limit} characters)")


def validate_public_run_input(input_payload: Mapping[str, Any]) -> None:
    """Reject caller attempts to inject server-owned LangGraph state.

    Pipeline state contains phase results, reviews, artifacts, transcripts, and
    other fields that influence routing and billing. LangGraph merges run input
    directly into that state, so an explicit allow-list is required at the auth
    boundary. The union also covers the stateless context and playground graphs.
    """

    unexpected = sorted(str(key) for key in input_payload if key not in PUBLIC_RUN_INPUT_KEYS)
    if unexpected:
        safe_names = [name for name in unexpected if name in _SAFE_SERVER_OWNED_RUN_FIELDS]
        suffix = f": {', '.join(safe_names)}" if safe_names else ""
        raise ValueError(
            "run input contains server-owned or unsupported field(s)" + suffix
        )
    validate_pipeline_input(input_payload)


def validate_gate_payload(payload: Any, *, settings: ApexSettings | None = None) -> None:
    """Bound a gate resume/edit payload without recursive traversal.

    Prompt parts receive the separately configured prompt-part budget. All other
    strings (instructions, discussion messages, notes, ids, and mapping keys) use
    the smaller gate-string budget. Node/depth caps also stop deeply nested JSON.
    """

    limits = (settings or get_settings()).runs
    stack: list[tuple[Any, tuple[str, ...], int]] = [(payload, (), 0)]
    total_chars = 0
    nodes = 0
    while stack:
        value, path, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_gate_payload_nodes:
            raise ValueError(
                f"gate payload exceeds the node limit ({limits.max_gate_payload_nodes})"
            )
        if depth > 16:
            raise ValueError("gate payload nesting exceeds 16 levels")
        if isinstance(value, BaseModel):
            stack.append((value.model_dump(mode="json", exclude_none=True), path, depth))
            continue
        if isinstance(value, str):
            if "\x00" in value:
                label = _safe_gate_path(path)
                raise ValueError(f"gate payload {label} must not contain U+0000")
            is_prompt_part = (
                len(path) >= 2
                and path[-2] == "prompt"
                and path[-1]
                in {
                    "system",
                    "user",
                    "application",
                }
            )
            per_string_limit = (
                limits.max_prompt_part_chars if is_prompt_part else limits.max_gate_string_chars
            )
            if len(value) > per_string_limit:
                label = _safe_gate_path(path)
                raise ValueError(f"gate payload {label} exceeds {per_string_limit} characters")
            total_chars += len(value)
        elif isinstance(value, Mapping):
            remaining = limits.max_gate_payload_nodes - nodes - len(stack)
            if len(value) > remaining:
                raise ValueError(
                    f"gate payload exceeds the node limit ({limits.max_gate_payload_nodes})"
                )
            for key, item in value.items():
                key_text = str(key)
                if len(key_text) > 256:
                    raise ValueError("gate payload mapping keys must not exceed 256 characters")
                if "\x00" in key_text:
                    raise ValueError("gate payload mapping keys must not contain U+0000")
                total_chars += len(key_text)
                stack.append((item, (*path, key_text), depth + 1))
        elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            remaining = limits.max_gate_payload_nodes - nodes - len(stack)
            if len(value) > remaining:
                raise ValueError(
                    f"gate payload exceeds the node limit ({limits.max_gate_payload_nodes})"
                )
            for item in value:
                stack.append((item, (*path, "[]"), depth + 1))
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("gate payload numbers must be finite")
        elif value is not None and not isinstance(value, bool | int):
            raise ValueError(f"gate payload contains unsupported {type(value).__name__} value")
        if total_chars > limits.max_gate_payload_chars:
            raise ValueError(
                "gate payload exceeds the deployment limit "
                f"({limits.max_gate_payload_chars} characters)"
            )


def _safe_gate_path(path: tuple[str, ...]) -> str:
    """Describe gate fields without reflecting attacker-controlled mapping keys."""

    return ".".join(
        part if part in _SAFE_GATE_PATH_FIELDS or part == "[]" else "field"
        for part in path
    ) or "value"


def validate_prompt_parts(
    *,
    system: str = "",
    user: str = "",
    application: str | None = None,
    additional_context: str = "",
    settings: ApexSettings | None = None,
) -> None:
    """Validate editable prompt parts before storing or rendering them."""

    limits = (settings or get_settings()).runs
    parts = {
        "system": system,
        "user": user,
        "application": application or "",
        "additional_context": additional_context,
    }
    for name, value in parts.items():
        if "\x00" in value:
            raise ValueError(f"{name} must not contain U+0000")
        per_part = (
            limits.max_gate_string_chars
            if name == "additional_context"
            else limits.max_prompt_part_chars
        )
        if len(value) > per_part:
            raise ValueError(f"{name} exceeds the deployment limit ({per_part} characters)")
    total = sum(len(value) for value in parts.values())
    if total > limits.max_model_input_chars:
        raise ValueError(
            f"prompt exceeds the deployment limit ({limits.max_model_input_chars} characters)"
        )


def validate_rendered_model_input(
    system: str, user: str, *, settings: ApexSettings | None = None
) -> None:
    """Final defense after context evidence and revision instructions are rendered."""

    limit = (settings or get_settings()).runs.max_model_input_chars
    if len(system) + len(user) > limit:
        raise ValueError(f"rendered model input exceeds the deployment limit ({limit} characters)")


__all__ = [
    "CONTEXT_RUN_INPUT_KEYS",
    "ContextDocumentPacket",
    "ContextRunInput",
    "MAX_GATE_STRING_CHARS_HARD",
    "MAX_MODEL_NAME_CHARS",
    "MAX_PROMPT_PART_CHARS_HARD",
    "MAX_STATELESS_SUBJECT_CHARS",
    "MAX_WORK_ITEM_KEY_CHARS",
    "MAX_WORK_ITEM_KEYS_HARD",
    "PLAYGROUND_RUN_INPUT_KEYS",
    "PUBLIC_RUN_INPUT_KEYS",
    "PlaygroundPrompt",
    "PlaygroundRunInput",
    "validate_context_packets",
    "validate_context_run_input",
    "validate_gate_payload",
    "validate_model_by_phase",
    "validate_pipeline_input",
    "validate_playground_render_budget",
    "validate_playground_run_input",
    "validate_public_run_input",
    "validate_prompt_parts",
    "validate_rendered_model_input",
]
