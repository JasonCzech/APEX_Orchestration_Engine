"""Explicit public projections for durable pipeline checkpoints and gates.

LangGraph state is operational storage, not an API response schema.  This module
keeps viewer responses limited to the fields used by the dashboard, validates
legacy values against fixed budgets, and removes connection affinity, provider
options, tool arguments, artifact keys, and other server-only state.
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit

from apex.domain.diagnostics import bounded_diagnostic, contains_credential_material
from apex.domain.input_limits import validate_json_object
from apex.domain.pipeline import PHASE_ORDER, PhaseStatus
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.services.public_projection import (
    public_engine_handle_summary,
    public_test_result_summary,
)

JsonDict = dict[str, Any]

_PHASES = frozenset(phase.value for phase in PHASE_ORDER)
_PHASE_STATUSES = frozenset(status.value for status in PhaseStatus)
_PROMPT_ORIGINS = frozenset({"catalog", "assistant_pin", "run_override", "gate_edit"})
_PROMPT_ACTIONS = frozenset({"approve", "modify", "skip_phase", "abort"})
_PHASE_ACTIONS = frozenset({"approve", "revise", "discuss", "abort"})
_ENGINE_RETRY_ACTIONS = frozenset({"retry"})
_ENGINE_RETRY_KINDS = frozenset(
    {
        "engine_provision_retry",
        "engine_cleanup_retry",
        "engine_collection_retry",
        "engine_collection_settle_retry",
    }
)
_PUBLIC_LOAD_TEST_FIELDS = frozenset({"title", "vusers", "ramp_s", "duration_s", "slas"})
MAX_PUBLIC_PIPELINE_STATE_BYTES = 512_000
MAX_PUBLIC_PIPELINE_STATE_NODES = 10_000
MAX_PUBLIC_GATE_BYTES = 256_000
MAX_PUBLIC_GATE_NODES = 4_096


def _text(value: Any, max_chars: int, *, allow_empty: bool = True) -> str | None:
    if type(value) is not str or len(value) > max_chars or (not allow_empty and not value):
        return None
    return bounded_diagnostic(value, max_chars=max(1, len(value)))


def public_text(value: Any, max_chars: int, *, allow_empty: bool = True) -> str | None:
    """Public wrapper for bounded legacy text used by summary projections."""

    return _text(value, max_chars, allow_empty=allow_empty)


def _finite_number(value: Any, *, minimum: float, maximum: float) -> int | float | None:
    if type(value) not in {int, float} or (type(value) is int and value.bit_length() > 256):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric < minimum or numeric > maximum:
        return None
    return value


def _public_source(value: Any) -> JsonDict | None:
    if type(value) is not dict:
        return None
    origin = value.get("origin")
    if type(origin) is not str or origin not in _PROMPT_ORIGINS:
        return None
    result: JsonDict = {"origin": origin}
    ref = _text(value.get("ref"), 2_048)
    editor = _text(value.get("editor"), 255)
    if ref is not None:
        result["ref"] = ref
    if editor is not None:
        result["editor"] = editor
    return result


def _safe_artifact_uri(value: Any) -> str | None:
    uri = _text(value, 4_096, allow_empty=False)
    if uri is None or "\\" in uri or any(ord(char) < 0x20 for char in uri):
        return None
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"memory", "s3", "apex-artifact"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return uri


def _public_artifact(value: Any) -> JsonDict | None:
    if type(value) is not dict:
        return None
    artifact_id = _text(value.get("id"), 128, allow_empty=False)
    if artifact_id is None:
        return None
    result: JsonDict = {"id": artifact_id}
    for field, maximum in (
        ("kind", 64),
        ("name", 512),
        ("media_type", 255),
        ("summary", 4_000),
        ("created_at", 64),
    ):
        item = _text(value.get(field), maximum)
        if item is not None:
            result[field] = item
    uri = _safe_artifact_uri(value.get("uri"))
    if uri is not None:
        result["uri"] = uri
    return result


def _public_context_preview(value: Any) -> JsonDict | None:
    if type(value) is not dict:
        return None
    packet_id = _text(value.get("id"), 128, allow_empty=False)
    if packet_id is None:
        return None
    result: JsonDict = {"id": packet_id}
    for field, maximum in (("source", 128), ("title", 500), ("summary", 4_000)):
        item = _text(value.get(field), maximum)
        if item is not None:
            result[field] = item
    return result


def _public_dialogue(value: Any) -> JsonDict | None:
    if type(value) is not dict:
        return None
    entry_id = _text(value.get("id"), 256, allow_empty=False)
    phase = value.get("phase")
    role = value.get("role")
    content = _text(value.get("content"), 20_000)
    if (
        entry_id is None
        or type(phase) is not str
        or phase not in _PHASES
        or type(role) is not str
        or role not in {"operator", "agent"}
        or content is None
    ):
        return None
    result: JsonDict = {"id": entry_id, "phase": phase, "role": role, "content": content}
    attempt = _finite_number(value.get("attempt"), minimum=1, maximum=1_000_000)
    at = _text(value.get("at"), 64)
    if type(attempt) is int:
        result["attempt"] = attempt
    if at is not None:
        result["at"] = at
    return result


def _public_approval(value: Any) -> JsonDict | None:
    if type(value) is not dict:
        return None
    approval_id = _text(value.get("id"), 256, allow_empty=False)
    gate = value.get("gate")
    action = _text(value.get("action"), 32, allow_empty=False)
    if (
        approval_id is None
        or type(gate) is not str
        or gate not in {"prompt_review", "phase_review"}
        or action is None
    ):
        return None
    result: JsonDict = {"id": approval_id, "gate": gate, "action": action}
    actor = _text(value.get("actor"), 255)
    at = _text(value.get("at"), 64)
    if actor is not None:
        result["actor"] = actor
    if at is not None:
        result["at"] = at
    return result


def _public_tool_call(value: Any) -> JsonDict | None:
    if type(value) is not dict:
        return None
    call_id = _text(value.get("id"), 256, allow_empty=False)
    tool = _text(value.get("tool"), 256, allow_empty=False)
    status = value.get("status")
    if call_id is None or tool is None or type(status) is not str or status not in {"ok", "error"}:
        return None
    # args_preview and error are deliberately omitted: the dashboard displays
    # only operation metadata, while those fields may carry provider input.
    result: JsonDict = {"id": call_id, "tool": tool, "status": status}
    duration = _finite_number(value.get("duration_ms"), minimum=0, maximum=86_400_000)
    at = _text(value.get("at"), 64)
    if type(duration) is int:
        result["duration_ms"] = duration
    if at is not None:
        result["at"] = at
    return result


def _public_prompt(value: Any) -> JsonDict | None:
    if type(value) is not dict:
        return None
    result: JsonDict = {}
    for field in ("system", "user", "application"):
        item = _text(value.get(field), 100_000)
        if item is not None:
            result[field] = item
    source = _public_source(value.get("source"))
    if source is not None:
        result["source"] = source
    return result or None


def _public_phase_result(phase: str, value: Any) -> JsonDict | None:
    if type(phase) is not str or phase not in _PHASES or type(value) is not dict:
        return None
    result: JsonDict = {"phase": phase}
    status = value.get("status")
    if type(status) is str and status in _PHASE_STATUSES:
        result["status"] = status
    attempt = _finite_number(value.get("attempt"), minimum=1, maximum=1_000_000)
    if type(attempt) is int:
        result["attempt"] = attempt
    for field, maximum in (
        ("started_at", 64),
        ("ended_at", 64),
        ("summary", 20_000),
        ("reasoning_digest", 20_000),
    ):
        item = _text(value.get(field), maximum)
        if item is not None:
            result[field] = item
    duration = _finite_number(value.get("duration_s"), minimum=0, maximum=86_400_000)
    if duration is not None:
        result["duration_s"] = duration
    transcript = _public_artifact(value.get("transcript_ref"))
    if transcript is not None:
        result["transcript_ref"] = transcript

    artifact_ids = value.get("artifact_ids")
    if type(artifact_ids) is list and len(artifact_ids) <= 256:
        projected_ids = [
            item for raw in artifact_ids if (item := _text(raw, 128, allow_empty=False)) is not None
        ]
        if len(projected_ids) == len(artifact_ids):
            result["artifact_ids"] = projected_ids
    for field in ("warnings", "errors"):
        items = value.get(field)
        if type(items) is list and len(items) <= 128:
            projected = [item for raw in items if (item := _text(raw, 4_096)) is not None]
            if len(projected) == len(items):
                result[field] = projected

    approvals = value.get("approvals")
    if type(approvals) is list and len(approvals) <= 100:
        projected_approvals = [
            item for raw in approvals if (item := _public_approval(raw)) is not None
        ]
        if len(projected_approvals) == len(approvals):
            result["approvals"] = projected_approvals
    tool_calls = value.get("tool_calls")
    if type(tool_calls) is list and len(tool_calls) <= 80:
        projected_calls = [
            item for raw in tool_calls if (item := _public_tool_call(raw)) is not None
        ]
        if len(projected_calls) == len(tool_calls):
            result["tool_calls"] = projected_calls

    source = _public_source(value.get("resolved_prompt_source"))
    if source is not None:
        result["resolved_prompt_source"] = source
    prompt = _public_prompt(value.get("resolved_prompt"))
    if prompt is not None:
        result["resolved_prompt"] = prompt
    test_summary = public_test_result_summary(value.get("test_summary"))
    if test_summary is not None:
        result["test_summary"] = test_summary
    engine = _text(value.get("engine"), 64, allow_empty=False)
    if engine is not None:
        result["engine"] = engine
    return result


def _public_prompt_review(value: Any) -> JsonDict | None:
    if type(value) is not dict:
        return None
    system = _text(value.get("system"), 100_000)
    phase_prompt = _text(value.get("phase_prompt"), 100_000)
    additional = _text(value.get("additional_context"), 50_000)
    source = _public_source(value.get("source"))
    updated_at = _text(value.get("updated_at"), 64)
    updated_by = _text(value.get("updated_by"), 255)
    if any(
        item is None for item in (system, phase_prompt, additional, source, updated_at, updated_by)
    ):
        return None
    result: JsonDict = {
        "system": system,
        "phase_prompt": phase_prompt,
        "additional_context": additional,
        "source": source,
        "updated_at": updated_at,
        "updated_by": updated_by,
    }
    application = _text(value.get("application"), 100_000)
    result["application"] = application
    return result


def _public_application_review(value: Any) -> JsonDict | None:
    if type(value) is not dict:
        return None
    source = _public_source(value.get("source"))
    updated_at = _text(value.get("updated_at"), 64)
    updated_by = _text(value.get("updated_by"), 255)
    if source is None or updated_at is None or updated_by is None:
        return None
    return {
        "content": _text(value.get("content"), 100_000),
        "source": source,
        "updated_at": updated_at,
        "updated_by": updated_by,
    }


def public_prompt_review(value: Any) -> JsonDict | None:
    """Project one prompt-review response through the public review schema."""

    return _public_prompt_review(value)


def _public_run_config(value: Any) -> JsonDict | None:
    if type(value) is not dict:
        return None
    # A loopback/legacy checkpoint can be much larger or deeper than the fields
    # this display projection retains. Bound the complete raw tree before
    # Pydantic recursively validates nonpublic prompt bodies, connection maps,
    # or provider options.
    try:
        validate_json_object(
            value,
            label="public pipeline run configuration",
            max_bytes=5_000_000,
            max_nodes=20_000,
            max_depth=24,
        )
    except (OverflowError, RecursionError, TypeError, ValueError):
        return None
    try:
        config = PipelineConfigurable.model_validate(value)
    except (TypeError, ValueError):
        if not contains_credential_material(value):
            return None
        # Legacy snapshots may predate durable credential rejection. Preserve
        # the dashboard's display-only contract by validating only independently
        # safe public fields; omit any field containing credential material.
        public_candidates: JsonDict = {}
        legacy_load_test = value.get("load_test")
        if type(legacy_load_test) is dict:
            safe_load_test: JsonDict = {}
            for field in _PUBLIC_LOAD_TEST_FIELDS - {"slas"}:
                if field not in legacy_load_test:
                    continue
                raw = legacy_load_test[field]
                if field == "title" and type(raw) is str:
                    safe_load_test[field] = bounded_diagnostic(
                        raw,
                        max_chars=max(1, min(len(raw), 1_000)),
                    )
                elif not contains_credential_material({field: raw}):
                    safe_load_test[field] = raw
            slas = legacy_load_test.get("slas")
            if type(slas) is dict:
                safe_slas = {
                    name: threshold
                    for name, threshold in slas.items()
                    if not contains_credential_material({name: threshold})
                }
                if safe_slas:
                    safe_load_test["slas"] = safe_slas
            if safe_load_test:
                public_candidates["load_test"] = safe_load_test
        for field in {
            "agent_backend",
            "engine",
            "gates",
            "limits",
            "model_by_phase",
            "phases",
            "start_phase",
            "stop_after",
        }:
            if field not in value:
                continue
            raw = value[field]
            if not contains_credential_material({field: raw}):
                public_candidates[field] = raw
            elif field == "engine" and type(raw) is str:
                # Keep a useful legacy display label without retaining the
                # credential portion. Already-redacted diagnostics no longer
                # classify as credential material during model validation.
                public_candidates[field] = bounded_diagnostic(
                    raw,
                    max_chars=max(1, min(len(raw), 64)),
                )
        try:
            config = PipelineConfigurable.model_validate(public_candidates)
        except (TypeError, ValueError):
            return None
    # This is a display projection, never a round-trippable rerun contract.
    # Scope ids, assistant/environment ids, provider targets, connection affinity,
    # prompt bodies, and pre-execution context stay server-side. Reruns recover the
    # complete validated snapshot through the trusted facade instead.
    result: JsonDict = {
        "phases": [phase.value for phase in config.phases] if config.phases else None,
        "start_phase": config.start_phase.value if config.start_phase else None,
        "stop_after": config.stop_after.value if config.stop_after else None,
        "gates": {
            phase.value: policy.model_dump(mode="json") for phase, policy in config.gates.items()
        },
        "limits": config.limits.model_dump(mode="json"),
        "agent_backend": config.agent_backend,
    }
    engine = _text(config.engine, 64, allow_empty=False)
    if engine is not None:
        result["engine"] = engine
    models = {
        phase.value: projected
        for phase, model in config.model_by_phase.items()
        if (projected := _text(model, 200, allow_empty=False)) is not None
    }
    if models:
        result["model_by_phase"] = models

    load_test: JsonDict = {}
    for field in _PUBLIC_LOAD_TEST_FIELDS - {"slas"}:
        item = config.load_test.get(field)
        if field == "title":
            projected = _text(item, 1_000, allow_empty=False)
            if projected is not None:
                load_test[field] = projected
        elif _finite_number(item, minimum=0, maximum=1_000_000_000_000) is not None:
            load_test[field] = item
    slas = config.load_test.get("slas")
    if type(slas) is dict:
        public_slas = {
            name: threshold
            for name, threshold in slas.items()
            if type(name) is str
            and _text(name, 64, allow_empty=False) == name
            and _finite_number(threshold, minimum=0, maximum=1_000_000_000_000) is not None
        }
        if public_slas:
            load_test["slas"] = public_slas
    if load_test:
        result["load_test"] = load_test
    return result


def _within_projection_budget(value: JsonDict, *, max_bytes: int, max_nodes: int) -> bool:
    try:
        validate_json_object(
            value,
            label="public pipeline projection",
            max_bytes=max_bytes,
            max_nodes=max_nodes,
            max_depth=24,
        )
    except (OverflowError, RecursionError, TypeError, ValueError):
        return False
    return True


def _compact_public_state(value: JsonDict) -> JsonDict:
    """Keep navigation/status data when a legacy checkpoint exceeds the response budget."""

    result = {
        key: value[key]
        for key in ("title", "phases_plan", "current_phase", "run_aborted", "engine_handle")
        if key in value
    }
    phase_results = value.get("phase_results")
    if type(phase_results) is dict:
        result["phase_results"] = {
            phase: {
                key: entry[key]
                for key in ("phase", "status", "attempt", "started_at", "ended_at", "duration_s")
                if key in entry
            }
            for phase, entry in phase_results.items()
            if type(entry) is dict
        }
    return result


def public_pipeline_state(value: Any) -> JsonDict:
    """Project one checkpoint into the bounded state consumed by the dashboard."""

    if type(value) is not dict:
        return {}
    result: JsonDict = {}
    title = _text(value.get("title"), 500)
    if title is not None:
        result["title"] = title
    phases = value.get("phases_plan")
    if (
        type(phases) is list
        and len(phases) <= len(_PHASES)
        and all(type(item) is str for item in phases)
        and len(set(phases)) == len(phases)
        and all(item in _PHASES for item in phases)
    ):
        result["phases_plan"] = list(phases)
    current_phase = value.get("current_phase")
    if current_phase is None or (type(current_phase) is str and current_phase in _PHASES):
        result["current_phase"] = current_phase
    if type(value.get("run_aborted")) is bool:
        result["run_aborted"] = value["run_aborted"]

    run_config = _public_run_config(value.get("run_config"))
    if run_config is not None:
        result["run_config"] = run_config

    phase_results = value.get("phase_results")
    if type(phase_results) is dict and len(phase_results) <= len(_PHASES):
        projected_results = {
            phase: projected
            for phase, entry in phase_results.items()
            if type(phase) is str and (projected := _public_phase_result(phase, entry)) is not None
        }
        result["phase_results"] = projected_results

    prompt_reviews = value.get("prompt_reviews")
    if type(prompt_reviews) is dict and len(prompt_reviews) <= len(_PHASES):
        result["prompt_reviews"] = {
            phase: projected
            for phase, review in prompt_reviews.items()
            if type(phase) is str
            and phase in _PHASES
            and (projected := _public_prompt_review(review)) is not None
        }
    application_reviews = value.get("application_reviews")
    if type(application_reviews) is dict and len(application_reviews) <= 32:
        result["application_reviews"] = {
            app_id: projected
            for app_id, review in application_reviews.items()
            if _text(app_id, 255, allow_empty=False) == app_id
            and (projected := _public_application_review(review)) is not None
        }

    artifacts = value.get("artifacts")
    if type(artifacts) is list and len(artifacts) <= 512:
        projected_artifacts = [
            item for raw in artifacts if (item := _public_artifact(raw)) is not None
        ]
        if len(projected_artifacts) == len(artifacts):
            result["artifacts"] = projected_artifacts
    dialogue = value.get("dialogue")
    if type(dialogue) is list and len(dialogue) <= 100:
        projected_dialogue = [
            item for raw in dialogue if (item := _public_dialogue(raw)) is not None
        ]
        if len(projected_dialogue) == len(dialogue):
            result["dialogue"] = projected_dialogue
    packets = value.get("context_packets")
    if type(packets) is list and len(packets) <= 64:
        projected_packets = [
            item for raw in packets if (item := _public_context_preview(raw)) is not None
        ]
        if len(projected_packets) == len(packets):
            result["context_packets"] = projected_packets

    handle = public_engine_handle_summary(value.get("engine_handle"))
    if handle is not None:
        result["engine_handle"] = handle
    if not _within_projection_budget(
        result,
        max_bytes=MAX_PUBLIC_PIPELINE_STATE_BYTES,
        max_nodes=MAX_PUBLIC_PIPELINE_STATE_NODES,
    ):
        return _compact_public_state(result)
    return result


def _public_actions(value: Any, allowed: frozenset[str]) -> list[str]:
    if type(value) is not list or len(value) > len(allowed):
        return []
    actions = [item for item in value if type(item) is str and item in allowed]
    return actions if len(actions) == len(value) and len(set(actions)) == len(actions) else []


def _valid_public_text(value: Any, max_chars: int, *, allow_empty: bool = True) -> bool:
    """Require a decision-visible string to survive projection byte-for-byte."""

    return (
        type(value) is str
        and (allow_empty or bool(value))
        and _text(value, max_chars, allow_empty=allow_empty) == value
    )


def _valid_nullable_text(value: Any, max_chars: int) -> bool:
    return value is None or _valid_public_text(value, max_chars)


def _meaningful_text(value: Any, max_chars: int) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and _valid_public_text(value, max_chars, allow_empty=False)
    )


def _valid_review_payload(kind: str, phase: str, value: Any) -> bool:
    """Require the complete human-review contract before exposing actions."""

    if type(value) is not dict or not _within_projection_budget(
        value,
        max_bytes=MAX_PUBLIC_GATE_BYTES,
        max_nodes=MAX_PUBLIC_GATE_NODES,
    ):
        return False
    schema_version = value.get("schema_version")
    payload_kind = value.get("kind")
    payload_phase = value.get("phase")
    actions = value.get("actions")
    if (
        type(schema_version) is not int
        or schema_version != 1
        or type(payload_kind) is not str
        or payload_kind != kind
        or type(payload_phase) is not str
        or payload_phase != phase
        or type(actions) is not list
        or any(type(action) is not str for action in actions)
        or (kind == "prompt_review" and actions != ["approve", "modify", "skip_phase", "abort"])
        or (kind == "phase_review" and actions != ["approve", "revise", "discuss", "abort"])
    ):
        return False
    if "error" in value and _text(value.get("error"), 4_096) is None:
        return False

    if kind == "prompt_review":
        prompt = value.get("prompt")
        if type(prompt) is not dict or any(
            field not in prompt for field in ("system", "user", "application", "source")
        ):
            return False
        if any(not _valid_public_text(prompt.get(field), 100_000) for field in ("system", "user")):
            return False
        if not _valid_nullable_text(prompt.get("application"), 100_000):
            return False
        source = prompt.get("source")
        if type(source) is not dict or any(field not in source for field in ("origin", "ref")):
            return False
        origin = source.get("origin")
        if type(origin) is not str or origin not in _PROMPT_ORIGINS:
            return False
        if not _valid_nullable_text(source.get("ref"), 2_048):
            return False
        if not _valid_public_text(value.get("additional_context"), 50_000):
            return False
        packets = value.get("context_packets")
        if type(packets) is not list or len(packets) > 64:
            return False
        for packet in packets:
            if type(packet) is not dict or any(
                field not in packet for field in ("id", "source", "title", "summary")
            ):
                return False
            if any(
                not _valid_nullable_text(packet.get(field), maximum)
                for field, maximum in (
                    ("id", 128),
                    ("source", 128),
                    ("title", 500),
                    ("summary", 4_000),
                )
            ):
                return False
        tools = value.get("tools")
        return (
            type(tools) is list
            and len(tools) <= 64
            and all(_valid_public_text(tool, 256) for tool in tools)
            and type(value.get("editable")) is bool
            and value.get("editable") is True
        )

    summary = value.get("summary")
    preview = value.get("result_preview")
    artifacts = value.get("artifacts")
    warnings = value.get("warnings")
    tail = value.get("dialogue_tail")
    if (
        "summary" not in value
        or not _valid_nullable_text(summary, 20_000)
        or type(preview) is not dict
        or any(field not in preview for field in ("summary", "reasoning_digest"))
        or any(
            not _valid_nullable_text(preview.get(field), 20_000)
            for field in ("summary", "reasoning_digest")
        )
        or type(artifacts) is not list
        or len(artifacts) > 256
        or type(warnings) is not list
        or len(warnings) > 128
        or any(not _valid_public_text(warning, 4_096) for warning in warnings)
        or type(tail) is not list
        or len(tail) > 3
    ):
        return False
    for artifact in artifacts:
        if type(artifact) is not dict or any(
            field not in artifact for field in ("id", "kind", "name")
        ):
            return False
        if any(
            not _valid_nullable_text(artifact.get(field), maximum)
            for field, maximum in (("id", 128), ("kind", 64), ("name", 512))
        ):
            return False
    for entry in tail:
        projected = _public_dialogue(entry)
        if (
            projected is None
            or projected.get("phase") != phase
            or projected.get("id") != entry.get("id")
            or projected.get("content") != entry.get("content")
            or ("at" in entry and projected.get("at") != entry.get("at"))
            or ("attempt" in entry and projected.get("attempt") != entry.get("attempt"))
        ):
            return False
    return (
        _meaningful_text(summary, 20_000)
        or any(
            _meaningful_text(preview.get(field), 20_000)
            for field in ("summary", "reasoning_digest")
        )
        or any(
            all(
                _meaningful_text(artifact.get(field), maximum)
                for field, maximum in (("id", 128), ("kind", 64), ("name", 512))
            )
            for artifact in artifacts
        )
        or any(_meaningful_text(warning, 4_096) for warning in warnings)
        or any(_meaningful_text(entry.get("content"), 20_000) for entry in tail)
    )


def _valid_engine_retry_payload(kind: str, phase: str, value: Any) -> bool:
    """Require the complete recovery contract; never synthesize resumability."""

    if type(value) is not dict:
        return False
    schema_version = value.get("schema_version")
    payload_kind = value.get("kind")
    payload_phase = value.get("phase")
    actions = value.get("actions")
    attempt = value.get("attempt")
    thread_id = value.get("thread_id")
    message = value.get("message")
    error = value.get("error")
    return (
        type(schema_version) is int
        and schema_version == 1
        and type(payload_kind) is str
        and payload_kind == kind
        and type(payload_phase) is str
        and payload_phase == phase
        and phase == "execution"
        and type(actions) is list
        and len(actions) == 1
        and type(actions[0]) is str
        and actions[0] == "retry"
        and type(attempt) is int
        and 1 <= attempt <= 1_000_000
        and type(thread_id) is str
        and 1 <= len(thread_id) <= 255
        and thread_id == thread_id.strip()
        and "\x00" not in thread_id
        and not contains_credential_material(thread_id)
        and type(message) is str
        and len(message) <= 4_096
        and (error is None or (type(error) is str and len(error) <= 4_096))
    )


def _public_gate_payload(kind: str, phase: str, value: Any) -> JsonDict:
    payload = value if type(value) is dict else {}
    result: JsonDict = {"schema_version": 1, "kind": kind, "phase": phase}
    if kind == "prompt_review":
        result["actions"] = _public_actions(payload.get("actions"), _PROMPT_ACTIONS)
        prompt = payload["prompt"]
        source = prompt["source"]
        result["prompt"] = {
            "system": None if prompt["system"] is None else _text(prompt["system"], 100_000),
            "user": None if prompt["user"] is None else _text(prompt["user"], 100_000),
            "application": (
                None if prompt["application"] is None else _text(prompt["application"], 100_000)
            ),
            "source": {
                "origin": source["origin"],
                "ref": None if source["ref"] is None else _text(source["ref"], 2_048),
            },
        }
        result["additional_context"] = _text(payload["additional_context"], 50_000)
        result["context_packets"] = [
            {
                "id": None if raw["id"] is None else _text(raw["id"], 128),
                "source": None if raw["source"] is None else _text(raw["source"], 128),
                "title": None if raw["title"] is None else _text(raw["title"], 500),
                "summary": None if raw["summary"] is None else _text(raw["summary"], 4_000),
            }
            for raw in payload["context_packets"]
        ]
        result["tools"] = [_text(raw, 256) for raw in payload["tools"]]
        result["editable"] = payload["editable"]
    elif kind == "phase_review":
        result["actions"] = _public_actions(payload.get("actions"), _PHASE_ACTIONS)
        result["summary"] = (
            None if payload["summary"] is None else _text(payload["summary"], 20_000)
        )
        preview = payload["result_preview"]
        result["result_preview"] = {
            field: None if preview[field] is None else _text(preview[field], 20_000)
            for field in ("summary", "reasoning_digest")
        }
        result["artifacts"] = [
            {
                field: None if raw[field] is None else _text(raw[field], maximum)
                for field, maximum in (("id", 128), ("kind", 64), ("name", 512))
            }
            for raw in payload["artifacts"]
        ]
        result["warnings"] = [_text(raw, 4_096) for raw in payload["warnings"]]
        result["dialogue_tail"] = [_public_dialogue(raw) for raw in payload["dialogue_tail"]]
    else:
        # Runtime recovery gates carry no provider capability: expose only the
        # bounded retry contract needed to resume the exact interrupt id.
        result["actions"] = _public_actions(
            payload.get("actions"),
            _ENGINE_RETRY_ACTIONS,
        )
        attempt = payload.get("attempt")
        if type(attempt) is int and 1 <= attempt <= 1_000_000:
            result["attempt"] = attempt
        thread_id = _text(payload.get("thread_id"), 255, allow_empty=False)
        if thread_id is not None:
            result["thread_id"] = thread_id
        message = _text(payload.get("message"), 4_096)
        if message is not None:
            result["message"] = message
    error = _text(payload.get("error"), 4_096)
    if error is not None:
        result["error"] = error
    return result


def public_gate(value: Any, *, include_payload: bool) -> JsonDict | None:
    """Project one pending interrupt, dropping malformed/unknown gate state."""

    if type(value) is not dict:
        return None
    interrupt_id = _text(value.get("interrupt_id"), 256, allow_empty=False)
    kind = value.get("kind")
    phase = value.get("phase")
    if (
        interrupt_id is None
        or type(kind) is not str
        or kind not in {"prompt_review", "phase_review", *_ENGINE_RETRY_KINDS}
        or type(phase) is not str
        or phase not in _PHASES
        or (kind in _ENGINE_RETRY_KINDS and phase != "execution")
        or (
            kind in _ENGINE_RETRY_KINDS
            and not _valid_engine_retry_payload(kind, phase, value.get("payload"))
        )
        or (
            kind in {"prompt_review", "phase_review"}
            and not _valid_review_payload(kind, phase, value.get("payload"))
        )
    ):
        return None
    result: JsonDict = {"interrupt_id": interrupt_id, "kind": kind, "phase": phase}
    if include_payload:
        result["payload"] = _public_gate_payload(kind, phase, value.get("payload"))
        if not _within_projection_budget(
            result,
            max_bytes=MAX_PUBLIC_GATE_BYTES,
            max_nodes=MAX_PUBLIC_GATE_NODES,
        ):
            return None
    return result
