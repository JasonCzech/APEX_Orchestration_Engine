"""Per-phase subgraph factory: prepare -> prompt_gate -> agent -> output_gate -> finalize.

Each interrupt lives in its own node (resume re-executes the interrupting node from the
top, so gate nodes are side-effect-free before interrupt()). The awaiting_* status and
gate_opened event are committed by a separate "open_*" node so they are durable in the
checkpoint before the graph pauses. Agent bodies are deterministic M1 stubs (no LLM, no
ports); state writes use deterministic ids so node re-execution stays idempotent under
the append-unique reducers.

M3: the execution phase swaps the stub agent for the checkpointed engine spine
(engine_reserve -> engine_start -> engine_poll ⟲ -> engine_collect -> settle, see
apex.graphs.pipeline.execution_phase); the gates around it are unchanged. The
script_scenario stub additionally emits a LoadTestSpec ("load_test_spec" entry
key) that the execution phase consumes, and the reporting stub mentions the
execution phase's test_summary KPIs when present.
"""

import asyncio
import json
import math
from datetime import datetime
from types import GetSetDescriptorType, MemberDescriptorType
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from apex.adapters.registry import PortKind
from apex.domain.diagnostics import (
    bounded_diagnostic,
    contains_credential_material,
    is_credential_field,
)
from apex.domain.durable_evidence import sanitize_durable_object
from apex.domain.integrations import LoadTestSpec, TestResultSummary
from apex.domain.pipeline import (
    MAX_CONTEXT_REF_CHARS,
    MAX_CONTEXT_SUMMARY_CHARS,
    MAX_CONTEXT_TEXT_CHARS,
    MAX_CONTEXT_TITLE_CHARS,
    MAX_GATE_DECISION_TEXT_CHARS,
    MAX_GATE_TEXT_CHARS,
    MAX_TOOL_ARGS_PREVIEW_BYTES,
    MAX_TOOL_CALL_RECORDS,
    PHASE_PREREQUISITES,
    ArtifactRef,
    DialogueEntry,
    ExternalResults,
    Phase,
    PhaseStatus,
    ToolCallRecord,
    utcnow_iso,
)
from apex.graphs.pipeline.configurable import GateMode, PipelineConfigurable
from apex.graphs.pipeline.gates import (
    PHASE_REVIEW_ACTIONS,
    PROMPT_REVIEW_ACTIONS,
    build_phase_review_payload,
    build_prompt_review_payload,
    make_approval,
    parse_gate_decision,
    resolve_actor,
)
from apex.graphs.pipeline.state import (
    MAX_DURABLE_ARTIFACTS,
    MAX_DURABLE_DIALOGUE_ENTRIES,
    MAX_DURABLE_PHASE_DIAGNOSTICS,
    JsonDict,
    PipelineState,
)
from apex.ports.artifact_store import StoredArtifact, transcript_artifact_key
from apex.services import usage as usage_events
from apex.services.artifact_references import persist_artifact_with_intent
from apex.services.connections import ConnectionResolver, DbConnectionStore, close_adapter
from apex.services.pricing import MAX_TOKEN_COUNT, coerce_token_count
from apex.services.prompts import (
    prompt_review_from_resolved,
    resolve_phase_prompt_sync,
    resolved_from_prompt_review,
)
from apex.services.results_fetch import redact_fetch_url
from apex.services.run_validation import validate_context_packets, validate_rendered_model_input
from apex.settings import get_settings

EVENT_SCHEMA_VERSION = 1
logger = structlog.get_logger(__name__)

_PHASE_STATUS_VALUES = frozenset(status.value for status in PhaseStatus)

# Bounds re-interrupt loops inside a single gate node (modify re-reviews, bad actions).
MAX_GATE_LOOPS = 10
MAX_AGENT_RESPONSE_BLOCKS = 128
MAX_AGENT_TOOL_CALLS_PER_RESPONSE = 8
_TOOL_ARG_SECRET_KEY_MARKERS = frozenset(
    {
        "auth",
        "basicauth",
        "bearertoken",
        "clientsecret",
        "credential",
        "httpauth",
        "password",
        "privatekey",
        "secret",
        "secretkey",
        "signature",
        "token",
    }
)
_TOOL_ARG_SECRET_KEY_FRAGMENTS = (
    "apikey",
    "authorization",
    "credential",
    "password",
    "privatekey",
    "secret",
    "signature",
    "token",
)


def emit_event(event: JsonDict) -> None:
    """Send a custom stream event; no-op outside a runnable context (plain unit calls)."""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(event)


def stub_tool_names(phase: Phase) -> list[str]:
    return [f"{phase.value}.stub_lookup"]


def _prompt_variables(state: PipelineState) -> dict[str, str]:
    title = state.get("title")
    request = state.get("request")
    if title is None:
        title = "untitled run"
    if request is None:
        request = "(no request provided)"
    if type(title) is not str or len(title) > 500 or "\x00" in title:
        raise ValueError("checkpointed pipeline title is invalid")
    if type(request) is not str or len(request) > 20_000 or "\x00" in request:
        raise ValueError("checkpointed pipeline request is invalid")
    variables = {
        "title": title,
        "request": request,
    }
    if contains_credential_material(variables, max_nodes=4, max_total_chars=20_564):
        raise ValueError("checkpointed pipeline intent contains credential material")
    return variables


def _entry(state: PipelineState, phase: Phase) -> JsonDict:
    results = state.get("phase_results")
    if results is None:
        results = {}
    if type(results) is not dict:
        raise ValueError("checkpointed phase results are invalid")
    entry = results.get(phase.value)
    if entry is None:
        entry = {}
    if type(entry) is not dict:
        raise ValueError("checkpointed phase result is invalid")
    return entry


def _attempt(entry: JsonDict) -> int:
    attempt = entry.get("attempt")
    if attempt is None:
        return 1
    if type(attempt) is not int or not 1 <= attempt <= 1_000_000:
        raise ValueError("checkpointed phase attempt is invalid")
    return attempt


def _dialogue_matches_attempt(entry: JsonDict, phase: Phase, attempt: int) -> bool:
    if type(entry) is not dict:
        raise ValueError("checkpointed dialogue entry is invalid")
    entry_phase = entry.get("phase")
    if type(entry_phase) is not str:
        raise ValueError("checkpointed dialogue entry is invalid")
    if entry_phase != phase.value:
        return False
    raw_attempt = entry.get("attempt")
    if raw_attempt is None:
        raw_attempt = 1
    return type(raw_attempt) is int and raw_attempt == attempt


def _phase_update(phase: Phase, attempt: int, **fields: Any) -> JsonDict:
    # The merge reducer replaces wholesale when attempt differs, so every partial
    # update must carry the current attempt to merge instead of clobber.
    return {"phase_results": {phase.value: {"attempt": attempt, **fields}}}


def _thread_id(config: RunnableConfig | None) -> str:
    if config is not None and type(config) is not dict:
        raise ValueError("pipeline run configuration is invalid")
    configurable = (config or {}).get("configurable")
    if configurable is None:
        configurable = {}
    if type(configurable) is not dict:
        raise ValueError("pipeline run configuration is invalid")
    thread_id = configurable.get("thread_id")
    if (
        type(thread_id) is not str
        or not thread_id
        or thread_id != thread_id.strip()
        or len(thread_id) > 255
        or "\x00" in thread_id
        or contains_credential_material(thread_id)
    ):
        raise ValueError("pipeline runs require a durable thread_id")
    return thread_id


def _make_artifact_resolver() -> ConnectionResolver:
    return ConnectionResolver(store=DbConnectionStore())


async def _close_transcript_resources_definitively(
    store: Any | None,
    resolver: Any,
) -> None:
    """Release the checkout and its resolver cache despite repeated cancellation."""

    coroutines = [resolver.close()]
    if store is not None:
        coroutines.insert(0, close_adapter(store))
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    cancelled = False
    error: BaseException | None = None
    for task in tasks:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            task.result()
        except asyncio.CancelledError:
            cancelled = True
        except BaseException as exc:
            if error is None:
                error = exc
    if cancelled:
        raise asyncio.CancelledError from None
    if error is not None:
        raise error


def _transcript_bytes(
    state: PipelineState,
    phase: Phase,
    entry: JsonDict,
    *,
    attempt: int,
    status: str,
) -> bytes:
    """Render a deterministic, human-readable record from checkpointed state."""

    raw_dialogue = state.get("dialogue")
    if raw_dialogue is None:
        raw_dialogue = []
    if type(raw_dialogue) is not list:
        raise ValueError("checkpointed dialogue is invalid")
    dialogue = [item for item in raw_dialogue if _dialogue_matches_attempt(item, phase, attempt)]
    document = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "phase": phase.value,
        "attempt": attempt,
        "status": status,
        "summary": entry.get("summary"),
        "reasoning_digest": entry.get("reasoning_digest"),
        "resolved_prompt": entry.get("resolved_prompt"),
        "approvals": entry.get("approvals") if entry.get("approvals") is not None else [],
        "tool_calls": entry.get("tool_calls") if entry.get("tool_calls") is not None else [],
        "warnings": entry.get("warnings") if entry.get("warnings") is not None else [],
        "errors": entry.get("errors") if entry.get("errors") is not None else [],
        "dialogue": dialogue,
    }
    if contains_credential_material(
        document,
        max_nodes=10_000,
        max_total_chars=1_000_000,
    ):
        raise ValueError("checkpointed transcript contains unsafe material")
    document = sanitize_durable_object(document)
    return (
        "APEX phase transcript\n"
        + json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


async def _persist_transcript(
    state: PipelineState,
    phase: Phase,
    entry: JsonDict,
    config: RunnableConfig,
    *,
    attempt: int,
    status: str,
) -> tuple[StoredArtifact, str]:
    cfg = PipelineConfigurable.from_state_for_phase(state, config, phase)
    key = transcript_artifact_key(_thread_id(config), phase.value, attempt)
    resolver = _make_artifact_resolver()
    store: Any = None
    try:
        store, connection_id = await resolver.resolve_with_connection_id(
            PortKind.ARTIFACT_STORE,
            connection_id=cfg.connections.get(PortKind.ARTIFACT_STORE.value),
            project_id=cfg.project_id,
        )
        stored = await persist_artifact_with_intent(
            store,
            artifact_key=key,
            connection_id=connection_id,
            kind="transcript",
            thread_id=_thread_id(config),
            project_id=cfg.project_id,
            app_id=cfg.app_id,
            payload=_transcript_bytes(state, phase, entry, attempt=attempt, status=status),
            content_type="text/plain; charset=utf-8",
        )
        return stored, connection_id
    finally:
        try:
            await _close_transcript_resources_definitively(store, resolver)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # cleanup cannot overturn a committed object + index
            logger.warning(
                "pipeline.transcript_resolver_close_failed",
                phase=phase.value,
                thread_id=_thread_id(config),
                error=bounded_diagnostic(exc),
            )


def _prerequisite_error(state: PipelineState, phase: Phase) -> str | None:
    results = state.get("phase_results")
    if results is None:
        results = {}
    if type(results) is not dict:
        raise ValueError("checkpointed phase results are invalid")
    for prereq in PHASE_PREREQUISITES[phase]:
        prereq_entry = results.get(prereq.value)
        if prereq_entry is None:
            prereq_entry = {}
        if type(prereq_entry) is not dict:
            raise ValueError("checkpointed prerequisite phase result is invalid")
        status = prereq_entry.get("status")
        if status is not None and (type(status) is not str or status not in _PHASE_STATUS_VALUES):
            raise ValueError("checkpointed prerequisite phase status is invalid")
        if status != PhaseStatus.SUCCEEDED.value:
            rendered_status = status if status is not None else "missing"
            return (
                f"Cannot run phase '{phase.value}': prerequisite '{prereq.value}' "
                f"ended with status '{rendered_status}'."
            )
    return None


def _state_application_override(state: PipelineState, cfg: PipelineConfigurable) -> str | None:
    """Run-scoped, app-wide application prompt override content, if set.

    Stored once per run keyed by app_id, so an edit on any phase is shared by
    every phase of the run.
    """
    if not cfg.app_id:
        return None
    reviews = state.get("application_reviews")
    if reviews is None:
        reviews = {}
    if type(reviews) is not dict:
        raise ValueError("checkpointed application prompt overrides are invalid")
    override = reviews.get(cfg.app_id)
    if override is None:
        return None
    if type(override) is not dict:
        raise ValueError("checkpointed application prompt override is invalid")
    content = override.get("content")
    if content is not None:
        limit = get_settings().runs.max_prompt_part_chars
        if (
            type(content) is not str
            or len(content) > limit
            or "\x00" in content
            or contains_credential_material(content)
        ):
            raise ValueError("checkpointed application prompt override is invalid")
        return content
    return None


def _apply_application_override(
    state: PipelineState,
    cfg: PipelineConfigurable,
    review: JsonDict,
    resolved: JsonDict,
) -> tuple[JsonDict, JsonDict]:
    override = _state_application_override(state, cfg)
    if override is None:
        return _validated_prompt_review(review)
    return _validated_prompt_review({**review, "application": override})


def _validated_prompt_review(review: Any) -> tuple[JsonDict, JsonDict]:
    """Validate checkpointed prompt state before replaying or re-checkpointing it."""

    if type(review) is not dict or any(
        type(key) is not str
        or key
        not in {
            "system",
            "phase_prompt",
            "application",
            "additional_context",
            "source",
            "updated_at",
            "updated_by",
        }
        for key in review
    ):
        raise ValueError("checkpointed prompt review is invalid")
    limits = get_settings().runs
    text_limits = {
        "system": limits.max_prompt_part_chars,
        "phase_prompt": limits.max_prompt_part_chars,
        "additional_context": limits.max_gate_string_chars,
        "updated_at": 64,
        "updated_by": 255,
    }
    for field, limit in text_limits.items():
        value = review.get(field, "")
        if type(value) is not str or len(value) > limit or "\x00" in value:
            raise ValueError("checkpointed prompt review is invalid")
    application = review.get("application")
    if application is not None and (
        type(application) is not str
        or len(application) > limits.max_prompt_part_chars
        or "\x00" in application
    ):
        raise ValueError("checkpointed prompt review is invalid")
    source = review.get("source")
    if type(source) is not dict or any(
        type(key) is not str or key not in {"origin", "ref", "editor"} for key in source
    ):
        raise ValueError("checkpointed prompt review is invalid")
    origin = source.get("origin")
    if type(origin) is not str or origin not in {
        "catalog",
        "assistant_pin",
        "run_override",
        "gate_edit",
    }:
        raise ValueError("checkpointed prompt review is invalid")
    for field, limit in (("ref", 2_048), ("editor", 255)):
        value = source.get(field)
        if value is not None and (type(value) is not str or len(value) > limit or "\x00" in value):
            raise ValueError("checkpointed prompt review is invalid")
    if contains_credential_material(review):
        raise ValueError("checkpointed prompt review must not contain credential material")
    resolved: JsonDict | None = None
    try:
        resolved = dict(resolved_from_prompt_review(review))
    except Exception:
        pass
    if resolved is None:
        raise ValueError("checkpointed prompt review is invalid")
    return dict(review), resolved


def _application_review_update(
    cfg: PipelineConfigurable,
    application: Any,
    source: JsonDict,
    config: RunnableConfig,
) -> JsonDict:
    """State update recording the run-scoped, app-wide application override."""
    if not cfg.app_id or application is None:
        return {}
    if (
        type(application) is not str
        or len(application) > get_settings().runs.max_prompt_part_chars
        or "\x00" in application
        or contains_credential_material(application)
    ):
        raise ValueError("application prompt override is invalid")
    return {
        "application_reviews": {
            cfg.app_id: {
                "content": application,
                "source": source,
                "updated_at": utcnow_iso(),
                "updated_by": resolve_actor(config),
            }
        }
    }


def _review_source_for_phase(
    state: PipelineState,
    phase: Phase,
    cfg: PipelineConfigurable,
) -> tuple[JsonDict, JsonDict]:
    """Return (prompt-review draft, resolved prompt) for prepare/gates.

    New runs seed state["prompt_reviews"] in plan_resolver. Old checkpoints may
    not have it, so this keeps the previous resolved_prompt/locator behavior.
    The application prompt is layered from the run-scoped, app-wide override so
    that every phase resolves the same application text.
    """
    reviews = state.get("prompt_reviews")
    if reviews is None:
        reviews = {}
    if type(reviews) is not dict:
        raise ValueError("checkpointed prompt reviews are invalid")
    review = reviews.get(phase.value)
    if review is not None:
        return _apply_application_override(state, cfg, review, {})

    entry = _entry(state, phase)
    prompt = entry.get("resolved_prompt")
    if prompt is not None and type(prompt) is not dict:
        raise ValueError("checkpointed resolved prompt is invalid")
    if type(prompt) is dict and len(prompt) > 0:
        system = prompt.get("system")
        user = prompt.get("user")
        application = prompt.get("application")
        if system is None:
            system = ""
        if user is None:
            user = ""
        if type(system) is not str or type(user) is not str:
            raise ValueError("checkpointed resolved prompt is invalid")
        source = entry.get("resolved_prompt_source")
        if source is None:
            source = {"origin": "catalog"}
        elif type(source) is not dict:
            raise ValueError("checkpointed resolved prompt source is invalid")
        review = {
            "system": system,
            "phase_prompt": user,
            "application": application,
            "additional_context": "",
            "source": dict(source),
            "updated_at": utcnow_iso(),
            "updated_by": "system",
        }
        return _apply_application_override(
            state,
            cfg,
            review,
            {},
        )

    resolved = resolve_phase_prompt_sync(phase, cfg, variables=_prompt_variables(state))
    review = dict(prompt_review_from_resolved(resolved))
    return _apply_application_override(state, cfg, review, dict(resolved))


def _prompt_review_update(phase: Phase, review: JsonDict) -> JsonDict:
    return {"prompt_reviews": {phase.value: review}}


def _review_to_prompt(review: JsonDict) -> JsonDict:
    system = review.get("system")
    user = review.get("phase_prompt")
    return {
        "system": system if system is not None else "",
        "user": user if user is not None else "",
        "application": review.get("application"),
    }


def _resolved_prompt_from_review(review: JsonDict) -> JsonDict:
    _validated, resolved = _validated_prompt_review(review)
    return {key: resolved[key] for key in ("system", "user", "application")}


def _gate_edited_review(
    phase: Phase,
    review: JsonDict,
    prompt: JsonDict,
    source: JsonDict,
    config: RunnableConfig,
    note: Any = None,
) -> JsonDict:
    system = prompt.get("system")
    user = prompt.get("user")
    next_review = {
        **review,
        "system": system if system is not None else "",
        "phase_prompt": user if user is not None else "",
        "application": prompt.get("application"),
        "source": source,
        "updated_at": utcnow_iso(),
        "updated_by": resolve_actor(config),
    }
    if note is not None:
        if type(note) is not str:
            raise ValueError("prompt review note is invalid")
        next_review["additional_context"] = note
    elif "additional_context" not in next_review:
        next_review["additional_context"] = ""
    validated, _resolved = _validated_prompt_review(next_review)
    return validated


def _make_prepare(phase: Phase):
    def prepare(state: PipelineState, config: RunnableConfig) -> JsonDict:
        cfg = PipelineConfigurable.from_state_for_phase(state, config, phase)
        attempt = _attempt(_entry(state, phase))
        prereq_error = _prerequisite_error(state, phase)
        if prereq_error is not None:
            return _phase_update(
                phase,
                attempt,
                status=PhaseStatus.FAILED.value,
                started_at=utcnow_iso(),
                errors=[prereq_error],
            )
        review, resolved = _review_source_for_phase(state, phase, cfg)
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "phase_status",
                "phase": phase.value,
                "status": PhaseStatus.RUNNING.value,
                "attempt": attempt,
            }
        )
        update = _phase_update(
            phase,
            attempt,
            status=PhaseStatus.RUNNING.value,
            started_at=utcnow_iso(),
            resolved_prompt={
                "system": resolved["system"],
                "user": resolved["user"],
                "application": resolved["application"],
            },
            resolved_prompt_source=dict(resolved["source"]),
        )
        raw_reviews = state.get("prompt_reviews")
        if raw_reviews is None:
            raw_reviews = {}
        if type(raw_reviews) is not dict:
            raise ValueError("checkpointed prompt reviews are invalid")
        if phase.value not in raw_reviews:
            update.update(_prompt_review_update(phase, review))
        update["current_phase"] = phase.value
        return update

    return prepare


def _make_route_after_prepare(phase: Phase):
    def route_after_prepare(state: PipelineState) -> str:
        entry = _entry(state, phase)
        status = entry.get("status")
        if status is not None and (type(status) is not str or status not in _PHASE_STATUS_VALUES):
            raise ValueError("checkpointed phase status is invalid")
        errors = _checkpoint_diagnostics(entry, "errors")
        if status == PhaseStatus.FAILED.value and len(errors) > 0:
            return "finalize"
        return "open_prompt_gate"

    return route_after_prepare


def _make_open_prompt_gate(phase: Phase):
    def open_prompt_gate(state: PipelineState, config: RunnableConfig) -> JsonDict:
        cfg = PipelineConfigurable.from_state_for_phase(state, config, phase)
        if cfg.gate_policy(phase).prompt_review is not GateMode.GATED:
            return {}
        attempt = _attempt(_entry(state, phase))
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "gate_opened",
                "gate": "prompt_review",
                "phase": phase.value,
                "attempt": attempt,
            }
        )
        return _phase_update(phase, attempt, status=PhaseStatus.AWAITING_PROMPT_REVIEW.value)

    return open_prompt_gate


def _make_prompt_gate(phase: Phase):
    def prompt_gate(state: PipelineState, config: RunnableConfig) -> Command[str]:
        cfg = PipelineConfigurable.from_state_for_phase(state, config, phase)
        if cfg.gate_policy(phase).prompt_review is not GateMode.GATED:
            return Command(goto="agent")
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        review, _resolved = _review_source_for_phase(state, phase, cfg)
        prompt = _review_to_prompt(review)
        raw_source = review.get("source")
        if type(raw_source) is not dict:
            raise ValueError("checkpointed prompt review source is invalid")
        source = dict(raw_source)
        additional_context = review.get("additional_context") or ""
        original_application = review.get("application")
        raw_packets = state.get("context_packets")
        if raw_packets is None:
            raw_packets = []
        packets = validate_context_packets(raw_packets)
        if contains_credential_material(
            packets,
            max_nodes=2_000,
            max_total_chars=1_000_000,
        ):
            raise ValueError("checkpointed context packets contain unsafe material")
        settings = get_settings()
        if cfg.agent_backend == "anthropic" and settings.llm.anthropic_api_key:
            tools = [
                tool.name
                for tool in _build_agent_tools(
                    settings,
                    approved_urls=_approved_fetch_urls(state),
                )
            ]
        else:
            tools = stub_tool_names(phase)
        error: str | None = None
        for _ in range(MAX_GATE_LOOPS):
            payload = build_prompt_review_payload(
                phase,
                prompt,
                source,
                packets,
                tools,
                error,
                additional_context=additional_context,
            )
            decision = parse_gate_decision(interrupt(payload), PROMPT_REVIEW_ACTIONS)
            action = decision["action"]
            error = None
            if action == "approve":
                note_changed = False
                if decision.get("note") is not None:
                    next_context = decision.get("note") or ""
                    note_changed = next_context != additional_context
                    additional_context = next_context
                if note_changed and source.get("origin") != "gate_edit":
                    source = {
                        "origin": "gate_edit",
                        "ref": source.get("ref"),
                        "editor": resolve_actor(config),
                    }
                review = _gate_edited_review(
                    phase,
                    review,
                    prompt,
                    source,
                    config,
                    note=additional_context,
                )
                update = _phase_update(
                    phase,
                    attempt,
                    resolved_prompt=_resolved_prompt_from_review(review),
                    resolved_prompt_source=source,
                    approvals=[
                        make_approval(
                            phase,
                            attempt,
                            "prompt_review",
                            "approve",
                            config,
                            note=additional_context or None,
                        )
                    ],
                )
                update.update(_prompt_review_update(phase, review))
                if review.get("application") != original_application:
                    update.update(
                        _application_review_update(cfg, review.get("application"), source, config)
                    )
                return Command(goto="agent", update=update)
            if action == "modify":
                edit = decision.get("prompt")
                if type(edit) is dict:
                    prompt = {
                        "system": edit.get("system", prompt.get("system")),
                        "user": edit.get("user", prompt.get("user")),
                        "application": edit.get("application", prompt.get("application")),
                    }
                if decision.get("note") is not None:
                    additional_context = decision.get("note") or ""
                source = {
                    "origin": "gate_edit",
                    "ref": source.get("ref"),
                    "editor": resolve_actor(config),
                }
                review = _gate_edited_review(
                    phase,
                    review,
                    prompt,
                    source,
                    config,
                    note=additional_context,
                )
                continue  # re-interrupt for re-review of the edited prompt
            if action == "skip_phase":
                update = _phase_update(
                    phase,
                    attempt,
                    status=PhaseStatus.SKIPPED.value,
                    approvals=[
                        make_approval(phase, attempt, "prompt_review", "skip_phase", config)
                    ],
                )
                return Command(goto="finalize", update=update)
            if action == "abort":
                update = _phase_update(
                    phase,
                    attempt,
                    status=PhaseStatus.ABORTED.value,
                    approvals=[make_approval(phase, attempt, "prompt_review", "abort", config)],
                )
                update["run_aborted"] = True
                return Command(goto="finalize", update=update)
            error = decision.get("error") or "unsupported action"
        error = (
            f"prompt review loop cap ({MAX_GATE_LOOPS}) reached without an explicit "
            "terminal decision"
        )
        update = _phase_update(
            phase,
            attempt,
            status=PhaseStatus.FAILED.value,
            errors=[error],
        )
        update.update(_prompt_review_update(phase, review))
        if review.get("application") != original_application:
            update.update(
                _application_review_update(cfg, review.get("application"), source, config)
            )
        return Command(goto="finalize", update=update)

    return prompt_gate


def _script_scenario_load_test_spec(
    state: PipelineState, config: RunnableConfig, attempt: int
) -> JsonDict:
    """LoadTestSpec-shaped dict the execution phase consumes (entry key "load_test_spec").

    Deterministic for (thread, attempt) — including the idempotency key — so node
    re-execution emits an identical spec. engine_reserve re-derives the key from the
    execution phase's own attempt before any engine call; that copy is authoritative,
    this one seeds it for spec review/UX. Per-run sizing knobs ride the (unvalidated)
    "load_test" configurable dict so demos and tests can shrink vusers/duration.
    """
    thread_id = _thread_id(config)
    overrides = PipelineConfigurable.from_state_for_phase(
        state,
        config,
        Phase.SCRIPT_SCENARIO,
    ).load_test
    title = _prompt_variables(state)["title"]
    spec = LoadTestSpec(
        idempotency_key=f"{thread_id}-execution-a{attempt}",
        title=f"{title} load test",
        # No fake remote id: APEX Load generates its default inline workload,
        # while LoadRunner uses the selected connection's configured test_id.
        script_refs=[],
        vusers=overrides.get("vusers", 10),
        ramp_s=overrides.get("ramp_s", 1.0),
        duration_s=overrides.get("duration_s", 2.0),
        slas={"p95_ms": 500.0, "error_rate": 0.05},
        # Preview only. The execution reserve node re-resolves environment_id
        # after assistant config merging and checkpoints the approved target.
        target_environment=None,
    )
    return spec.model_dump(mode="json")


def _reporting_kpi_suffix(state: PipelineState) -> str:
    """Deterministic KPI mention when the execution phase produced a test_summary."""
    test_summary = _validated_checkpoint_summary(_entry(state, Phase.EXECUTION).get("test_summary"))
    if test_summary is None:
        return ""
    kpis = test_summary.kpis
    kpi_text = ", ".join(
        f"{key}={value:g}" if isinstance(value, int | float) else f"{key}={value}"
        for key, value in sorted(kpis.items())
    )
    verdict = "passed" if test_summary.passed else "failed"
    return (
        f" | execution {verdict} on engine {test_summary.engine} — "
        f"KPIs: {kpi_text or 'none reported'}"
    )


def _validated_checkpoint_summary(value: Any) -> TestResultSummary | None:
    """Fail closed on malformed/legacy provider summaries used in model output."""

    if type(value) is not dict or len(value) > 5:
        return None
    allowed = {"engine", "passed", "kpis", "sla_breaches", "notes"}
    if any(type(key) is not str or key not in allowed for key in value):
        return None
    engine = value.get("engine")
    passed = value.get("passed")
    kpis = value.get("kpis", {})
    breaches = value.get("sla_breaches", [])
    notes = value.get("notes")
    if (
        type(engine) is not str
        or not 1 <= len(engine) <= 64
        or type(passed) is not bool
        or type(kpis) is not dict
        or len(kpis) > 64
        or type(breaches) is not list
        or len(breaches) > 128
        or (notes is not None and (type(notes) is not str or len(notes) > 20_000))
    ):
        return None
    for name, metric in kpis.items():
        if type(name) is not str or not 1 <= len(name) <= 64 or "\x00" in name:
            return None
        if type(metric) is int:
            if abs(metric) > 1_000_000_000_000:
                return None
        elif type(metric) is float:
            if not math.isfinite(metric) or abs(metric) > 1_000_000_000_000:
                return None
        else:
            return None
    if any(
        type(breach) is not str or not 1 <= len(breach) <= 2_048 or "\x00" in breach
        for breach in breaches
    ):
        return None
    if contains_credential_material(value, max_nodes=256, max_total_chars=300_000):
        return None
    try:
        return TestResultSummary.model_validate(value)
    except Exception:
        return None


def _stub_agent_body(
    phase: Phase,
    state: PipelineState,
    config: RunnableConfig,
    *,
    warning: str | None = None,
) -> JsonDict:
    """Deterministic, offline agent stub (the default backend)."""
    entry = _entry(state, phase)
    attempt = _attempt(entry)
    revise_count = entry.get("revise_count")
    if revise_count is None:
        revise_count = 0
    if type(revise_count) is not int or not 0 <= revise_count <= 10:
        raise ValueError("checkpointed phase revision count is invalid")
    variables = _prompt_variables(state)
    title = variables["title"]
    request = variables["request"]
    summary = f"[{phase.value}] stub result for '{title}': {request}"
    instructions = entry.get("revise_instructions")
    if instructions is not None:
        if (
            type(instructions) is not str
            or len(instructions) > get_settings().runs.max_gate_string_chars
            or "\x00" in instructions
            or contains_credential_material(instructions)
        ):
            raise ValueError("checkpointed revision instructions are invalid")
        if instructions:
            summary += f" (revised per: {instructions})"
    if phase is Phase.REPORTING:
        summary += _reporting_kpi_suffix(state)
    summary = bounded_diagnostic(summary, max_chars=MAX_GATE_TEXT_CHARS)
    digest = (
        f"Deterministic stub reasoning for {phase.value} "
        f"(attempt {attempt}, revision {revise_count})."
    )
    tool_call = ToolCallRecord(
        id=f"{phase.value}-a{attempt}-r{revise_count}-stub-lookup",
        tool=f"{phase.value}.stub_lookup",
        args_preview={"title": title},
        status="ok",
        duration_ms=0,
    ).model_dump(mode="json")
    emit_event(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "type": "tool_call",
            "phase": phase.value,
            "id": tool_call["id"],
            "tool": tool_call["tool"],
            "status": tool_call["status"],
        }
    )
    extra: dict[str, Any] = {}
    if phase is Phase.SCRIPT_SCENARIO:
        extra["load_test_spec"] = _script_scenario_load_test_spec(state, config, attempt)
    if warning:
        extra["warnings"] = [warning]
    return _phase_update(
        phase,
        attempt,
        status=PhaseStatus.RUNNING.value,
        summary=summary,
        reasoning_digest=digest,
        tool_calls=[tool_call],
        **extra,
    )


def _message_text(message: Any) -> str:
    """Extract the text from an AIMessage whose content may be a string or, when
    thinking is enabled, a list of content blocks (thinking + text).

    Extraction is itself bounded. Do not first join or strip provider-controlled
    content: a non-conforming backend may ignore the requested token cap.
    """
    content = _raw_provider_field(message, "content", "")
    if type(content) is str:
        return bounded_diagnostic(content, max_chars=MAX_GATE_TEXT_CHARS).strip()
    if type(content) is not list:
        return ""
    parts: list[str] = []
    remaining = MAX_GATE_TEXT_CHARS
    for block in content[:MAX_AGENT_RESPONSE_BLOCKS]:
        raw_text: str | None = None
        if type(block) is str:
            raw_text = block
        elif type(block) is dict and block.get("type") == "text":
            text = block.get("text")
            if type(text) is str:
                raw_text = text
        if raw_text is None:
            continue
        separator = 1 if parts else 0
        available = remaining - separator
        if available <= 0:
            break
        rendered = bounded_diagnostic(raw_text, max_chars=available).strip()
        if not rendered:
            continue
        parts.append(rendered)
        remaining -= separator + len(rendered)
        if remaining <= 0:
            break
    return bounded_diagnostic(
        "\n".join(parts),
        max_chars=MAX_GATE_TEXT_CHARS,
    ).strip()


def _response_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Materialize only the bounded plain-list tool-call shape LangChain promises."""

    calls = _raw_provider_field(response, "tool_calls", None)
    if calls is None:
        calls = []
    if type(calls) is not list:
        raise ValueError("model tool_calls must be a list")
    if len(calls) > MAX_AGENT_TOOL_CALLS_PER_RESPONSE:
        raise ValueError("model returned too many tool calls in one response")
    if any(type(call) is not dict for call in calls):
        raise ValueError("model tool calls must be objects")
    return calls


def _canonical_tool_calls(
    calls: list[dict[str, Any]],
    *,
    phase: Phase,
    attempt: int,
    first_index: int,
    seen_ids: set[str],
) -> list[dict[str, Any]]:
    """Copy the tiny tool protocol envelope before it can re-enter model input."""

    canonical: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        tool_name = _tool_call_text(
            call.get("name"),
            label="tool name",
            default="unknown",
        )
        tool_id = _tool_call_text(
            call.get("id"),
            label="tool call id",
            default=f"{phase.value}-a{attempt}-tool{first_index + index}",
        )
        if tool_id in seen_ids:
            raise ValueError("model returned a duplicate tool call id")
        seen_ids.add(tool_id)
        canonical.append(
            {
                "name": tool_name,
                "args": _tool_call_args(call.get("args")),
                "id": tool_id,
                "type": "tool_call",
            }
        )
    return canonical


def _raw_provider_field(value: Any, field: str, default: Any = None) -> Any:
    """Read a response field without executing descriptors or ``__getattr__``."""

    if type(value) is dict:
        return value.get(field, default)
    try:
        mro = type.__getattribute__(type(value), "__mro__")
        if type(mro) is not tuple:
            return default
        descriptor: Any = None
        for base in mro:
            namespace = type.__getattribute__(base, "__dict__")
            if "__dict__" in namespace:
                descriptor = namespace["__dict__"]
                break
        if type(descriptor) not in {GetSetDescriptorType, MemberDescriptorType}:
            return default
        state = descriptor.__get__(value, type(value))
    except Exception:
        return default
    if type(state) is not dict:
        return default
    return state.get(field, default)


def _tool_call_text(value: Any, *, label: str, default: str) -> str:
    if value is None:
        return default
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or "\x00" in value
        or contains_credential_material(value)
    ):
        raise ValueError(f"model {label} must be a bounded credential-free string")
    return value


def _tool_call_args(value: Any) -> dict[str, Any]:
    """Validate model tool arguments without copying or serializing an oversized tree."""

    if value is None:
        return {}
    if type(value) is not dict:
        raise ValueError("model tool call args must be an object")
    remaining_chars = MAX_TOOL_ARGS_PREVIEW_BYTES // 4
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 64 or depth > 4:
            raise ValueError("model tool call args exceed the structural limit")
        if type(current) is dict:
            if len(current) > 16:
                raise ValueError("model tool call args contain too many fields")
            for key, nested in current.items():
                if type(key) is not str or not key or len(key) > 128 or "\x00" in key:
                    raise ValueError("model tool call arg names are invalid")
                remaining_chars -= len(key)
                stack.append((nested, depth + 1))
        elif type(current) is list:
            if len(current) > 16:
                raise ValueError("model tool call args contain too many items")
            stack.extend((nested, depth + 1) for nested in current)
        elif type(current) is str:
            if "\x00" in current:
                raise ValueError("model tool call args must not contain U+0000")
            remaining_chars -= len(current)
        elif current is None or type(current) in {bool, int}:
            if type(current) is int and current.bit_length() > 256:
                raise ValueError("model tool call args contain an oversized integer")
            remaining_chars -= 32
        elif type(current) is float and math.isfinite(current):
            remaining_chars -= 32
        else:
            raise ValueError("model tool call args contain unsupported values")
        if remaining_chars < 0:
            raise ValueError("model tool call args exceed the character limit")
    return value


def _compose_system(resolved: JsonDict) -> str:
    if type(resolved) is not dict:
        raise ValueError("checkpointed resolved prompt is invalid")
    raw_system = resolved.get("system")
    if raw_system is None:
        raw_system = ""
    if type(raw_system) is not str:
        raise ValueError("checkpointed resolved prompt is invalid")
    system = raw_system.strip()
    application = resolved.get("application")
    if application is not None and type(application) is not str:
        raise ValueError("checkpointed resolved prompt is invalid")
    app = application.strip() if application is not None else ""
    if app:
        system = f"{system}\n\n=== APPLICATION CONTEXT ===\n{app}" if system else app
    return system or "You are an APEX analysis agent."


def _context_packets_block(state: PipelineState) -> str:
    raw_packets = state.get("context_packets")
    if raw_packets is None:
        raw_packets = []
    packets = validate_context_packets(raw_packets)
    if contains_credential_material(
        packets,
        max_nodes=2_000,
        max_total_chars=1_000_000,
    ):
        raise ValueError("checkpointed context packets contain unsafe material")
    settings = get_settings()
    document_budget = settings.documents.max_context_chars_total
    runs = getattr(settings, "runs", None)
    run_budget = getattr(runs, "max_context_chars_total", document_budget)
    remaining = min(document_budget, run_budget)
    prefix = "=== CONTEXT / EVIDENCE ===\n"
    remaining -= len(prefix)
    if remaining <= 0:
        return prefix[: min(len(prefix), min(document_budget, run_budget))]
    sections: list[str] = []
    for packet in packets:
        if remaining <= 0:
            break
        if type(packet) is not dict:
            raise ValueError("checkpointed context packet is invalid")
        title = packet.get("title")
        if title is None or title == "":
            title = packet.get("source")
        if title is None or title == "":
            title = "evidence"
        if type(title) is not str or len(title) > MAX_CONTEXT_TITLE_CHARS or "\x00" in title:
            raise ValueError("checkpointed context packet is invalid")
        header = f"- {title}"
        summary = packet.get("summary")
        if summary is None:
            summary = ""
        if (
            type(summary) is not str
            or len(summary) > MAX_CONTEXT_SUMMARY_CHARS
            or "\x00" in summary
        ):
            raise ValueError("checkpointed context packet is invalid")
        if summary:
            header += f": {summary}"
        ref = packet.get("ref")
        if ref is None:
            ref = ""
        if type(ref) is not str or len(ref) > MAX_CONTEXT_REF_CHARS or "\x00" in ref:
            raise ValueError("checkpointed context packet is invalid")
        if ref:
            header += f" ({ref})"
        raw_text = packet.get("text")
        if raw_text is None:
            raw_text = ""
        if (
            type(raw_text) is not str
            or len(raw_text) > MAX_CONTEXT_TEXT_CHARS
            or "\x00" in raw_text
        ):
            raise ValueError("checkpointed context packet is invalid")
        text = raw_text.strip()
        separator_cost = 1 if sections else 0
        available = remaining - separator_cost
        if available <= 0:
            break
        section = header
        if text:
            framing = len(header) + len('\n"""\n') + len('\n"""')
            text_budget = max(0, available - framing)
            if len(text) > text_budget:
                marker = "…[truncated]"
                if text_budget >= len(marker):
                    text = text[: text_budget - len(marker)].rstrip() + marker
                else:
                    text = text[:text_budget]
            section = header + "\n" + _fence(text)
        if len(section) > available:
            marker = "…[truncated]"
            if available >= len(marker):
                section = section[: available - len(marker)].rstrip() + marker
            else:
                section = section[:available]
        sections.append(section)
        remaining -= separator_cost + len(section)
    if not sections:
        return ""
    return prefix + "\n".join(sections)


def _fence(text: str) -> str:
    """Delimit document text so the model can tell evidence content from instructions."""
    return f'"""\n{text}\n"""'


def _compose_user(state: PipelineState, phase: Phase, resolved: JsonDict, entry: JsonDict) -> str:
    blocks: list[str] = []
    if type(resolved) is not dict:
        raise ValueError("checkpointed resolved prompt is invalid")
    raw_user = resolved.get("user")
    if raw_user is None:
        raw_user = ""
    if type(raw_user) is not str:
        raise ValueError("checkpointed resolved prompt is invalid")
    user = raw_user.strip()
    if user:
        blocks.append(user)
    packets = _context_packets_block(state)
    if packets:
        blocks.append(packets)
    if phase is Phase.REPORTING:
        suffix = _reporting_kpi_suffix(state).strip(" |").strip()
        if suffix:
            blocks.append(f"Execution results: {suffix}")
    instructions = entry.get("revise_instructions")
    if instructions is not None:
        if (
            type(instructions) is not str
            or len(instructions) > get_settings().runs.max_gate_string_chars
            or "\x00" in instructions
            or contains_credential_material(instructions)
        ):
            raise ValueError("checkpointed revision instructions are invalid")
        if instructions:
            blocks.append(f"Operator revision instructions: {instructions}")
    return "\n\n".join(blocks) or "(no request provided)"


def _approved_fetch_urls(state: PipelineState) -> frozenset[str]:
    """Return only server-derived, credential-free URLs supplied by this run."""

    results = state.get("phase_results")
    if results is None:
        results = {}
    if type(results) is not dict:
        raise ValueError("checkpointed phase results are invalid")
    execution = results.get(Phase.EXECUTION.value)
    if execution is None:
        execution = {}
    if type(execution) is not dict:
        raise ValueError("checkpointed execution phase is invalid")
    if execution.get("external") is not True:
        return frozenset()
    source_uri = execution.get("source_uri")
    try:
        validated = ExternalResults.model_validate(
            {"source": "external-results", "uri": source_uri}
        )
    except ValueError:
        # Legacy checkpoints may contain URI shapes accepted before the durable
        # secret boundary was enforced. Never turn one into egress authority.
        return frozenset()
    return frozenset({validated.uri}) if validated.uri is not None else frozenset()


def _build_agent_tools(settings: Any, *, approved_urls: frozenset[str]) -> list[Any]:
    """The `fetch_results` tool when enabled with an allow-list, else no tools.

    Deny-by-default: returns [] unless explicitly enabled, so the agent binds no
    egress capability by default. Lazy-imports langchain so the stub path is clean.
    """
    if (
        not settings.llm.fetch_tool_enabled
        or not settings.llm.fetch_allowed_hosts
        or not approved_urls
    ):
        return []
    from langchain_core.tools import tool

    from apex.services.results_fetch import FetchError, fetch_results_text

    @tool
    def fetch_results(url: str) -> str:
        """Fetch the exact results URL supplied with this run so you can analyze it.

        Do not modify the URL path, query, or fragment.
        """
        try:
            if url not in approved_urls:
                raise FetchError("results URL was not supplied by this run")
            return fetch_results_text(
                url,
                allowed_hosts=settings.llm.fetch_allowed_hosts,
                allow_private=settings.llm.fetch_allow_private_hosts,
                require_https=_fetch_requires_https(settings),
                max_bytes=settings.llm.fetch_max_bytes,
                timeout_s=settings.llm.fetch_timeout_s,
            )
        except FetchError as exc:
            return bounded_diagnostic(f"error: {bounded_diagnostic(exc)}")

    return [fetch_results]


def _invoke_agent_tool(
    tool: Any,
    args: dict[str, Any],
    *,
    settings: Any,
    remaining_chars: int,
    approved_urls: frozenset[str],
) -> Any:
    """Invoke one tool without allowing its result to exceed the prompt budget."""

    if remaining_chars <= 256:
        return "error: model-input budget exhausted; tool call skipped"
    if getattr(tool, "name", None) != "fetch_results":
        return tool.invoke(args)

    from apex.services.results_fetch import FetchError, fetch_results_text

    try:
        requested_url = args.get("url")
        if not isinstance(requested_url, str) or requested_url not in approved_urls:
            raise FetchError("results URL was not supplied by this run")
        return fetch_results_text(
            requested_url,
            allowed_hosts=settings.llm.fetch_allowed_hosts,
            allow_private=settings.llm.fetch_allow_private_hosts,
            require_https=_fetch_requires_https(settings),
            max_bytes=min(settings.llm.fetch_max_bytes, remaining_chars),
            timeout_s=settings.llm.fetch_timeout_s,
        )
    except FetchError as exc:
        return bounded_diagnostic(f"error: {bounded_diagnostic(exc)}")


def _fetch_requires_https(settings: Any) -> bool:
    """Require TLS for every fetch performed by a locked deployment."""

    return bool(getattr(settings, "is_locked_down", False))


def _accumulate_usage(acc: dict[str, Any], usage: Any) -> None:
    """Sum untrusted usage metadata without exceptions or unbounded totals."""
    if type(usage) is not dict:
        return

    def saturated_sum(left: Any, right: Any) -> int:
        return min(coerce_token_count(left) + coerce_token_count(right), MAX_TOKEN_COUNT)

    for key in ("input_tokens", "output_tokens", "total_tokens"):
        acc[key] = saturated_sum(acc.get(key), usage.get(key))
    for detail_key in ("input_token_details", "output_token_details"):
        source = usage.get(detail_key)
        if type(source) is dict:
            existing = acc.get(detail_key)
            target: dict[str, int] = existing if type(existing) is dict else {}
            acc[detail_key] = target
            for index, (name, value) in enumerate(source.items()):
                if index >= 128:
                    break
                # Detail labels are provider-controlled. Keep the normalized
                # metadata JSON-bounded as well as bounding every count.
                if (
                    type(name) is not str
                    or not name
                    or len(name) > 128
                    or is_credential_field(name)
                    or contains_credential_material(name)
                ):
                    continue
                if name not in target and len(target) >= 64:
                    continue
                target[name] = saturated_sum(target.get(name), value)


def _llm_agent_body(
    phase: Phase,
    state: PipelineState,
    config: RunnableConfig,
    cfg: PipelineConfigurable,
    entry: JsonDict,
    attempt: int,
) -> JsonDict:
    """Anthropic-backed agent. Reads the resolved prompt + context packets, calls the
    model (optionally looping through the fetch tool), and records the model + usage so
    finalize writes a costed AgentEvent."""
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    settings = get_settings()
    model = cfg.model_by_phase.get(phase) or settings.llm.default_model
    # Always pass checkpointed prompt state through the exact replay validator.
    # A resumed legacy checkpoint can enter this node directly, without rerunning
    # prepare, so merely checking that ``resolved_prompt`` is a truthy mapping
    # would let credential-bearing prompt text reach the provider.
    _review, resolved = _review_source_for_phase(state, phase, cfg)
    system_text = _compose_system(resolved)
    user_text = _compose_user(state, phase, resolved, entry)
    validate_rendered_model_input(system_text, user_text, settings=settings)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": settings.llm.max_tokens,
        "timeout": settings.llm.timeout_s,
    }
    if settings.llm.anthropic_api_key:
        kwargs["api_key"] = settings.llm.anthropic_api_key
    if settings.llm.adaptive_thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    llm = ChatAnthropic(**kwargs)

    approved_fetch_urls = _approved_fetch_urls(state)
    tools = _build_agent_tools(settings, approved_urls=approved_fetch_urls)
    tools_by_name = {tool.name: tool for tool in tools}
    runnable = llm.bind_tools(tools) if tools else llm

    messages: list[Any] = [SystemMessage(content=system_text), HumanMessage(content=user_text)]
    usage: dict[str, Any] = {}
    tool_calls_record: list[JsonDict] = []
    response: Any = None
    max_tool_rounds = max(1, settings.llm.fetch_max_tool_iters) if tools else 0
    tool_rounds = 0
    tool_context_chars = 0
    tool_context_parts: list[str] = []
    seen_tool_ids: set[str] = set()

    def append_tool_context(value: str, *, truncate: bool) -> str:
        """Account for every provider-controlled character before model reuse."""

        nonlocal tool_context_chars
        # The rendered validation string prefixes the first context part with a
        # newline and separates every later part with one as well.
        separator = 1
        available = max(
            0,
            settings.runs.max_model_input_chars
            - len(system_text)
            - len(user_text)
            - tool_context_chars
            - separator,
        )
        rendered = value
        if len(rendered) > available:
            if not truncate:
                raise ValueError("model tool context exceeds the deployment input limit")
            marker = "\n…[tool result truncated]"
            if available <= len(marker):
                rendered = marker[:available]
            else:
                rendered = rendered[: available - len(marker)] + marker
        if rendered:
            tool_context_parts.append(rendered)
            tool_context_chars += separator + len(rendered)
        validate_rendered_model_input(
            system_text,
            user_text + ("\n" + "\n".join(tool_context_parts) if tool_context_parts else ""),
            settings=settings,
        )
        return rendered

    while True:
        response = runnable.invoke(messages)
        _accumulate_usage(usage, _raw_provider_field(response, "usage_metadata"))
        raw_calls = _response_tool_calls(response)
        if not raw_calls:
            break
        calls = _canonical_tool_calls(
            raw_calls,
            phase=phase,
            attempt=attempt,
            first_index=len(tool_calls_record),
            seen_ids=seen_tool_ids,
        )
        # Release the raw response before tool execution and, critically, before
        # evaluating the next provider call. Otherwise an ignored token cap can
        # keep a huge content body live while the follow-up response is allocated.
        response = None
        raw_calls = []
        call_context = json.dumps(
            calls,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        append_tool_context(call_context, truncate=False)
        # Never retain or resend raw provider content. Tool turns need only the
        # canonical call envelope; ordinary final content is read once below.
        messages.append(AIMessage(content="", tool_calls=calls))
        if tool_rounds >= max_tool_rounds:
            for call in calls:
                content = append_tool_context(
                    "error: configured tool-call round limit reached",
                    truncate=True,
                )
                messages.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=call["id"],
                    )
                )
            response = llm.invoke(messages)
            _accumulate_usage(usage, _raw_provider_field(response, "usage_metadata"))
            if _response_tool_calls(response):
                raise ValueError("model exceeded the configured tool-call round limit")
            break
        tool_rounds += 1
        for call in calls:
            tool_name = call["name"]
            tool = tools_by_name.get(tool_name)
            tool_id = call["id"]
            args = call["args"]
            if len(tool_calls_record) >= MAX_TOOL_CALL_RECORDS:
                raise ValueError("model returned too many aggregate tool calls")
            try:
                remaining_chars = (
                    settings.runs.max_model_input_chars
                    - len(system_text)
                    - len(user_text)
                    - tool_context_chars
                )
                output = (
                    _invoke_agent_tool(
                        tool,
                        args,
                        settings=settings,
                        remaining_chars=remaining_chars,
                        approved_urls=approved_fetch_urls,
                    )
                    if tool is not None
                    else f"error: unknown tool {tool_name!r}"
                )
            except Exception as exc:  # noqa: BLE001 — tool failures feed back to the model
                detail = bounded_diagnostic(exc)
                output = bounded_diagnostic(f"error: {detail}")
                record = ToolCallRecord(
                    id=tool_id,
                    tool=tool_name,
                    args_preview=_safe_tool_args(args),
                    status="error",
                    error=detail,
                ).model_dump(mode="json")
            else:
                record = ToolCallRecord(
                    id=tool_id,
                    tool=tool_name,
                    args_preview=_safe_tool_args(args),
                    status="ok",
                ).model_dump(mode="json")
            tool_calls_record.append(record)
            emit_event(
                {
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "type": "tool_call",
                    "phase": phase.value,
                    "id": record["id"],
                    "tool": record["tool"],
                    "status": record["status"],
                }
            )
            output_text = (
                output
                if type(output) is str
                else bounded_diagnostic(output, max_chars=MAX_GATE_TEXT_CHARS)
            )
            output_text = append_tool_context(output_text, truncate=True)
            messages.append(ToolMessage(content=output_text, tool_call_id=tool_id))

    # Model output is an untrusted durable boundary. It may echo a credential
    # from fetched provider content and providers may return more text than the
    # requested token cap. Redact and bound before the checkpoint or stream can
    # retain the response.
    raw_summary = _message_text(response) or f"[{phase.value}] (no text returned)"
    summary = bounded_diagnostic(raw_summary, max_chars=MAX_GATE_TEXT_CHARS)
    emit_event(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "type": "agent_message",
            "phase": phase.value,
            "model": model,
            "chars": len(summary),
        }
    )
    extra: dict[str, Any] = {}
    if phase is Phase.SCRIPT_SCENARIO:
        extra["load_test_spec"] = _script_scenario_load_test_spec(state, config, attempt)
    return _phase_update(
        phase,
        attempt,
        status=PhaseStatus.RUNNING.value,
        summary=summary,
        reasoning_digest=f"LLM reasoning for {phase.value} via {model} (attempt {attempt}).",
        tool_calls=tool_calls_record,
        model=model,
        usage_metadata=usage,
        **extra,
    )


def _safe_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """Build a recursively bounded, credential-redacted durable JSON preview."""

    remaining_chars = 4_096
    nodes = 0
    active_containers: set[int] = set()

    def key_is_sensitive(key: str) -> bool:
        normalized = "".join(character for character in key.casefold() if character.isalnum())
        return normalized in _TOOL_ARG_SECRET_KEY_MARKERS or any(
            marker in normalized for marker in _TOOL_ARG_SECRET_KEY_FRAGMENTS
        )

    def safe_text(value: Any, *, key: str | None, max_chars: int) -> str:
        """Render one untrusted scalar without preserving credential syntax."""

        nonlocal remaining_chars
        if type(value) is str:
            lowered_key = (key or "").casefold()
            url_context = lowered_key in {"url", "uri", "link"} or lowered_key.endswith(
                ("_url", "_uri", "_link")
            )
            rendered = (
                redact_fetch_url(value)
                if url_context or value.casefold().startswith(("http://", "https://"))
                else bounded_diagnostic(value, max_chars=max_chars)
            )
        else:
            rendered = bounded_diagnostic(value, max_chars=max_chars)
        # Apply the generic scrub after URL normalization too, including to
        # custom-object diagnostics and dynamic mapping keys.
        rendered = bounded_diagnostic(rendered, max_chars=max_chars)
        limit = min(max_chars, remaining_chars)
        result = rendered[:limit]
        remaining_chars -= len(result)
        return result

    def preview(value: Any, *, depth: int, key: str | None = None) -> Any:
        nonlocal remaining_chars, nodes
        if nodes >= 64 or remaining_chars <= 0:
            return "…[preview truncated]"
        nodes += 1
        if key is not None and key_is_sensitive(key):
            return safe_text("[REDACTED]", key=None, max_chars=512)
        if type(value) is str:
            return safe_text(value, key=key, max_chars=512)
        if value is None or type(value) in {bool, int}:
            if type(value) is int and value.bit_length() > 256:
                return safe_text("…[integer omitted]", key=None, max_chars=64)
            return value
        if type(value) is float:
            return (
                value
                if math.isfinite(value)
                else safe_text("…[non-finite number]", key=None, max_chars=64)
            )
        if depth >= 4:
            return "…[max depth]"
        if type(value) in {dict, list, tuple}:
            container_id = id(value)
            if container_id in active_containers:
                return "…[cycle]"
            active_containers.add(container_id)
            try:
                if type(value) is dict:
                    mapping_preview: dict[str, Any] = {}
                    for index, (raw_key, nested) in enumerate(value.items()):
                        if index >= 16:
                            mapping_preview["__truncated__"] = "…[more fields]"
                            break
                        safe_key = (
                            safe_text(raw_key, key=None, max_chars=128)
                            if type(raw_key) is str
                            else "[non-string-key]"
                        ) or "_"
                        mapping_preview[safe_key] = preview(
                            nested,
                            depth=depth + 1,
                            key=safe_key,
                        )
                    return mapping_preview
                list_preview = [preview(item, depth=depth + 1, key=key) for item in value[:16]]
                if len(value) > 16:
                    list_preview.append("…[more items]")
                return list_preview
            finally:
                active_containers.discard(container_id)
        return safe_text(value, key=key, max_chars=512)

    safe = preview(args, depth=0)
    if type(safe) is not dict:
        return {"preview": "…[tool arguments omitted]"}
    if len(json.dumps(safe, ensure_ascii=False).encode("utf-8")) > MAX_TOOL_ARGS_PREVIEW_BYTES:
        return {"preview": "…[tool arguments exceeded byte budget]"}
    return safe


def _make_agent(phase: Phase):
    def agent(state: PipelineState, config: RunnableConfig) -> JsonDict:
        cfg = PipelineConfigurable.from_state_for_phase(state, config, phase)
        if cfg.agent_backend == "anthropic":
            entry = _entry(state, phase)
            attempt = _attempt(entry)
            if not get_settings().llm.anthropic_api_key:
                if getattr(get_settings(), "is_locked_down", False):
                    return _phase_update(
                        phase,
                        attempt,
                        status=PhaseStatus.FAILED.value,
                        summary=f"[{phase.value}] LLM backend unavailable",
                        reasoning_digest="Anthropic API key is required in locked environments",
                        errors=["agent_backend=anthropic requires an Anthropic API key"],
                    )
                # Backend requested but unconfigured: degrade to the stub so a
                # misconfigured run still produces output instead of crashing.
                return _stub_agent_body(
                    phase,
                    state,
                    config,
                    warning="agent_backend=anthropic but no Anthropic API key configured; "
                    "used the deterministic stub",
                )
            try:
                return _llm_agent_body(phase, state, config, cfg, entry, attempt)
            except Exception as exc:  # noqa: BLE001 — surface as a failed phase, not a crash
                detail = bounded_diagnostic(exc)
                emit_event(
                    {
                        "schema_version": EVENT_SCHEMA_VERSION,
                        "type": "agent_error",
                        "phase": phase.value,
                        "error": detail,
                    }
                )
                return _phase_update(
                    phase,
                    attempt,
                    status=PhaseStatus.FAILED.value,
                    summary=f"[{phase.value}] LLM agent error",
                    reasoning_digest=detail,
                    errors=[bounded_diagnostic(f"LLM agent error: {detail}")],
                )
        return _stub_agent_body(phase, state, config)

    return agent


def _checkpoint_review_text(
    value: Any,
    *,
    label: str,
    limit: int = MAX_GATE_TEXT_CHARS,
) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or len(value) > limit
        or "\x00" in value
        or contains_credential_material(value)
    ):
        raise ValueError(f"checkpointed {label} is invalid")
    return value


def _checkpoint_terminal_text(value: Any, *, label: str) -> str | None:
    try:
        return _checkpoint_review_text(value, label=label)
    except ValueError:
        return None


def _checkpoint_terminal_timestamp(value: Any) -> str | None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 64
        or "\x00" in value
        or contains_credential_material(value)
    ):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (OverflowError, ValueError):
        return None
    return value if parsed.tzinfo is not None else None


def _checkpoint_diagnostics(entry: JsonDict, field: str) -> list[str]:
    raw = entry.get(field)
    if raw is None:
        return []
    if type(raw) is not list or len(raw) > MAX_DURABLE_PHASE_DIAGNOSTICS:
        raise ValueError(f"checkpointed phase {field} are invalid")
    diagnostics: list[str] = []
    for value in raw:
        validated = _checkpoint_review_text(
            value,
            label=f"phase {field}",
            limit=4_096,
        )
        if validated is None:
            raise ValueError(f"checkpointed phase {field} are invalid")
        diagnostics.append(validated)
    return diagnostics


def _checkpoint_artifact_previews(state: PipelineState, entry: JsonDict) -> list[JsonDict]:
    raw_ids = entry.get("artifact_ids")
    if raw_ids is None:
        raw_ids = []
    if (
        type(raw_ids) is not list
        or len(raw_ids) > MAX_DURABLE_ARTIFACTS
        or any(
            type(artifact_id) is not str
            or not 1 <= len(artifact_id) <= 128
            or "\x00" in artifact_id
            or contains_credential_material(artifact_id)
            for artifact_id in raw_ids
        )
    ):
        raise ValueError("checkpointed phase artifact ids are invalid")
    artifact_ids = set(raw_ids)

    raw_artifacts = state.get("artifacts")
    if raw_artifacts is None:
        raw_artifacts = []
    if type(raw_artifacts) is not list or len(raw_artifacts) > MAX_DURABLE_ARTIFACTS:
        raise ValueError("checkpointed artifacts are invalid")
    previews: list[JsonDict] = []
    for artifact in raw_artifacts:
        if type(artifact) is not dict:
            raise ValueError("checkpointed artifact is invalid")
        artifact_id = artifact.get("id")
        if type(artifact_id) is not str:
            raise ValueError("checkpointed artifact is invalid")
        if artifact_id not in artifact_ids:
            continue
        kind = artifact.get("kind")
        name = artifact.get("name")
        if (
            type(kind) is not str
            or type(name) is not str
            or not 1 <= len(kind) <= 64
            or not 1 <= len(name) <= 512
            or "\x00" in kind
            or "\x00" in name
            or contains_credential_material({"id": artifact_id, "kind": kind, "name": name})
        ):
            raise ValueError("checkpointed artifact is invalid")
        previews.append({"id": artifact_id, "kind": kind, "name": name})
    return previews


def _checkpoint_phase_dialogue(
    state: PipelineState,
    phase: Phase,
    attempt: int,
) -> list[JsonDict]:
    raw_dialogue = state.get("dialogue")
    if raw_dialogue is None:
        return []
    if type(raw_dialogue) is not list or len(raw_dialogue) > MAX_DURABLE_DIALOGUE_ENTRIES:
        raise ValueError("checkpointed dialogue is invalid")
    phase_dialogue: list[JsonDict] = []
    allowed = {"id", "phase", "attempt", "role", "content", "at"}
    for entry in raw_dialogue:
        if type(entry) is not dict or any(
            type(key) is not str or key not in allowed for key in entry
        ):
            raise ValueError("checkpointed dialogue entry is invalid")
        entry_phase = entry.get("phase")
        entry_attempt = entry.get("attempt", 1)
        entry_id = entry.get("id")
        role = entry.get("role")
        content = entry.get("content")
        at = entry.get("at")
        if (
            type(entry_phase) is not str
            or type(entry_attempt) is not int
            or not 1 <= entry_attempt <= 1_000_000
            or type(entry_id) is not str
            or not 1 <= len(entry_id) <= 256
            or type(role) is not str
            or role not in {"operator", "agent"}
            or type(content) is not str
            or len(content) > MAX_GATE_DECISION_TEXT_CHARS
            or type(at) is not str
            or not 1 <= len(at) <= 64
            or any("\x00" in value for value in (entry_phase, entry_id, role, content, at))
            or contains_credential_material(entry)
        ):
            raise ValueError("checkpointed dialogue entry is invalid")
        if entry_phase == phase.value and entry_attempt == attempt:
            phase_dialogue.append(dict(entry))
    return phase_dialogue


def _make_open_output_gate(phase: Phase):
    def open_output_gate(state: PipelineState, config: RunnableConfig) -> JsonDict:
        cfg = PipelineConfigurable.from_state_for_phase(state, config, phase)
        if cfg.gate_policy(phase).output_review is not GateMode.GATED:
            return {}
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "gate_opened",
                "gate": "phase_review",
                "phase": phase.value,
                "attempt": attempt,
            }
        )
        fields: JsonDict = {"status": PhaseStatus.AWAITING_OUTPUT_REVIEW.value}
        collection_settled = entry.get("engine_collection_settled")
        if collection_settled is not None and type(collection_settled) is not bool:
            raise ValueError("checkpointed engine collection state is invalid")
        if phase is Phase.EXECUTION and collection_settled is True:
            fields.update(
                engine_collection_settled=False,
                engine_collection_final_status=None,
                engine_collection_next=None,
            )
        return _phase_update(phase, attempt, **fields)

    return open_output_gate


def _make_output_gate(phase: Phase):
    def output_gate(state: PipelineState, config: RunnableConfig) -> Command[str]:
        cfg = PipelineConfigurable.from_state_for_phase(state, config, phase)
        if cfg.gate_policy(phase).output_review is not GateMode.GATED:
            return Command(goto="finalize")
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        revise_count = entry.get("revise_count")
        if revise_count is None:
            revise_count = 0
        if type(revise_count) is not int or not 0 <= revise_count <= 10:
            raise ValueError("checkpointed phase revision count is invalid")
        summary = _checkpoint_review_text(entry.get("summary"), label="phase summary")
        reasoning_digest = _checkpoint_review_text(
            entry.get("reasoning_digest"),
            label="phase reasoning digest",
        )
        warnings = _checkpoint_diagnostics(entry, "warnings")
        result_preview = {"summary": summary, "reasoning_digest": reasoning_digest}
        artifact_previews = _checkpoint_artifact_previews(state, entry)
        phase_dialogue = _checkpoint_phase_dialogue(state, phase, attempt)
        if not _phase_review_has_visible_evidence(
            summary,
            reasoning_digest,
            artifact_previews,
            warnings,
            phase_dialogue,
        ):
            # Never checkpoint an interrupt that the public contract must
            # quarantine. A legacy/invalid empty result cannot be approved
            # meaningfully; terminate this attempt with an actionable error.
            return Command(
                goto="finalize",
                update=_phase_update(
                    phase,
                    attempt,
                    status=PhaseStatus.FAILED.value,
                    errors=["phase review requires visible result evidence"],
                ),
            )
        new_dialogue: list[JsonDict] = []
        max_turns = cfg.limits.max_dialogue_turns
        error: str | None = None
        for _ in range(max_turns + MAX_GATE_LOOPS):
            tail = (phase_dialogue + new_dialogue)[-3:]
            payload = build_phase_review_payload(
                phase, summary, result_preview, artifact_previews, warnings, tail, error
            )
            decision = parse_gate_decision(interrupt(payload), PHASE_REVIEW_ACTIONS)
            action = decision["action"]
            error = None
            extra: JsonDict = {"dialogue": new_dialogue} if new_dialogue else {}
            if action == "approve":
                update = _phase_update(
                    phase,
                    attempt,
                    approvals=[make_approval(phase, attempt, "phase_review", "approve", config)],
                )
                return Command(goto="finalize", update={**update, **extra})
            if action == "revise":
                instructions = decision.get("instructions") or ""
                if revise_count >= cfg.limits.max_revise_loops:
                    error = (
                        f"max_revise_loops ({cfg.limits.max_revise_loops}) reached; "
                        "choose approve or abort explicitly"
                    )
                    continue
                revision_fields: JsonDict = {}
                target = "agent"
                if phase is Phase.EXECUTION:
                    # Engine artifacts and normalized KPIs already exist. A report
                    # wording revision must not traverse reserve/start/poll again.
                    target = "open_output_gate"
                    revision_fields = {
                        "summary": bounded_diagnostic(
                            (
                                f"{summary or 'Execution results'} | analysis revised per: "
                                f"{instructions or '(no instructions supplied)'}"
                            ),
                            max_chars=MAX_GATE_TEXT_CHARS,
                        ),
                        "reasoning_digest": (
                            "Execution analysis revision only; the external load run "
                            "was not restarted."
                        ),
                    }
                update = _phase_update(
                    phase,
                    attempt,
                    status=PhaseStatus.RUNNING.value,
                    revise_instructions=instructions,
                    revise_count=revise_count + 1,
                    approvals=[
                        make_approval(
                            phase,
                            attempt,
                            "phase_review",
                            "revise",
                            config,
                            instructions,
                            sequence=revise_count + 1,
                        )
                    ],
                    **revision_fields,
                )
                return Command(goto=target, update={**update, **extra})
            if action == "discuss":
                message = decision.get("message") or ""
                operator_turns = sum(
                    1 for d in phase_dialogue + new_dialogue if d.get("role") == "operator"
                )
                if operator_turns >= max_turns:
                    error = f"max_dialogue_turns ({max_turns}) reached; choose another action"
                    continue
                index = len(phase_dialogue) + len(new_dialogue)
                new_dialogue = new_dialogue + [
                    DialogueEntry(
                        id=f"{phase.value}-a{attempt}-d{index}-operator",
                        phase=phase,
                        attempt=attempt,
                        role="operator",
                        content=message,
                    ).model_dump(mode="json"),
                    DialogueEntry(
                        id=f"{phase.value}-a{attempt}-d{index + 1}-agent",
                        phase=phase,
                        attempt=attempt,
                        role="agent",
                        content=bounded_diagnostic(
                            f"[{phase.value} agent stub] acknowledged: {message}",
                            max_chars=MAX_GATE_DECISION_TEXT_CHARS,
                        ),
                    ).model_dump(mode="json"),
                ]
                continue  # re-interrupt with the refreshed dialogue tail
            if action == "abort":
                update = _phase_update(
                    phase,
                    attempt,
                    status=PhaseStatus.ABORTED.value,
                    approvals=[make_approval(phase, attempt, "phase_review", "abort", config)],
                )
                update["run_aborted"] = True
                return Command(goto="finalize", update={**update, **extra})
            error = decision.get("error") or "unsupported action"
        error = "phase review loop cap reached without an explicit terminal decision"
        update = _phase_update(
            phase,
            attempt,
            status=PhaseStatus.FAILED.value,
            errors=[error],
        )
        extra = {"dialogue": new_dialogue} if new_dialogue else {}
        return Command(goto="finalize", update={**update, **extra})

    return output_gate


def _phase_review_has_visible_evidence(
    summary: str | None,
    reasoning_digest: str | None,
    artifacts: list[JsonDict],
    warnings: list[str],
    dialogue: list[JsonDict],
) -> bool:
    def meaningful(value: Any) -> bool:
        return type(value) is str and bool(value.strip())

    return (
        meaningful(summary)
        or meaningful(reasoning_digest)
        or any(
            all(meaningful(artifact.get(field)) for field in ("id", "kind", "name"))
            for artifact in artifacts
        )
        or any(meaningful(warning) for warning in warnings)
        or any(meaningful(entry.get("content")) for entry in dialogue)
    )


def _make_finalize(phase: Phase):
    def finalize(state: PipelineState, config: RunnableConfig) -> JsonDict:
        # Finalization attributes usage and persists provider-backed evidence.  Validate
        # the durable run contract before the best-effort transcript block so a config
        # drift error cannot be swallowed as an observational artifact warning.
        PipelineConfigurable.from_state_for_phase(state, config, phase)
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        safe_summary = _checkpoint_terminal_text(
            entry.get("summary"),
            label="phase summary",
        )
        safe_reasoning_digest = _checkpoint_terminal_text(
            entry.get("reasoning_digest"),
            label="phase reasoning digest",
        )
        safe_started_at = _checkpoint_terminal_timestamp(entry.get("started_at"))
        # Validate every diagnostic before transcript/telemetry side effects. A
        # malformed checkpoint must not be able to commit those effects and only
        # then fail while building the terminal state update.
        warnings = _checkpoint_diagnostics(entry, "warnings")
        errors = _checkpoint_diagnostics(entry, "errors")

        raw_status = entry.get("status")
        terminal_statuses = {
            PhaseStatus.SUCCEEDED.value,
            PhaseStatus.FAILED.value,
            PhaseStatus.SKIPPED.value,
            PhaseStatus.ABORTED.value,
        }
        completable_statuses = {
            PhaseStatus.RUNNING.value,
            PhaseStatus.AWAITING_OUTPUT_REVIEW.value,
        }
        invalid_terminal_transition = (
            type(raw_status) is not str
            or not 1 <= len(raw_status) <= 64
            or raw_status not in terminal_statuses | completable_statuses
        )
        if invalid_terminal_transition:
            status = PhaseStatus.FAILED.value
        elif raw_status in terminal_statuses:
            status = raw_status
        else:
            status = PhaseStatus.SUCCEEDED.value

        collection_settled = False
        if phase is Phase.EXECUTION:
            raw_collection_settled = entry.get("engine_collection_settled")
            if raw_collection_settled is not None and type(raw_collection_settled) is not bool:
                invalid_terminal_transition = True
            collection_settled = raw_collection_settled is True
            collection_status = entry.get("engine_collection_final_status")
            if collection_settled:
                if collection_status is None:
                    pass
                elif type(collection_status) is str and collection_status in {
                    PhaseStatus.FAILED.value,
                    PhaseStatus.ABORTED.value,
                }:
                    status = collection_status
                else:
                    invalid_terminal_transition = True
            elif collection_status is not None:
                invalid_terminal_transition = True

        if invalid_terminal_transition:
            status = PhaseStatus.FAILED.value
            checkpoint_error = "checkpointed phase terminal transition was invalid"
            if checkpoint_error not in errors:
                errors = [*errors, checkpoint_error][-MAX_DURABLE_PHASE_DIAGNOSTICS:]
        transcript_entry = {
            **entry,
            "summary": safe_summary,
            "reasoning_digest": safe_reasoning_digest,
            "started_at": safe_started_at,
            "warnings": warnings,
            "errors": errors,
        }
        ended_at = utcnow_iso()
        duration_s: float | None = None
        started_at = safe_started_at
        if type(started_at) is str and started_at:
            try:
                parsed_start = datetime.fromisoformat(started_at)
                parsed_end = datetime.fromisoformat(ended_at)
                if parsed_start.tzinfo is None:
                    parsed_start = parsed_start.replace(tzinfo=parsed_end.tzinfo)
                duration_s = max(0.0, (parsed_end - parsed_start).total_seconds())
            except (OverflowError, TypeError, ValueError):
                # A legacy/malformed timestamp must not strand a completed phase
                # before its terminal checkpoint. Duration is observational only.
                duration_s = None
        transcript: JsonDict | None = None
        transcript_warning: str | None = None
        try:
            stored, artifact_connection_id = asyncio.run(
                _persist_transcript(
                    state,
                    phase,
                    transcript_entry,
                    config,
                    attempt=attempt,
                    status=status,
                )
            )
            transcript_summary = (
                safe_reasoning_digest[:MAX_CONTEXT_SUMMARY_CHARS]
                if safe_reasoning_digest is not None
                else None
            )
            transcript = ArtifactRef(
                id=f"{phase.value}-a{attempt}-transcript",
                kind="transcript",
                name=f"{phase.value} transcript (attempt {attempt})",
                uri=stored.uri,
                key=stored.key,
                artifact_connection_id=artifact_connection_id,
                media_type="text/plain",
                summary=transcript_summary,
            ).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - terminal phase state is authoritative
            transcript_warning = "phase transcript persistence failed"
            logger.warning(
                "pipeline.transcript_persistence_failed",
                phase=phase.value,
                attempt=attempt,
                error=bounded_diagnostic(exc),
            )
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "phase_status",
                "phase": phase.value,
                "status": status,
                "attempt": attempt,
            }
        )
        # Usage analytics (M6): best-effort, never fails the run.
        usage_events.record_phase_usage_sync(phase.value, status, config, attempt=attempt)
        usage_metadata = entry.get("usage_metadata")
        recorded_model = entry.get("model")
        normalized_usage: dict[str, Any] = {}
        _accumulate_usage(normalized_usage, usage_metadata)
        safe_model = (
            recorded_model
            if type(recorded_model) is str
            and 1 <= len(recorded_model) <= 200
            and "\x00" not in recorded_model
            and not contains_credential_material(recorded_model)
            else None
        )
        usage_events.record_agent_event_sync(
            phase=phase.value,
            status=status,
            attempt=attempt,
            config=config,
            latency_ms=max(0, round(duration_s * 1000)) if duration_s is not None else None,
            usage=normalized_usage if normalized_usage else None,
            agent_name=f"{phase.value}.worker",
            model=safe_model,
        )
        if transcript_warning is not None:
            if transcript_warning not in warnings:
                warnings = [*warnings, transcript_warning][-MAX_DURABLE_PHASE_DIAGNOSTICS:]
        finalize_fields: JsonDict = {
            "status": status,
            "summary": safe_summary,
            "reasoning_digest": safe_reasoning_digest,
            "started_at": safe_started_at,
            "ended_at": ended_at,
            "duration_s": duration_s,
            "artifact_ids": [transcript["id"]] if transcript is not None else [],
            "transcript_ref": transcript,
            "warnings": warnings,
            "errors": errors,
        }
        if phase is Phase.EXECUTION:
            finalize_fields.update(
                engine_collection_settled=False,
                engine_collection_final_status=None,
                engine_collection_next=None,
            )
        update = _phase_update(
            phase,
            attempt,
            **finalize_fields,
        )
        if collection_settled and status == PhaseStatus.ABORTED.value:
            update["run_aborted"] = True
        if transcript is not None:
            update["artifacts"] = [transcript]
        return update

    return finalize


def make_phase_subgraph(phase: Phase) -> CompiledStateGraph[PipelineState, Any, Any, Any]:
    """Build the compiled phase spine for one Phase, sharing the parent state schema.

    Compiled without a checkpointer so it inherits the parent graph's persistence.
    For Phase.EXECUTION the stub agent is replaced by the engine spine (the gates
    still route to "agent", which the engine wiring provides as a no-op alias into
    engine_reserve; engine_collect checkpoints output before the settle node routes
    to open_output_gate / finalize).
    """
    builder = StateGraph(PipelineState)
    builder.add_node("prepare", _make_prepare(phase))
    builder.add_node("open_prompt_gate", _make_open_prompt_gate(phase))
    builder.add_node("prompt_gate", _make_prompt_gate(phase), destinations=("agent", "finalize"))
    builder.add_node("open_output_gate", _make_open_output_gate(phase))
    builder.add_node(
        "output_gate",
        _make_output_gate(phase),
        destinations=("agent", "open_output_gate", "finalize"),
    )
    builder.add_node("finalize", _make_finalize(phase))
    builder.add_conditional_edges(
        "prepare", _make_route_after_prepare(phase), ["open_prompt_gate", "finalize"]
    )
    builder.add_edge("open_prompt_gate", "prompt_gate")
    if phase is Phase.EXECUTION:
        # Imported lazily: execution_phase imports helpers from this module, so a
        # top-level import here would be circular.
        from apex.graphs.pipeline.execution_phase import (
            add_execution_engine_nodes,
            route_execution_entry,
        )

        add_execution_engine_nodes(builder)
        builder.add_conditional_edges(
            START,
            route_execution_entry,
            [
                "prepare",
                "engine_cleanup",
                "engine_provision_resume",
                "engine_collection_resume",
                "engine_collection_settle",
                "engine_collection_settle_resume",
                "open_output_gate",
                "finalize",
            ],
        )
    else:
        builder.add_edge(START, "prepare")
        builder.add_node("agent", _make_agent(phase))
        builder.add_edge("agent", "open_output_gate")
    builder.add_edge("open_output_gate", "output_gate")
    builder.add_edge("finalize", END)
    return builder.compile(name=f"phase_{phase.value}")


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "emit_event",
    "make_phase_subgraph",
    "stub_tool_names",
]
