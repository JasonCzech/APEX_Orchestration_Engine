"""7-phase master pipeline graph (ADR-0004).

Topology: plan_resolver entry node -> conditional-edge router -> one compiled phase
subgraph per Phase -> END. The router re-runs after every phase, picking the first
phase in the run plan whose result is still non-terminal, so a phase subset run jumps
straight to its target and a mid-run abort short-circuits to END.

Compiled WITHOUT a checkpointer: the LangGraph server injects persistence; tests
compile `builder` with InMemorySaver.
"""

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from apex.domain.pipeline import (
    PHASE_ORDER,
    PHASE_PREREQUISITES,
    TERMINAL_PHASE_STATUSES,
    ContextPacket,
    ExternalResults,
    Phase,
    PhaseResult,
    PhaseStatus,
)
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.phase_subgraph import (
    EVENT_SCHEMA_VERSION,
    emit_event,
    make_phase_subgraph,
)
from apex.graphs.pipeline.state import JsonDict, PipelineInput, PipelineState
from apex.services.prompts import prompt_review_from_resolved, resolve_phase_prompts_sync
from apex.services.run_validation import validate_context_packets, validate_pipeline_input


def _prompt_variables(state: PipelineState) -> dict[str, str]:
    return {
        "title": state.get("title") or "untitled run",
        "request": state.get("request") or "(no request provided)",
    }


def _external_seed(external: JsonDict, *, attempt: int) -> tuple[JsonDict, JsonDict]:
    """Synthetic succeeded-execution result + context packet from external results.

    Maps the ExternalResults payload onto the execution phase's test_summary shape so
    the reporting phase reads externally-supplied results exactly as it reads a real
    engine run. The packet gives the analysis agent the source link/notes as evidence.
    """
    results = ExternalResults.model_validate(external)
    test_summary = {
        "engine": results.engine or results.source,
        "passed": True if results.passed is None else bool(results.passed),
        "kpis": dict(results.kpis),
        "sla_breaches": [],
        "notes": results.notes,
    }
    summary_text = results.summary or f"External results supplied by {results.source}"
    execution_entry = {
        **PhaseResult(
            phase=Phase.EXECUTION,
            status=PhaseStatus.SUCCEEDED,
            attempt=attempt,
            summary=summary_text,
        ).as_state(),
        "test_summary": test_summary,
        "external": True,
        "source_uri": results.uri,
    }
    packet = ContextPacket(
        id="external-results",
        source=results.source,
        title="External results",
        summary=summary_text,
        ref=results.uri,
    ).model_dump(mode="json")
    return execution_entry, packet


def plan_resolver(state: PipelineState, config: RunnableConfig) -> JsonDict:
    """Resolve the run plan from config, validate prerequisites, seed phase entries."""
    # Direct LangGraph calls and assistant/checkpoint merges bypass the REST model,
    # so validate the final state immediately before any phase can spend resources.
    validate_pipeline_input(state)
    cfg = PipelineConfigurable.from_config(config)
    selected = cfg.selected_phases()
    if not selected:
        raise ValueError("Pipeline phase plan is empty")
    configurable = config.get("configurable") or {}
    if not str(configurable.get("thread_id") or "").strip():
        raise ValueError(
            "pipeline runs require a durable thread_id; stateless runs are not allowed"
        )
    selected_set = set(selected)  # canonical order => membership implies "runs earlier"
    existing = dict(state.get("phase_results") or {})

    # Externally-supplied results seed a succeeded execution entry so analysis-only
    # runs (reporting/postmortem) satisfy the execution prerequisite honestly, without
    # the caller forging internal phase state. Skipped if execution is itself selected
    # on this run. A newly supplied envelope deliberately supersedes earlier
    # execution evidence and therefore receives the next monotonic attempt.
    external = state.get("external_results")
    external_execution: JsonDict | None = None
    external_packet: JsonDict | None = None
    if external and Phase.EXECUTION not in selected_set:
        exec_current = existing.get(Phase.EXECUTION.value) or {}
        external_attempt = max(1, int(exec_current.get("attempt") or 0) + 1)
        external_execution, external_packet = _external_seed(external, attempt=external_attempt)
        existing[Phase.EXECUTION.value] = external_execution

    for phase in selected:
        for prereq in PHASE_PREREQUISITES[phase]:
            if prereq in selected_set:
                continue
            if existing.get(prereq.value, {}).get("status") == PhaseStatus.SUCCEEDED:
                continue
            raise ValueError(
                f"Cannot run phase '{phase.value}': prerequisite '{prereq.value}' is "
                "neither earlier in the selected plan nor succeeded on this thread."
            )

    seeded: dict[str, JsonDict] = {}
    if external_execution is not None:
        seeded[Phase.EXECUTION.value] = external_execution
    for phase in selected:
        current = existing.get(phase.value)
        if current and current.get("status") in TERMINAL_PHASE_STATUSES:
            attempt = int(current.get("attempt") or 0) + 1
        elif current:
            attempt = int(current.get("attempt") or 1)
        else:
            attempt = 1
        seeded[phase.value] = PhaseResult(
            phase=phase, status=PhaseStatus.PENDING, attempt=attempt
        ).as_state()

    existing_reviews = state.get("prompt_reviews") or {}
    missing_review_phases = [phase for phase in PHASE_ORDER if phase.value not in existing_reviews]
    seeded_reviews: dict[str, JsonDict] = {}
    if missing_review_phases:
        resolved_reviews = resolve_phase_prompts_sync(
            missing_review_phases,
            cfg,
            variables=_prompt_variables(state),
        )
        seeded_reviews = {
            phase.value: dict(prompt_review_from_resolved(resolved_reviews[phase]))
            for phase in missing_review_phases
        }

    plan = [phase.value for phase in selected]
    emit_event({"schema_version": EVENT_SCHEMA_VERSION, "type": "plan_resolved", "phases": plan})
    update = {
        "phases_plan": plan,
        "current_phase": None,
        "run_aborted": False,
        "phase_results": seeded,
        # RunnableConfig is not recoverable from a checkpoint. Persist every
        # user-controlled run setting so a later human-gate resume cannot silently
        # fall back to another project, engine, connection, model, or gate policy.
        "run_config": cfg.snapshot(),
        # Legacy/read-model convenience; run_config is authoritative for new runs.
        "limits": cfg.limits.model_dump(mode="json"),
        # ExternalResults is a per-run input envelope. Consume it even when this
        # run selected a real execution phase so a later checkpoint resume/rerun
        # can never silently reuse stale caller-supplied evidence.
        "external_results": None,
    }
    if Phase.EXECUTION in selected_set:
        # A thread rerun starts a new execution attempt. Do not leave the previous
        # attempt's top-level handle available to the abort facade while the new
        # attempt is still in prompt review or provisioning.
        update["engine_handle"] = None
    if seeded_reviews:
        update["prompt_reviews"] = seeded_reviews
    if external_packet is not None:
        current_packets = list(state.get("context_packets") or [])
        candidate_packets = [
            packet for packet in current_packets if packet.get("id") != external_packet["id"]
        ]
        validate_context_packets([*candidate_packets, external_packet])
        update["context_packets"] = [external_packet]
    return update


def route_next_phase(state: PipelineState) -> str:
    """Next plan phase whose result is non-terminal; END on abort or plan exhaustion."""
    if state.get("run_aborted"):
        return END
    results = state.get("phase_results") or {}
    for name in state.get("phases_plan") or []:
        entry = results.get(name)
        if not entry or entry.get("status") not in TERMINAL_PHASE_STATUSES:
            return name
    return END


builder = StateGraph(PipelineState, input_schema=PipelineInput)
builder.add_node("plan_resolver", plan_resolver)
builder.add_edge(START, "plan_resolver")

_ROUTE_TARGETS = [phase.value for phase in PHASE_ORDER] + [END]
builder.add_conditional_edges("plan_resolver", route_next_phase, _ROUTE_TARGETS)
for _phase in PHASE_ORDER:
    builder.add_node(_phase.value, make_phase_subgraph(_phase))
    builder.add_conditional_edges(_phase.value, route_next_phase, _ROUTE_TARGETS)

# Compiled without a checkpointer: the LangGraph server injects its own persistence.
graph = builder.compile(name="pipeline")
