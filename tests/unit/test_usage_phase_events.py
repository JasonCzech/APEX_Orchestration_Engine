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


@pytest.fixture
def agent_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture agent analytics rows from the finalize hook (no DB)."""
    captured: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(usage, "record_agent_event_sync", fake)
    return captured


def test_finalize_emits_terminal_usage_event(
    phase_events: list[tuple[str, str]], agent_events: list[dict[str, Any]]
) -> None:
    g = builder.compile(checkpointer=InMemorySaver())
    cfg = config(
        "t-usage-1",
        phases=["story_analysis"],
        gates={"story_analysis": dict(AUTO)},
        model_by_phase={"story_analysis": "claude-3-5-sonnet-latest"},
    )
    result = g.invoke({"title": "Demo", "request": "load test the checkout flow"}, cfg)
    assert "__interrupt__" not in result
    assert phase_events == [("story_analysis", "succeeded")]
    assert agent_events[0]["phase"] == "story_analysis"
    assert agent_events[0]["status"] == "succeeded"
    assert agent_events[0]["attempt"] == 1
    assert agent_events[0]["agent_name"] == "story_analysis.worker"
    assert isinstance(agent_events[0]["latency_ms"], int)
    assert agent_events[0]["usage"] is None


def test_finalize_emits_one_event_per_selected_phase(
    phase_events: list[tuple[str, str]], agent_events: list[dict[str, Any]]
) -> None:
    g = builder.compile(checkpointer=InMemorySaver())
    cfg = config(
        "t-usage-2",
        phases=["story_analysis", "test_planning"],
        gates={"story_analysis": dict(AUTO), "test_planning": dict(AUTO)},
    )
    g.invoke({"title": "Demo", "request": "two phases"}, cfg)
    assert phase_events == [("story_analysis", "succeeded"), ("test_planning", "succeeded")]
    assert [event["phase"] for event in agent_events] == ["story_analysis", "test_planning"]


def test_record_agent_event_sync_resolves_model_and_maps_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """record_agent_event_sync resolves model from model_by_phase, maps the phase
    status, and forwards thread/project — without a DB (review R7)."""
    captured: dict[str, Any] = {}

    async def fake_record_agent_event(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(usage, "record_agent_event", fake_record_agent_event)

    cfg = {
        "configurable": {
            "thread_id": "t-77",
            "project_id": "proj-x",
            "model_by_phase": {"reporting": "claude-3-5-haiku-latest"},
        }
    }
    usage.record_agent_event_sync(
        phase="reporting", status="failed", attempt=2, config=cfg, latency_ms=12
    )
    assert captured["model"] == "claude-3-5-haiku-latest"
    assert captured["provider"] == "anthropic"
    assert captured["status"] == "error"  # failed -> error
    assert captured["thread_id"] == "t-77"
    assert captured["project_id"] == "proj-x"
    assert captured["agent_name"] == "reporting.worker"

    captured.clear()
    usage.record_agent_event_sync(
        phase="reporting", status="succeeded", attempt=1, config=cfg, latency_ms=5
    )
    assert captured["status"] == "ok"  # succeeded -> ok
