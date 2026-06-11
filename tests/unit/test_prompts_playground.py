"""Playground graph: deterministic render + stub completion, fully offline."""

from apex.graphs.playground.graph import graph


def test_graph_compiles_for_server_registration() -> None:
    assert graph.name == "playground"
    assert graph.checkpointer is None  # the server injects persistence


def test_invoke_renders_prompt_and_stub_completion() -> None:
    result = graph.invoke(
        {
            "prompt": {"system": "You test {service}.", "user": "Try {scenario} now"},
            "sample_input": {"service": "checkout", "scenario": "spike"},
        }
    )
    assert result["rendered_prompt"] == {
        "system": "You test checkout.",
        "user": "Try spike now",
    }
    completion = result["completion"]
    assert "stub completion" in completion
    assert "M4" in completion
    assert "echo: Try spike now" in completion


def test_invoke_is_deterministic_and_handles_missing_input() -> None:
    first = graph.invoke({"prompt": {"system": "S", "user": "U"}})
    second = graph.invoke({"prompt": {"system": "S", "user": "U"}})
    assert first == second

    empty = graph.invoke({})
    assert empty["rendered_prompt"] == {"system": "", "user": ""}
    assert "stub completion" in empty["completion"]
