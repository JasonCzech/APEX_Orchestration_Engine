"""Phase finalize emits one graph-surface usage event per terminal status."""

from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from apex.graphs.pipeline.graph import builder
from apex.services import usage

AUTO = {"prompt_review": "auto", "output_review": "auto"}


def config(thread_id: str, **configurable: Any) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, **configurable}}


@pytest.fixture
def phase_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture (phase, status) pairs from the finalize hook (no DB)."""
    captured: list[tuple[str, str]] = []

    def fake(phase: str, status: str, config: Any) -> None:
        captured.append((phase, status))

    monkeypatch.setattr(usage, "record_phase_usage_sync", fake)
    return captured


def test_finalize_emits_terminal_usage_event(phase_events: list[tuple[str, str]]) -> None:
    g = builder.compile(checkpointer=InMemorySaver())
    cfg = config(
        "t-usage-1",
        phases=["story_analysis"],
        gates={"story_analysis": dict(AUTO)},
    )
    result = g.invoke({"title": "Demo", "request": "load test the checkout flow"}, cfg)
    assert "__interrupt__" not in result
    assert phase_events == [("story_analysis", "succeeded")]


def test_finalize_emits_one_event_per_selected_phase(
    phase_events: list[tuple[str, str]],
) -> None:
    g = builder.compile(checkpointer=InMemorySaver())
    cfg = config(
        "t-usage-2",
        phases=["story_analysis", "test_planning"],
        gates={"story_analysis": dict(AUTO), "test_planning": dict(AUTO)},
    )
    g.invoke({"title": "Demo", "request": "two phases"}, cfg)
    assert phase_events == [("story_analysis", "succeeded"), ("test_planning", "succeeded")]
