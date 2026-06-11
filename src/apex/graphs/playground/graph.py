"""Playground graph: render a prompt and produce a deterministic stub completion.

Backs POST /v1/prompts/{id}/test (stateless background runs) and the dashboard
prompt playground. No LLM in M2 — the stub keeps the input/output contract and
the activity feed demoable fully offline; real model wiring lands in M4.

Input:  {"prompt": {"system": str, "user": str}, "sample_input": {...}?}
Output: adds {"rendered_prompt": {"system", "user"}, "completion": str}
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from apex.services.prompts import render_template


class PlaygroundState(TypedDict, total=False):
    prompt: dict[str, Any]
    sample_input: dict[str, Any]
    rendered_prompt: dict[str, str]
    completion: str


def render_prompt(state: PlaygroundState) -> dict[str, Any]:
    """Substitute {placeholders} in the prompt pair from sample_input."""
    prompt = state.get("prompt") or {}
    variables = state.get("sample_input") or {}
    return {
        "rendered_prompt": {
            "system": render_template(str(prompt.get("system") or ""), variables),
            "user": render_template(str(prompt.get("user") or ""), variables),
        }
    }


def stub_completion(state: PlaygroundState) -> dict[str, Any]:
    """Deterministic stand-in for a model call (M4 replaces this node)."""
    rendered = state.get("rendered_prompt") or {}
    system = rendered.get("system", "")
    user = rendered.get("user", "")
    echo = user or system
    completion = (
        "[stub completion — the playground runs without an LLM in M2; "
        "model wiring lands in M4]\n"
        f"system prompt: {len(system)} chars\n"
        f"user prompt: {len(user)} chars\n"
        f"echo: {echo[:200]}"
    )
    return {"completion": completion}


builder = StateGraph(PlaygroundState)
builder.add_node("render_prompt", render_prompt)
builder.add_node("stub_completion", stub_completion)
builder.add_edge(START, "render_prompt")
builder.add_edge("render_prompt", "stub_completion")
builder.add_edge("stub_completion", END)

graph = builder.compile(name="playground")

__all__ = ["PlaygroundState", "builder", "graph"]
