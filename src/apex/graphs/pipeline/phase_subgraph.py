"""Per-phase subgraph factory: prepare -> prompt_gate -> agent -> output_gate -> finalize.

Each interrupt lives in its own node (resume re-executes the interrupting node from the
top, so gate nodes are side-effect-free before interrupt()). The awaiting_* status and
gate_opened event are committed by a separate "open_*" node so they are durable in the
checkpoint before the graph pauses. Agent bodies are deterministic M1 stubs (no LLM, no
ports); state writes use deterministic ids so node re-execution stays idempotent under
the append-unique reducers.

M3: the execution phase swaps the stub agent for the checkpointed engine spine
(engine_reserve -> engine_start -> engine_poll ⟲ -> engine_collect, see
apex.graphs.pipeline.execution_phase); the gates around it are unchanged. The
script_scenario stub additionally emits a LoadTestSpec ("load_test_spec" entry
key) that the execution phase consumes, and the reporting stub mentions the
execution phase's test_summary KPIs when present.
"""

from datetime import datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from apex.domain.integrations import LoadTestSpec
from apex.domain.pipeline import (
    PHASE_PREREQUISITES,
    TERMINAL_PHASE_STATUSES,
    ArtifactRef,
    DialogueEntry,
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
from apex.graphs.pipeline.state import JsonDict, PipelineState
from apex.services import usage as usage_events
from apex.services.prompts import (
    prompt_review_from_resolved,
    resolve_phase_prompt_sync,
    resolved_from_prompt_review,
    user_prompt_with_context,
)

EVENT_SCHEMA_VERSION = 1

# Bounds re-interrupt loops inside a single gate node (modify re-reviews, bad actions).
MAX_GATE_LOOPS = 10


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
    return {
        "title": state.get("title") or "untitled run",
        "request": state.get("request") or "(no request provided)",
    }


def _entry(state: PipelineState, phase: Phase) -> JsonDict:
    return (state.get("phase_results") or {}).get(phase.value) or {}


def _attempt(entry: JsonDict) -> int:
    return int(entry.get("attempt") or 1)


def _phase_update(phase: Phase, attempt: int, **fields: Any) -> JsonDict:
    # The merge reducer replaces wholesale when attempt differs, so every partial
    # update must carry the current attempt to merge instead of clobber.
    return {"phase_results": {phase.value: {"attempt": attempt, **fields}}}


def _thread_id(config: RunnableConfig | None) -> str:
    configurable = dict((config or {}).get("configurable") or {})
    return str(configurable.get("thread_id") or "no-thread")


def _prerequisite_error(state: PipelineState, phase: Phase) -> str | None:
    results = state.get("phase_results") or {}
    for prereq in PHASE_PREREQUISITES[phase]:
        status = (results.get(prereq.value) or {}).get("status")
        if status != PhaseStatus.SUCCEEDED.value:
            return (
                f"Cannot run phase '{phase.value}': prerequisite '{prereq.value}' "
                f"ended with status '{status or 'missing'}'."
            )
    return None


def _state_application_override(state: PipelineState, cfg: PipelineConfigurable) -> str | None:
    """Run-scoped, app-wide application prompt override content, if set.

    Stored once per run keyed by app_id, so an edit on any phase is shared by
    every phase of the run.
    """
    if not cfg.app_id:
        return None
    override = (state.get("application_reviews") or {}).get(cfg.app_id)
    if isinstance(override, dict) and override.get("content") is not None:
        return str(override["content"])
    return None


def _apply_application_override(
    state: PipelineState,
    cfg: PipelineConfigurable,
    review: JsonDict,
    resolved: JsonDict,
) -> tuple[JsonDict, JsonDict]:
    override = _state_application_override(state, cfg)
    if override is None:
        return review, resolved
    return {**review, "application": override}, {**resolved, "application": override}


def _application_review_update(
    cfg: PipelineConfigurable,
    application: Any,
    source: JsonDict,
    config: RunnableConfig,
) -> JsonDict:
    """State update recording the run-scoped, app-wide application override."""
    if not cfg.app_id or application is None:
        return {}
    return {
        "application_reviews": {
            cfg.app_id: {
                "content": str(application),
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
    reviews = state.get("prompt_reviews") or {}
    review = reviews.get(phase.value)
    if isinstance(review, dict):
        return _apply_application_override(
            state, cfg, review, dict(resolved_from_prompt_review(review))
        )

    entry = _entry(state, phase)
    prompt = entry.get("resolved_prompt")
    if isinstance(prompt, dict) and (prompt.get("system") or prompt.get("user")):
        source = entry.get("resolved_prompt_source")
        review = {
            "system": prompt.get("system") or "",
            "phase_prompt": prompt.get("user") or "",
            "application": prompt.get("application"),
            "additional_context": "",
            "source": dict(source) if isinstance(source, dict) else {"origin": "catalog"},
            "updated_at": utcnow_iso(),
            "updated_by": "system",
        }
        return _apply_application_override(
            state,
            cfg,
            review,
            {
                "system": review["system"],
                "user": review["phase_prompt"],
                "application": review["application"],
                "source": review["source"],
            },
        )

    resolved = resolve_phase_prompt_sync(phase, cfg, variables=_prompt_variables(state))
    review = dict(prompt_review_from_resolved(resolved))
    return _apply_application_override(state, cfg, review, dict(resolved))


def _prompt_review_update(phase: Phase, review: JsonDict) -> JsonDict:
    return {"prompt_reviews": {phase.value: review}}


def _review_to_prompt(review: JsonDict) -> JsonDict:
    return {
        "system": review.get("system") or "",
        "user": review.get("phase_prompt") or "",
        "application": review.get("application"),
    }


def _resolved_prompt_from_review(review: JsonDict) -> JsonDict:
    return {
        "system": review.get("system") or "",
        "user": user_prompt_with_context(
            str(review.get("phase_prompt") or ""),
            str(review.get("additional_context") or ""),
        ),
        "application": review.get("application"),
    }


def _gate_edited_review(
    phase: Phase,
    review: JsonDict,
    prompt: JsonDict,
    source: JsonDict,
    config: RunnableConfig,
    note: Any = None,
) -> JsonDict:
    next_review = {
        **review,
        "system": prompt.get("system") or "",
        "phase_prompt": prompt.get("user") or "",
        "application": prompt.get("application"),
        "source": source,
        "updated_at": utcnow_iso(),
        "updated_by": resolve_actor(config),
    }
    if note is not None:
        next_review["additional_context"] = str(note)
    elif "additional_context" not in next_review:
        next_review["additional_context"] = ""
    return next_review


def _make_prepare(phase: Phase):
    def prepare(state: PipelineState, config: RunnableConfig) -> JsonDict:
        cfg = PipelineConfigurable.from_config(config)
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
        if phase.value not in (state.get("prompt_reviews") or {}):
            update.update(_prompt_review_update(phase, review))
        update["current_phase"] = phase.value
        return update

    return prepare


def _make_route_after_prepare(phase: Phase):
    def route_after_prepare(state: PipelineState) -> str:
        entry = _entry(state, phase)
        if entry.get("status") == PhaseStatus.FAILED.value and entry.get("errors"):
            return "finalize"
        return "open_prompt_gate"

    return route_after_prepare


def _make_open_prompt_gate(phase: Phase):
    def open_prompt_gate(state: PipelineState, config: RunnableConfig) -> JsonDict:
        cfg = PipelineConfigurable.from_config(config)
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
        cfg = PipelineConfigurable.from_config(config)
        if cfg.gate_policy(phase).prompt_review is not GateMode.GATED:
            return Command(goto="agent")
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        review, _resolved = _review_source_for_phase(state, phase, cfg)
        prompt = _review_to_prompt(review)
        source = dict(review.get("source") or entry.get("resolved_prompt_source") or {})
        additional_context = str(review.get("additional_context") or "")
        original_application = review.get("application")
        packets = list(state.get("context_packets") or [])
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
                    next_context = str(decision.get("note") or "")
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
                if isinstance(edit, dict):
                    prompt = {
                        "system": edit.get("system", prompt.get("system")),
                        "user": edit.get("user", prompt.get("user")),
                        "application": edit.get("application", prompt.get("application")),
                    }
                if decision.get("note") is not None:
                    additional_context = str(decision.get("note") or "")
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
        warning = f"prompt review loop cap ({MAX_GATE_LOOPS}) reached; proceeding with last prompt"
        update = _phase_update(
            phase,
            attempt,
            resolved_prompt=_resolved_prompt_from_review(review),
            resolved_prompt_source=source,
            warnings=[warning],
        )
        update.update(_prompt_review_update(phase, review))
        if review.get("application") != original_application:
            update.update(
                _application_review_update(cfg, review.get("application"), source, config)
            )
        return Command(goto="agent", update=update)

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
    configurable = dict((config or {}).get("configurable") or {})
    thread_id = str(configurable.get("thread_id") or "no-thread")
    overrides = configurable.get("load_test")
    overrides = dict(overrides) if isinstance(overrides, dict) else {}
    title = state.get("title") or "untitled run"
    spec = LoadTestSpec(
        idempotency_key=f"{thread_id}-execution-a{attempt}",
        title=f"{title} load test",
        script_refs=[f"stub://scripts/{thread_id}/script_scenario-a{attempt}.jmx"],
        vusers=int(overrides.get("vusers") or 10),
        ramp_s=float(overrides.get("ramp_s") or 1.0),
        duration_s=float(overrides.get("duration_s") or 2.0),
        slas={"p95_ms": 500.0, "error_rate": 0.05},
        target_environment=PipelineConfigurable.from_config(config).environment_id,
    )
    return spec.model_dump(mode="json")


def _reporting_kpi_suffix(state: PipelineState) -> str:
    """Deterministic KPI mention when the execution phase produced a test_summary."""
    test_summary = _entry(state, Phase.EXECUTION).get("test_summary")
    if not isinstance(test_summary, dict):
        return ""
    kpis = test_summary.get("kpis") or {}
    kpi_text = ", ".join(
        f"{key}={value:g}" if isinstance(value, int | float) else f"{key}={value}"
        for key, value in sorted(kpis.items())
    )
    verdict = "passed" if test_summary.get("passed") else "failed"
    return (
        f" | execution {verdict} on engine {test_summary.get('engine')} — "
        f"KPIs: {kpi_text or 'none reported'}"
    )


def _make_agent(phase: Phase):
    def agent(state: PipelineState, config: RunnableConfig) -> JsonDict:
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        revise_count = int(entry.get("revise_count") or 0)
        title = state.get("title") or "untitled run"
        request = state.get("request") or "(no request provided)"
        summary = f"[{phase.value}] stub result for '{title}': {request}"
        instructions = entry.get("revise_instructions")
        if instructions:
            summary += f" (revised per: {instructions})"
        if phase is Phase.REPORTING:
            summary += _reporting_kpi_suffix(state)
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
        return _phase_update(
            phase,
            attempt,
            status=PhaseStatus.RUNNING.value,
            summary=summary,
            reasoning_digest=digest,
            tool_calls=[tool_call],
            **extra,
        )

    return agent


def _make_open_output_gate(phase: Phase):
    def open_output_gate(state: PipelineState, config: RunnableConfig) -> JsonDict:
        cfg = PipelineConfigurable.from_config(config)
        if cfg.gate_policy(phase).output_review is not GateMode.GATED:
            return {}
        attempt = _attempt(_entry(state, phase))
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "gate_opened",
                "gate": "phase_review",
                "phase": phase.value,
                "attempt": attempt,
            }
        )
        return _phase_update(phase, attempt, status=PhaseStatus.AWAITING_OUTPUT_REVIEW.value)

    return open_output_gate


def _make_output_gate(phase: Phase):
    def output_gate(state: PipelineState, config: RunnableConfig) -> Command[str]:
        cfg = PipelineConfigurable.from_config(config)
        if cfg.gate_policy(phase).output_review is not GateMode.GATED:
            return Command(goto="finalize")
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        revise_count = int(entry.get("revise_count") or 0)
        summary = entry.get("summary")
        warnings = list(entry.get("warnings") or [])
        result_preview = {"summary": summary, "reasoning_digest": entry.get("reasoning_digest")}
        artifact_ids = set(entry.get("artifact_ids") or [])
        artifact_previews = [
            {"id": a.get("id"), "kind": a.get("kind"), "name": a.get("name")}
            for a in state.get("artifacts") or []
            if a.get("id") in artifact_ids
        ]
        phase_dialogue = [d for d in state.get("dialogue") or [] if d.get("phase") == phase.value]
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
                instructions = str(decision.get("instructions") or "")
                if revise_count >= cfg.limits.max_revise_loops:
                    warning = (
                        f"max_revise_loops ({cfg.limits.max_revise_loops}) reached; "
                        "proceeding as approve"
                    )
                    update = _phase_update(
                        phase,
                        attempt,
                        warnings=[warning],
                        approvals=[
                            make_approval(
                                phase,
                                attempt,
                                "phase_review",
                                "approve",
                                config,
                                warning,
                            )
                        ],
                    )
                    return Command(goto="finalize", update={**update, **extra})
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
                        )
                    ],
                )
                return Command(goto="agent", update={**update, **extra})
            if action == "discuss":
                message = str(decision.get("message") or "")
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
                        role="operator",
                        content=message,
                    ).model_dump(mode="json"),
                    DialogueEntry(
                        id=f"{phase.value}-a{attempt}-d{index + 1}-agent",
                        phase=phase,
                        role="agent",
                        content=f"[{phase.value} agent stub] acknowledged: {message}",
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
        warning = "phase review loop cap reached; proceeding as approve"
        update = _phase_update(
            phase,
            attempt,
            warnings=[warning],
            approvals=[make_approval(phase, attempt, "phase_review", "approve", config, warning)],
        )
        extra = {"dialogue": new_dialogue} if new_dialogue else {}
        return Command(goto="finalize", update={**update, **extra})

    return output_gate


def _make_finalize(phase: Phase):
    def finalize(state: PipelineState, config: RunnableConfig) -> JsonDict:
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        status = entry.get("status")
        if status not in TERMINAL_PHASE_STATUSES:
            status = PhaseStatus.SUCCEEDED.value
        ended_at = utcnow_iso()
        duration_s: float | None = None
        started_at = entry.get("started_at")
        if started_at:
            delta = datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)
            duration_s = delta.total_seconds()
        transcript = ArtifactRef(
            id=f"{phase.value}-a{attempt}-transcript",
            kind="transcript",
            name=f"{phase.value} transcript (attempt {attempt})",
            uri=f"memory://transcripts/{_thread_id(config)}/{phase.value}/attempt-{attempt}",
            media_type="text/plain",
            summary=entry.get("reasoning_digest"),
        ).model_dump(mode="json")
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
        usage_events.record_phase_usage_sync(phase.value, str(status), config)
        usage_metadata = entry.get("usage_metadata")
        usage_events.record_agent_event_sync(
            phase=phase.value,
            status=str(status),
            attempt=attempt,
            config=config,
            latency_ms=max(0, round(duration_s * 1000)) if duration_s is not None else None,
            usage=usage_metadata if isinstance(usage_metadata, dict) else None,
            agent_name=f"{phase.value}.worker",
        )
        update = _phase_update(
            phase,
            attempt,
            status=status,
            ended_at=ended_at,
            duration_s=duration_s,
            artifact_ids=[transcript["id"]],
            transcript_ref=transcript,
        )
        update["artifacts"] = [transcript]
        return update

    return finalize


def make_phase_subgraph(phase: Phase) -> CompiledStateGraph[PipelineState, Any, Any, Any]:
    """Build the compiled phase spine for one Phase, sharing the parent state schema.

    Compiled without a checkpointer so it inherits the parent graph's persistence.
    For Phase.EXECUTION the stub agent is replaced by the engine spine (the gates
    still route to "agent", which the engine wiring provides as a no-op alias into
    engine_reserve; engine_collect routes to open_output_gate / finalize itself).
    """
    builder = StateGraph(PipelineState)
    builder.add_node("prepare", _make_prepare(phase))
    builder.add_node("open_prompt_gate", _make_open_prompt_gate(phase))
    builder.add_node("prompt_gate", _make_prompt_gate(phase), destinations=("agent", "finalize"))
    builder.add_node("open_output_gate", _make_open_output_gate(phase))
    builder.add_node("output_gate", _make_output_gate(phase), destinations=("agent", "finalize"))
    builder.add_node("finalize", _make_finalize(phase))
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges(
        "prepare", _make_route_after_prepare(phase), ["open_prompt_gate", "finalize"]
    )
    builder.add_edge("open_prompt_gate", "prompt_gate")
    if phase is Phase.EXECUTION:
        # Imported lazily: execution_phase imports helpers from this module, so a
        # top-level import here would be circular.
        from apex.graphs.pipeline.execution_phase import add_execution_engine_nodes

        add_execution_engine_nodes(builder)
    else:
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
