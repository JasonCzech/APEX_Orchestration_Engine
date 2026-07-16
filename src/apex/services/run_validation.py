"""Shared resource-budget validation for pipeline creation, auth, and execution.

The same helpers are intentionally called at all three boundaries. The API catches
ordinary callers early, the LangGraph auth hook covers direct SDK calls, and graph
nodes revalidate assistant-inherited and checkpointed values before spending money.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from string import Formatter
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import RecordId, ScopeId, validation_error_summary
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
MAX_MODEL_PHASE_OVERRIDES = 32
MAX_PROMPT_PART_CHARS_HARD = 100_000
MAX_GATE_STRING_CHARS_HARD = 50_000
MAX_STATELESS_SUBJECT_CHARS = 2_000
MAX_WORK_ITEM_KEY_CHARS = 256
MAX_WORK_ITEM_KEYS_HARD = 100
MAX_STATELESS_MAPPING_KEY_CHARS = 256

CONTEXT_RUN_INPUT_KEYS = frozenset(
    {
        "app_id",
        "document_packets",
        "project_id",
        "subject",
        "work_item_keys",
        "work_tracking_connection_id",
    }
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
    work_tracking_connection_id: RecordId | None = None

    @model_validator(mode="after")
    def require_exact_work_tracking_affinity(self) -> ContextRunInput:
        connection_id = self.work_tracking_connection_id
        if connection_id is not None and (
            connection_id != connection_id.strip()
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in connection_id)
        ):
            raise ValueError("work_tracking_connection_id is invalid")
        if self.work_item_keys and connection_id is None:
            raise ValueError(
                "work_tracking_connection_id is required when work_item_keys are present"
            )
        if not self.work_item_keys and connection_id is not None:
            raise ValueError(
                "work_tracking_connection_id requires at least one work_item_key"
            )
        return self


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
        "work_tracking_connection_id",
    }
)


def validate_model_by_phase(
    model_by_phase: Mapping[Any, str], *, settings: ApexSettings | None = None
) -> None:
    """Reject model overrides outside the deployment-owned exact allow-list."""

    if type(model_by_phase) is not dict or len(model_by_phase) > MAX_MODEL_PHASE_OVERRIDES:
        raise ValueError("model_by_phase must be an object of model-name strings")
    selected: set[str] = set()
    for model in model_by_phase.values():
        if (
            type(model) is not str
            or not 1 <= len(model) <= MAX_MODEL_NAME_CHARS
            or model != model.strip()
            or "\x00" in model
        ):
            raise ValueError("model_by_phase values must be 1-200 character model names")
        selected.add(model)
    allowed = set((settings or get_settings()).llm.allowed_models)
    denied = sorted(selected - allowed)
    if denied:
        raise ValueError("model_by_phase contains a model not allowed by APEX_LLM__ALLOWED_MODELS")


def validate_context_packets(
    packets: list[Any] | None, *, settings: ApexSettings | None = None
) -> list[dict[str, Any]]:
    """Validate packet fields, unique ids, count, and complete serialized size."""

    if packets is None:
        return []
    if type(packets) is not list:
        raise ValueError("context_packets must be a list")
    limits = (settings or get_settings()).runs
    if len(packets) > limits.max_context_packets:
        raise ValueError(
            f"context_packets exceeds the deployment limit ({limits.max_context_packets})"
        )

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    rendered_chars = 2  # JSON list brackets
    allowed_fields = {"id", "source", "title", "summary", "ref", "text"}
    for index, raw in enumerate(packets):
        if type(raw) is not dict or any(
            type(key) is not str or key not in allowed_fields for key in raw
        ):
            raise ValueError(f"context_packets[{index}] must be an object")
        if any(value is not None and type(value) is not str for value in raw.values()):
            raise ValueError(f"context_packets[{index}] contains an unsupported value")
        # Inspect the raw bounded packet before model validation. A credential in
        # a field that also violates a length constraint would otherwise survive
        # on Pydantic's rejected-input exception chain.
        if contains_credential_material(
            raw,
            max_nodes=16,
            # The deployment's aggregate context budget may be lower than the
            # sum of individually valid packet fields. Scan the complete bounded
            # model envelope here; the rendered-size check below remains
            # responsible for reporting a deployment-budget violation.
            max_total_chars=max(
                limits.max_context_chars_total,
                MAX_CONTEXT_ID_CHARS
                + MAX_CONTEXT_SOURCE_CHARS
                + MAX_CONTEXT_TITLE_CHARS
                + MAX_CONTEXT_SUMMARY_CHARS
                + MAX_CONTEXT_REF_CHARS
                + MAX_CONTEXT_TEXT_CHARS
                + 256,
            ),
        ):
            raise ValueError("context_packets must not contain credential material")
        validation_summary: str | None = None
        try:
            packet = ContextPacket.model_validate(raw, strict=True)
        except ValidationError as exc:
            validation_summary = validation_error_summary(exc)
            packet = None
        if validation_summary is not None:
            raise ValueError(f"context_packets[{index}] is invalid: {validation_summary}")
        assert packet is not None
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
    if contains_credential_material(
        normalized,
        max_nodes=max(1, limits.max_context_packets * 8),
        max_total_chars=max(1, limits.max_context_chars_total),
    ):
        raise ValueError("context_packets must not contain credential material")
    return normalized


def validate_pipeline_input(input_payload: Mapping[str, Any]) -> None:
    """Validate run input that can contribute directly to model context/state."""

    if type(input_payload) is not dict:
        raise ValueError("run input must be a JSON object")
    durable_input = {
        key: input_payload[key]
        for key in ("title", "request", "external_results", "context_packets")
        if key in input_payload
    }
    raw_external = durable_input.get("external_results")
    if type(raw_external) is dict:
        raw_uri = raw_external.get("uri")
        if type(raw_uri) is str:
            invalid_external_uri = False
            try:
                ExternalResults.validate_uri(raw_uri)
            except ValueError:
                invalid_external_uri = True
            if invalid_external_uri:
                # Preserve a specific, capability-free contract error before the
                # generic credential scanner rejects signed query parameters.
                raise ValueError(
                    "run input external_results is invalid: external results uri is invalid"
                )
    # This must precede every Pydantic/TypeAdapter call so rejected-input objects
    # can never retain a credential on an exception chain.
    if contains_credential_material(durable_input):
        raise ValueError("pipeline run input must not contain credential material")
    if "title" in input_payload:
        title = input_payload.get("title")
        if type(title) is not str or not title.strip() or len(title) > 500 or "\x00" in title:
            raise ValueError("run input title must be a non-empty string of at most 500 characters")
    if "request" in input_payload:
        request = input_payload.get("request")
        if type(request) is not str or len(request) > 20_000 or "\x00" in request:
            raise ValueError("run input request must be a string of at most 20000 characters")
    for field_name in ("project_id", "app_id"):
        value = input_payload.get(field_name)
        if value is not None:
            if type(value) is not str:
                raise ValueError(f"run input {field_name} is invalid")
            scope_error_summary: str | None = None
            try:
                _SCOPE_ID_ADAPTER.validate_python(value, strict=True)
            except ValidationError as exc:
                scope_error_summary = validation_error_summary(exc)
            if scope_error_summary is not None:
                raise ValueError(f"run input {field_name} is invalid: {scope_error_summary}")
    external_results = input_payload.get("external_results")
    if external_results is not None:
        if type(external_results) is not dict:
            raise ValueError("run input external_results must be an object")
        settings = get_settings()
        _validate_json_tree(external_results, label="external_results", settings=settings)
        external_error_summary: str | None = None
        try:
            ExternalResults.model_validate(external_results, strict=True)
        except ValidationError as exc:
            external_error_summary = validation_error_summary(exc)
        if external_error_summary is not None:
            raise ValueError(f"run input external_results is invalid: {external_error_summary}")
    context_packets = input_payload.get("context_packets")
    if context_packets is not None:
        if type(context_packets) is not list:
            raise ValueError("context_packets must be a list")
        validate_context_packets(context_packets)


def _validate_json_tree(
    payload: Any,
    *,
    label: str,
    settings: ApexSettings,
) -> None:
    """Iteratively validate a strict JSON tree before any recursive parser sees it."""

    if type(payload) is not dict:
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

        if type(value) is dict:
            remaining = limits.max_stateless_payload_nodes - nodes - len(stack)
            if len(value) > remaining:
                raise ValueError(
                    f"{label} exceeds the node limit ({limits.max_stateless_payload_nodes})"
                )
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError(f"{label} mapping keys must be strings")
                if len(key) > MAX_STATELESS_MAPPING_KEY_CHARS:
                    raise ValueError(
                        f"{label} mapping keys must not exceed "
                        f"{MAX_STATELESS_MAPPING_KEY_CHARS} characters"
                    )
                if "\x00" in key:
                    raise ValueError(f"{label} mapping keys must not contain U+0000")
                stack.append((item, depth + 1))
        elif type(value) is list:
            remaining = limits.max_stateless_payload_nodes - nodes - len(stack)
            if len(value) > remaining:
                raise ValueError(
                    f"{label} exceeds the node limit ({limits.max_stateless_payload_nodes})"
                )
            for item in value:
                stack.append((item, depth + 1))
        elif type(value) is str:
            if len(value) > limits.max_stateless_payload_bytes:
                raise ValueError(
                    f"{label} serialized payload exceeds the deployment limit "
                    f"({limits.max_stateless_payload_bytes} bytes)"
                )
            if "\x00" in value:
                raise ValueError(f"{label} strings must not contain U+0000")
        elif value is None or type(value) is bool:
            continue
        elif type(value) is int:
            if value.bit_length() > 256:
                raise ValueError(f"{label} integers must not exceed 256 bits")
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError(f"{label} numbers must be finite")
        else:
            unsupported = "tuple" if type(value) is tuple else "value"
            raise ValueError(f"{label} contains an unsupported {unsupported}")

    serialized: bytes | None = None
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        pass
    if serialized is None:
        raise ValueError(f"{label} must be serializable as finite JSON")
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
    if type(input_payload) is not dict:
        raise ValueError("context run input must be a JSON object")
    _validate_json_tree(input_payload, label="context run input", settings=active_settings)
    if contains_credential_material(input_payload):
        raise ValueError("context run input must not contain credential material")
    payload = dict(input_payload)
    validation_summary: str | None = None
    try:
        validated = ContextRunInput.model_validate(payload, strict=True)
    except ValidationError as exc:
        validation_summary = validation_error_summary(exc)
        validated = None
    if validation_summary is not None:
        raise ValueError(f"context run input is invalid: {validation_summary}")
    assert validated is not None

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
    if type(input_payload) is not dict:
        raise ValueError("playground run input must be a JSON object")
    _validate_json_tree(input_payload, label="playground run input", settings=active_settings)
    if contains_credential_material(input_payload):
        raise ValueError("playground run input must not contain credential material")
    payload = dict(input_payload)
    validation_summary: str | None = None
    try:
        validated = PlaygroundRunInput.model_validate(payload, strict=True)
    except ValidationError as exc:
        validation_summary = validation_error_summary(exc)
        validated = None
    if validation_summary is not None:
        raise ValueError(f"playground run input is invalid: {validation_summary}")
    assert validated is not None
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
            converted_bound: int | None = None
            try:
                # repr/ascii escaping and non-decimal integer formatting can be
                # wider than str(); eight times is a deliberately conservative cap.
                converted_bound = len(str(raw_value)) * 8 + 2
            except (TypeError, ValueError, OverflowError):
                pass
            if converted_bound is None:
                raise ValueError("playground sample value cannot be rendered safely")
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

    if type(input_payload) is not dict:
        raise ValueError("run input must be a JSON object")
    if any(type(key) is not str for key in input_payload):
        raise ValueError("run input mapping keys must be strings")
    unexpected = sorted(key for key in input_payload if key not in PUBLIC_RUN_INPUT_KEYS)
    if unexpected:
        safe_names = [name for name in unexpected if name in _SAFE_SERVER_OWNED_RUN_FIELDS]
        suffix = f": {', '.join(safe_names)}" if safe_names else ""
        raise ValueError("run input contains server-owned or unsupported field(s)" + suffix)
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
        # This boundary accepts JSON, not arbitrary Python objects.  In particular,
        # do not call provider/user supplied ``model_dump``/``str``/iterator hooks
        # while deciding whether a native LangGraph resume is safe to checkpoint.
        if type(value) is str:
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
        elif type(value) is dict:
            remaining = limits.max_gate_payload_nodes - nodes - len(stack)
            if len(value) > remaining:
                raise ValueError(
                    f"gate payload exceeds the node limit ({limits.max_gate_payload_nodes})"
                )
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("gate payload mapping keys must be strings")
                key_text = key
                if len(key_text) > 256:
                    raise ValueError("gate payload mapping keys must not exceed 256 characters")
                if "\x00" in key_text:
                    raise ValueError("gate payload mapping keys must not contain U+0000")
                total_chars += len(key_text)
                stack.append((item, (*path, key_text), depth + 1))
        elif type(value) is list:
            remaining = limits.max_gate_payload_nodes - nodes - len(stack)
            if len(value) > remaining:
                raise ValueError(
                    f"gate payload exceeds the node limit ({limits.max_gate_payload_nodes})"
                )
            for item in value:
                stack.append((item, (*path, "[]"), depth + 1))
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError("gate payload numbers must be finite")
        elif value is not None and type(value) not in {bool, int}:
            raise ValueError("gate payload contains an unsupported value")
        elif type(value) is int and value.bit_length() > 256:
            raise ValueError("gate payload integers must not exceed 256 bits")
        if total_chars > limits.max_gate_payload_chars:
            raise ValueError(
                "gate payload exceeds the deployment limit "
                f"({limits.max_gate_payload_chars} characters)"
            )


def _safe_gate_path(path: tuple[str, ...]) -> str:
    """Describe gate fields without reflecting attacker-controlled mapping keys."""

    return (
        ".".join(
            part if part in _SAFE_GATE_PATH_FIELDS or part == "[]" else "field" for part in path
        )
        or "value"
    )


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
        if type(value) is not str:
            raise ValueError(f"{name} must be a string")
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

    if type(system) is not str or type(user) is not str:
        raise ValueError("rendered model input must contain strings")
    if "\x00" in system or "\x00" in user:
        raise ValueError("rendered model input must not contain U+0000")
    limit = (settings or get_settings()).runs.max_model_input_chars
    if len(system) + len(user) > limit:
        raise ValueError(f"rendered model input exceeds the deployment limit ({limit} characters)")
    if contains_credential_material(
        {"system": system, "user": user},
        max_nodes=4,
        max_string_chars=limit,
        # The scanner accounts for mapping keys as well as values. The model
        # budget applies to rendered values only, so reserve fixed key overhead.
        max_total_chars=limit + len("system") + len("user"),
    ):
        raise ValueError("rendered model input must not contain credential material")


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
