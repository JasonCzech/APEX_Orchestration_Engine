"""Playground graph: deterministic render + stub completion, fully offline."""

from typing import Any, cast

import pytest

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


def test_graph_rejects_render_amplification_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_render(*args: object, **kwargs: object) -> str:
        raise AssertionError("rendering must happen after expansion preflight")

    monkeypatch.setattr("apex.graphs.playground.graph.render_template", forbidden_render)
    with pytest.raises(ValueError, match="rendered model input"):
        graph.invoke(
            {
                "prompt": {"user": "{value}" * 1_000},
                "sample_input": {"value": "x" * 100},
            }
        )


def test_graph_input_schema_drops_caller_owned_output_fields() -> None:
    result = graph.invoke(
        cast(
            Any,
            {
                "prompt": {"user": "real"},
                "rendered_prompt": {"user": "forged"},
                "completion": "forged",
            },
        )
    )
    assert result["rendered_prompt"]["user"] == "real"
    assert result["completion"] != "forged"
