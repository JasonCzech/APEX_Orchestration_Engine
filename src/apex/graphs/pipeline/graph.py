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

from apex.domain.diagnostics import contains_credential_material
from apex.domain.pipeline import (
    PHASE_ORDER,
    PHASE_PREREQUISITES,
    TERMINAL_PHASE_STATUSES,
    ContextPacket,
    EngineHandle,
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

_PHASE_STATUS_VALUES = frozenset(status.value for status in PhaseStatus)


def _prompt_variables(state: PipelineState) -> dict[str, str]:
    title = state.get("title")
    request = state.get("request")
    if title is None:
        title = "untitled run"
    elif type(title) is not str or len(title) > 500 or "\x00" in title:
        raise ValueError("checkpointed pipeline title is invalid")
    elif title == "":
        title = "untitled run"
    if request is None:
        request = "(no request provided)"
    elif type(request) is not str or len(request) > 20_000 or "\x00" in request:
        raise ValueError("checkpointed pipeline request is invalid")
    elif request == "":
        request = "(no request provided)"
    variables = {
        "title": title,
        "request": request,
    }
    if contains_credential_material(variables, max_nodes=4, max_total_chars=20_564):
        raise ValueError("checkpointed pipeline intent contains credential material")
    return variables


def _checkpoint_attempt(value: object, *, default: int) -> int:
    if value is None:
        return default
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError("checkpointed phase attempt is invalid")
    return value


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
    if type(config) is not dict:
        raise ValueError("pipeline run configuration is invalid")
    configurable = config.get("configurable")
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
        raise ValueError(
            "pipeline runs require a durable thread_id; stateless runs are not allowed"
        )
    selected_set = set(selected)  # canonical order => membership implies "runs earlier"
    previous_handle = state.get("engine_handle")
    if previous_handle is not None:
        if type(previous_handle) is not dict or contains_credential_material(previous_handle):
            raise ValueError("checkpointed engine handle is invalid")
        validated_handle: EngineHandle | None = None
        try:
            validated_handle = EngineHandle.model_validate(previous_handle, strict=True)
        except (TypeError, ValueError):
            pass
        if validated_handle is None:
            raise ValueError("checkpointed engine handle is invalid")
        if validated_handle.model_dump(mode="json") != previous_handle:
            raise ValueError("checkpointed engine handle is not canonical")
    raw_existing = state.get("phase_results")
    if raw_existing is None:
        raw_existing = {}
    if type(raw_existing) is not dict or any(
        type(name) is not str or type(entry) is not dict for name, entry in raw_existing.items()
    ):
        raise ValueError("checkpointed phase results are invalid")
    existing = dict(raw_existing)

    # Externally-supplied results seed a succeeded execution entry so analysis-only
    # runs (reporting/postmortem) satisfy the execution prerequisite honestly, without
    # the caller forging internal phase state. Skipped if execution is itself selected
    # on this run. A newly supplied envelope deliberately supersedes earlier
    # execution evidence and therefore receives the next monotonic attempt.
    external = state.get("external_results")
    external_execution: JsonDict | None = None
    external_packet: JsonDict | None = None
    if external is not None and Phase.EXECUTION not in selected_set:
        exec_current = existing.get(Phase.EXECUTION.value)
        if exec_current is None:
            exec_current = {}
        if type(exec_current) is not dict:
            raise ValueError("checkpointed execution phase is invalid")
        external_attempt = max(
            1,
            _checkpoint_attempt(exec_current.get("attempt"), default=0) + 1,
        )
        external_execution, external_packet = _external_seed(external, attempt=external_attempt)
        existing[Phase.EXECUTION.value] = external_execution

    for phase in selected:
        for prereq in PHASE_PREREQUISITES[phase]:
            if prereq in selected_set:
                continue
            prereq_entry = existing.get(prereq.value)
            if prereq_entry is not None and type(prereq_entry) is not dict:
                raise ValueError("checkpointed prerequisite phase is invalid")
            prereq_status = prereq_entry.get("status") if prereq_entry is not None else None
            if prereq_status is not None and type(prereq_status) is not str:
                raise ValueError("checkpointed prerequisite phase status is invalid")
            if prereq_status == PhaseStatus.SUCCEEDED:
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
        if current is not None and type(current) is not dict:
            raise ValueError("checkpointed phase result is invalid")
        current_status = current.get("status") if current is not None else None
        if current_status is not None and (
            type(current_status) is not str or current_status not in _PHASE_STATUS_VALUES
        ):
            raise ValueError("checkpointed phase status is invalid")
        if current is not None and current_status in TERMINAL_PHASE_STATUSES:
            attempt = _checkpoint_attempt(current.get("attempt"), default=0) + 1
        elif current is not None and len(current) > 0:
            attempt = _checkpoint_attempt(current.get("attempt"), default=1)
        else:
            attempt = 1
        seeded[phase.value] = PhaseResult(
            phase=phase, status=PhaseStatus.PENDING, attempt=attempt
        ).as_state()

    existing_reviews = state.get("prompt_reviews")
    if existing_reviews is None:
        existing_reviews = {}
    if type(existing_reviews) is not dict:
        raise ValueError("checkpointed prompt reviews are invalid")
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
        # Force reducer validation of inherited review channels even when this
        # run already has every phase review. Otherwise a malformed legacy value
        # for an unselected phase/application can ride into a fresh checkpoint
        # without any node ever reading it.
        "prompt_reviews": seeded_reviews,
        "application_reviews": {},
        # Touch every accumulated JSON channel at the new-run boundary. Their
        # reducers revalidate inherited legacy state even if the run pauses at
        # its first human gate before a phase would otherwise append anything.
        "artifacts": [],
        "dialogue": [],
    }
    if Phase.EXECUTION in selected_set:
        # A thread rerun starts a new execution attempt. Do not leave the previous
        # attempt's top-level handle available to the abort facade while the new
        # attempt is still in prompt review or provisioning.
        update["engine_handle"] = None
    if external_packet is not None:
        raw_packets = state.get("context_packets")
        if raw_packets is None:
            raw_packets = []
        current_packets = validate_context_packets(raw_packets)
        candidate_packets = [
            packet for packet in current_packets if packet["id"] != external_packet["id"]
        ]
        validate_context_packets([*candidate_packets, external_packet])
        update["context_packets"] = [external_packet]
    else:
        update["context_packets"] = []
    return update


def route_next_phase(
    state: PipelineState,
    config: RunnableConfig | None = None,
) -> str:
    """Next plan phase whose result is non-terminal; END on abort or plan exhaustion."""
    run_aborted = state.get("run_aborted")
    if run_aborted is not None and type(run_aborted) is not bool:
        raise ValueError("checkpointed run abort flag is invalid")
    if run_aborted is True:
        return END
    results = state.get("phase_results")
    if results is None:
        results = {}
    if type(results) is not dict:
        raise ValueError("checkpointed phase results are invalid")
    plan = state.get("phases_plan")
    if plan is None:
        plan = []
    phase_names = [phase.value for phase in PHASE_ORDER]
    if (
        type(plan) is not list
        or len(plan) > len(phase_names)
        or any(
            type(name) is not str
            or not 1 <= len(name) <= 64
            or "\x00" in name
            or contains_credential_material(name)
            or name not in phase_names
            for name in plan
        )
        or len(set(plan)) != len(plan)
        or plan != [name for name in phase_names if name in set(plan)]
    ):
        raise ValueError("checkpointed phase plan is invalid")
    snapshot = state.get("run_config")
    if snapshot is not None:
        # Conditional-edge routing is itself an authorization boundary. A valid
        # durable config must not be paired with a poisoned plan that enters an
        # unselected provider/model phase.
        effective_config: RunnableConfig
        if config is None:
            if type(snapshot) is not dict:
                raise ValueError("checkpointed pipeline configuration is invalid")
            effective_config = {"configurable": snapshot}
        else:
            effective_config = config
        cfg = PipelineConfigurable.from_state(state, effective_config)
        expected_plan = [phase.value for phase in cfg.selected_phases()]
        if plan != expected_plan:
            raise ValueError(
                "checkpointed pipeline phase plan does not match its durable configuration"
            )
    for name in plan:
        entry = results.get(name)
        if entry is not None and type(entry) is not dict:
            raise ValueError("checkpointed phase result is invalid")
        status = entry.get("status") if entry is not None else None
        if status is not None and (
            type(status) is not str
            or not 1 <= len(status) <= 64
            or status not in _PHASE_STATUS_VALUES
        ):
            raise ValueError("checkpointed phase status is invalid")
        if entry is None or len(entry) == 0 or status not in TERMINAL_PHASE_STATUSES:
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
