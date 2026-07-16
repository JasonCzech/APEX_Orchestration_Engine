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
from apex.services.run_validation import (
    PLAYGROUND_RUN_INPUT_KEYS,
    validate_playground_render_budget,
    validate_playground_run_input,
    validate_rendered_model_input,
)


class PlaygroundInput(TypedDict, total=False):
    """Only caller-owned channels may enter a new playground checkpoint."""

    prompt: dict[str, Any]
    sample_input: dict[str, Any]
    project_id: str | None
    app_id: str | None


class PlaygroundState(TypedDict, total=False):
    prompt: dict[str, Any]
    sample_input: dict[str, Any]
    project_id: str | None
    app_id: str | None
    rendered_prompt: dict[str, str]
    completion: str


def render_prompt(state: PlaygroundState) -> dict[str, Any]:
    """Substitute {placeholders} in the prompt pair from sample_input."""
    public_input = {key: state[key] for key in PLAYGROUND_RUN_INPUT_KEYS if key in state}
    validated = validate_playground_run_input(public_input)
    prompt = validated.prompt
    variables = validated.sample_input
    validate_playground_render_budget(prompt, variables)
    system = render_template(prompt.system, variables)
    user = render_template(prompt.user, variables)
    validate_rendered_model_input(system, user)
    return {
        "rendered_prompt": {
            "system": system,
            "user": user,
        }
    }


def stub_completion(state: PlaygroundState) -> dict[str, Any]:
    """Deterministic stand-in for a model call (M4 replaces this node)."""
    # This node can resume independently after an inter-node crash, so treat its
    # checkpointed predecessor output as untrusted rather than assuming the input
    # schema or render node just ran in this process.
    rendered = state.get("rendered_prompt")
    if (
        type(rendered) is not dict
        or set(rendered) != {"system", "user"}
        or type(rendered.get("system")) is not str
        or type(rendered.get("user")) is not str
    ):
        raise ValueError("checkpointed rendered prompt is invalid")
    system = rendered["system"]
    user = rendered["user"]
    validate_rendered_model_input(system, user)
    echo = user or system
    completion = (
        "[stub completion — the playground runs without an LLM in M2; "
        "model wiring lands in M4]\n"
        f"system prompt: {len(system)} chars\n"
        f"user prompt: {len(user)} chars\n"
        f"echo: {echo[:200]}"
    )
    return {"completion": completion}


builder = StateGraph(PlaygroundState, input_schema=PlaygroundInput)
builder.add_node("render_prompt", render_prompt)
builder.add_node("stub_completion", stub_completion)
builder.add_edge(START, "render_prompt")
builder.add_edge("render_prompt", "stub_completion")
builder.add_edge("stub_completion", END)

graph = builder.compile(name="playground")

__all__ = ["PlaygroundInput", "PlaygroundState", "builder", "graph"]
