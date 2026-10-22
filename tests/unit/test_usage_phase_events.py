"""Phase finalize emits one graph-surface usage event per terminal status."""

from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, StateSnapshot

from apex.auth.identity import RUNTIME_ACTOR_ID_KEY
from apex.graphs.pipeline.graph import builder
from apex.services import usage

AUTO = {"prompt_review": "auto", "output_review": "auto"}


def config(thread_id: str, **configurable: Any) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, **configurable}}


@pytest.fixture
def phase_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture (phase, status) pairs from the finalize hook (no DB)."""
    captured: list[tuple[str, str]] = []

    def fake(phase: str, status: str, config: Any, *, attempt: int | None = None) -> None:
        assert attempt is not None
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
            "app_id": "app-x",
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
    assert captured["app_id"] == "app-x"
    assert captured["agent_name"] == "reporting.worker"

    captured.clear()
    usage.record_agent_event_sync(
        phase="reporting", status="succeeded", attempt=1, config=cfg, latency_ms=5
    )
    assert captured["status"] == "ok"  # succeeded -> ok


def test_usage_keeps_invoking_actor_and_time_after_another_user_approves(
    agent_events: list[dict[str, Any]],
) -> None:
    g = builder.compile(checkpointer=InMemorySaver())
    cfg = config(
        "cross-user-review",
        phases=["story_analysis"],
        gates={"story_analysis": {"prompt_review": "auto", "output_review": "gated"}},
        **{RUNTIME_ACTOR_ID_KEY: "alice"},
    )
    result = g.invoke({"title": "Demo", "request": "load test checkout"}, cfg)
    assert "__interrupt__" in result
    phase_state = g.get_state(cfg, subgraphs=True).tasks[0].state
    assert isinstance(phase_state, StateSnapshot)
    invoked_at = phase_state.values["phase_results"]["story_analysis"]["usage_occurred_at"]
    assert agent_events == []
    resumed: RunnableConfig = {
        **cfg,
        "configurable": {**cfg.get("configurable", {}), RUNTIME_ACTOR_ID_KEY: "bob"},
    }
    g.invoke(Command(resume={"action": "approve"}), resumed)
    assert len(agent_events) == 1
    assert agent_events[0]["consumer_id"] == "alice"
    assert agent_events[0]["occurred_at"].isoformat() == invoked_at


def test_legacy_usage_does_not_inherit_current_finalizer_identity(monkeypatch):
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(usage, "record_agent_event", capture)
    usage.record_agent_event_sync(
        phase="reporting",
        status="succeeded",
        attempt=1,
        config=config("legacy", **{RUNTIME_ACTOR_ID_KEY: "bob"}),
        latency_ms=0,
    )
    assert captured["consumer_id"] is None


def test_cross_user_revisions_retain_each_invocation(agent_events, monkeypatch):
    from apex.graphs.pipeline import phase_subgraph

    times = iter([10.0, 10.012, 20.0, 20.025])
    monkeypatch.setattr(phase_subgraph, "perf_counter", lambda: next(times))
    g = builder.compile(checkpointer=InMemorySaver())
    cfg = config(
        "cross-user-revision",
        phases=["story_analysis"],
        gates={"story_analysis": {"prompt_review": "auto", "output_review": "gated"}},
        **{RUNTIME_ACTOR_ID_KEY: "alice"},
    )
    g.invoke({"title": "Demo", "request": "load test checkout"}, cfg)
    bob: RunnableConfig = {
        **cfg,
        "configurable": {**cfg.get("configurable", {}), RUNTIME_ACTOR_ID_KEY: "bob"},
    }
    g.invoke(Command(resume={"action": "revise", "instructions": "Add a checkout case"}), bob)
    g.invoke(Command(resume={"action": "approve"}), bob)
    assert [(e["consumer_id"], e["revision"], e["latency_ms"]) for e in agent_events] == [
        ("alice", 0, 12),
        ("bob", 1, 25),
    ]
