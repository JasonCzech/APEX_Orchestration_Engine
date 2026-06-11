"""M0 toy pipeline graph — proves the LangGraph server wiring end to end.

Replaced in M1 by the 7-phase master graph (plan node + conditional-edge router +
phase subgraphs with interrupt() gates).
"""

from langgraph.graph import END, START, StateGraph

from apex.graphs.pipeline.state import PipelineState


def plan_node(state: PipelineState) -> PipelineState:
    request = state.get("request", "")
    return {"plan": f"M0 toy plan for: {request or '(empty request)'}"}


def report_node(state: PipelineState) -> PipelineState:
    return {"summary": f"Completed. {state.get('plan', '')}"}


builder = StateGraph(PipelineState)
builder.add_node("plan", plan_node)
builder.add_node("report", report_node)
builder.add_edge(START, "plan")
builder.add_edge("plan", "report")
builder.add_edge("report", END)

# Compiled without a checkpointer: the LangGraph server injects its own persistence.
graph = builder.compile()
