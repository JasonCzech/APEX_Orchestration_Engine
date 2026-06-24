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
    PhaseResult,
    PhaseStatus,
)
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.phase_subgraph import (
    EVENT_SCHEMA_VERSION,
    emit_event,
    make_phase_subgraph,
)
from apex.graphs.pipeline.state import JsonDict, PipelineState


def plan_resolver(state: PipelineState, config: RunnableConfig) -> JsonDict:
    """Resolve the run plan from config, validate prerequisites, seed phase entries."""
    cfg = PipelineConfigurable.from_config(config)
    selected = cfg.selected_phases()
    if not selected:
        raise ValueError("Pipeline phase plan is empty")
    existing = state.get("phase_results") or {}

    selected_set = set(selected)  # canonical order => membership implies "runs earlier"
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

    plan = [phase.value for phase in selected]
    emit_event({"schema_version": EVENT_SCHEMA_VERSION, "type": "plan_resolved", "phases": plan})
    return {
        "phases_plan": plan,
        "current_phase": None,
        "run_aborted": False,
        "phase_results": seeded,
        # Checkpoint the resolved limits so a later gate-resume can recover them (the run
        # configurable is not retrievable via get_state). See pipeline_read._limits_from_state.
        "limits": cfg.limits.model_dump(mode="json"),
    }


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


builder = StateGraph(PipelineState)
builder.add_node("plan_resolver", plan_resolver)
builder.add_edge(START, "plan_resolver")

_ROUTE_TARGETS = [phase.value for phase in PHASE_ORDER] + [END]
builder.add_conditional_edges("plan_resolver", route_next_phase, _ROUTE_TARGETS)
for _phase in PHASE_ORDER:
    builder.add_node(_phase.value, make_phase_subgraph(_phase))
    builder.add_conditional_edges(_phase.value, route_next_phase, _ROUTE_TARGETS)

# Compiled without a checkpointer: the LangGraph server injects its own persistence.
graph = builder.compile(name="pipeline")
