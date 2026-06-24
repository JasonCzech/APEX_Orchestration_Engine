"""Tests for the 7-phase master pipeline graph: routing, gates, events, attempts.

All runs are driven through InMemorySaver-compiled graphs with Command(resume=...)
cycles against the deterministic M1 stub agents — fast and fully offline.
"""

from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from apex.domain.pipeline import PHASE_ORDER, Phase, PhaseResult, PhaseStatus
from apex.graphs.pipeline.graph import builder, graph

AUTO = {"prompt_review": "auto", "output_review": "auto"}


def compiled() -> CompiledStateGraph[Any, Any, Any, Any]:
    return builder.compile(checkpointer=InMemorySaver())


def config(thread_id: str, **configurable: Any) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, **configurable}}


def all_auto() -> dict[str, dict[str, str]]:
    return {phase.value: dict(AUTO) for phase in PHASE_ORDER}


def pending_interrupt(result: dict[str, Any]) -> dict[str, Any]:
    interrupts = result.get("__interrupt__")
    assert interrupts, f"expected a pending interrupt, got keys {sorted(result)}"
    return interrupts[0].value


def custom_events(
    g: CompiledStateGraph[Any, Any, Any, Any], inputs: dict[str, Any], cfg: RunnableConfig
) -> list[dict[str, Any]]:
    """Collect custom stream events; subgraphs=True surfaces phase-node events."""
    return [
        cast(dict[str, Any], event)
        for _ns, event in g.stream(inputs, cfg, stream_mode="custom", subgraphs=True)
    ]


def subgraph_values(
    g: CompiledStateGraph[Any, Any, Any, Any], cfg: RunnableConfig
) -> dict[str, Any]:
    """State inside the paused phase subgraph's namespaced checkpoint."""
    task_state = g.get_state(cfg, subgraphs=True).tasks[0].state
    assert isinstance(task_state, StateSnapshot)
    return task_state.values


def test_module_level_graph_compiles_without_checkpointer() -> None:
    assert graph.name == "pipeline"
    assert graph.checkpointer is None  # the server injects persistence


def test_all_auto_runs_all_seven_phases() -> None:
    g = compiled()
    cfg = config("t1", gates=all_auto())
    result = g.invoke({"title": "Demo", "request": "Load test the checkout flow"}, cfg)
    assert "__interrupt__" not in result
    assert result["phases_plan"] == [phase.value for phase in PHASE_ORDER]
    assert result["run_aborted"] is False
    for phase in PHASE_ORDER:
        entry = result["phase_results"][phase.value]
        assert entry["status"] == "succeeded"
        assert entry["attempt"] == 1
        assert entry["transcript_ref"]["uri"] == f"memory://transcripts/t1/{phase.value}/attempt-1"
        assert entry["transcript_ref"]["id"] in entry["artifact_ids"]
        assert entry["duration_s"] is not None
        assert entry["resolved_prompt_source"]["origin"] == "catalog"
        if phase is Phase.EXECUTION:
            # M3: execution runs the real engine spine (sim engine), not the stub agent.
            assert "Engine run" in entry["summary"]
            assert entry["test_summary"]["passed"] is True
            assert len(entry["artifact_ids"]) == 2  # transcript + engine results
        else:
            assert "Load test the checkout flow" in entry["summary"]
            assert [t["tool"] for t in entry["tool_calls"]] == [f"{phase.value}.stub_lookup"]
    assert len(result["artifacts"]) == len(PHASE_ORDER) + 1  # + engine results artifact


def test_gated_run_pauses_for_prompt_review_then_approve() -> None:
    g = compiled()
    cfg = config("t2")  # default gate policy: everything gated
    result = g.invoke({"title": "Demo", "request": "r"}, cfg)
    payload = pending_interrupt(result)
    assert payload["kind"] == "prompt_review"
    assert payload["phase"] == "story_analysis"
    assert payload["actions"] == ["approve", "modify", "skip_phase", "abort"]
    assert payload["editable"] is True
    assert payload["prompt"]["source"]["origin"] == "catalog"
    assert payload["tools"] == ["story_analysis.stub_lookup"]
    status = subgraph_values(g, cfg)["phase_results"]["story_analysis"]["status"]
    assert status == PhaseStatus.AWAITING_PROMPT_REVIEW

    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    payload = pending_interrupt(result)
    assert payload["kind"] == "phase_review"
    assert payload["phase"] == "story_analysis"
    entry = subgraph_values(g, cfg)["phase_results"]["story_analysis"]
    assert entry["status"] == PhaseStatus.AWAITING_OUTPUT_REVIEW
    approvals = [(a["gate"], a["action"], a["actor"]) for a in entry["approvals"]]
    assert approvals == [("prompt_review", "approve", "unknown")]


def test_modify_reinterrupts_with_edited_prompt_then_approve() -> None:
    g = compiled()
    cfg = config("t3", phases=["story_analysis"])
    g.invoke({"title": "Demo"}, cfg)
    result = g.invoke(
        Command(resume={"action": "modify", "prompt": {"system": "You are edited."}}), cfg
    )
    payload = pending_interrupt(result)
    assert payload["kind"] == "prompt_review"
    assert payload["prompt"]["system"] == "You are edited."
    assert payload["prompt"]["source"]["origin"] == "gate_edit"
    assert payload["prompt"]["user"].startswith("Title: Demo")  # system-only edit

    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    assert pending_interrupt(result)["kind"] == "phase_review"
    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    entry = result["phase_results"]["story_analysis"]
    assert entry["status"] == "succeeded"
    assert entry["resolved_prompt"]["system"] == "You are edited."
    assert entry["resolved_prompt_source"]["origin"] == "gate_edit"


def test_skip_phase_blocks_downstream_prerequisite() -> None:
    g = compiled()
    gates = all_auto()
    gates["story_analysis"] = {"prompt_review": "gated", "output_review": "auto"}
    cfg = config("t4", gates=gates, phases=["story_analysis", "test_planning"])
    g.invoke({"title": "Demo"}, cfg)
    result = g.invoke(Command(resume={"action": "skip_phase"}), cfg)
    assert "__interrupt__" not in result
    story = result["phase_results"]["story_analysis"]
    assert story["status"] == "skipped"
    assert story["tool_calls"] == []  # agent never ran
    assert [(a["gate"], a["action"]) for a in story["approvals"]] == [
        ("prompt_review", "skip_phase")
    ]
    test_planning = result["phase_results"]["test_planning"]
    assert test_planning["status"] == "failed"
    assert test_planning["tool_calls"] == []
    assert "prerequisite 'story_analysis'" in test_planning["errors"][0]


def test_revise_loops_back_to_agent_and_caps() -> None:
    g = compiled()
    gates = {"story_analysis": {"prompt_review": "auto", "output_review": "gated"}}
    cfg = config("t5", gates=gates, phases=["story_analysis"], limits={"max_revise_loops": 1})
    result = g.invoke({"title": "Demo"}, cfg)
    assert pending_interrupt(result)["kind"] == "phase_review"

    result = g.invoke(
        Command(resume={"action": "revise", "instructions": "add latency numbers"}), cfg
    )
    payload = pending_interrupt(result)
    assert payload["kind"] == "phase_review"
    assert "revised per: add latency numbers" in payload["summary"]

    # cap reached: the next revise proceeds as approve with a warning
    result = g.invoke(Command(resume={"action": "revise", "instructions": "more"}), cfg)
    assert "__interrupt__" not in result
    entry = result["phase_results"]["story_analysis"]
    assert entry["status"] == "succeeded"
    assert any("max_revise_loops" in warning for warning in entry["warnings"])
    assert len(entry["tool_calls"]) == 2  # one stub lookup per agent round


def test_discuss_appends_dialogue_and_reinterrupts() -> None:
    g = compiled()
    gates = {"story_analysis": {"prompt_review": "auto", "output_review": "gated"}}
    cfg = config("t6", gates=gates, phases=["story_analysis"])
    g.invoke({"title": "Demo"}, cfg)
    result = g.invoke(Command(resume={"action": "discuss", "message": "why this scope?"}), cfg)
    payload = pending_interrupt(result)
    assert payload["kind"] == "phase_review"
    tail = payload["dialogue_tail"]
    assert [entry["role"] for entry in tail] == ["operator", "agent"]
    assert tail[0]["content"] == "why this scope?"
    assert "why this scope?" in tail[1]["content"]

    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    assert "__interrupt__" not in result
    dialogue = [d for d in result["dialogue"] if d["phase"] == "story_analysis"]
    assert [d["role"] for d in dialogue] == ["operator", "agent"]
    assert result["phase_results"]["story_analysis"]["status"] == "succeeded"


def test_abort_ends_run_and_leaves_downstream_pending() -> None:
    g = compiled()
    cfg = config("t7")  # default: all phases gated; abort at the first prompt gate
    g.invoke({"title": "Demo"}, cfg)
    result = g.invoke(Command(resume={"action": "abort"}), cfg)
    assert "__interrupt__" not in result
    assert result["run_aborted"] is True
    assert result["phase_results"]["story_analysis"]["status"] == "aborted"
    for phase in PHASE_ORDER[1:]:
        assert result["phase_results"][phase.value]["status"] == "pending"


def test_subset_run_uses_thread_state_and_increments_attempt() -> None:
    g = compiled()
    cfg = config("t8", gates=all_auto(), phases=["test_planning"])
    seeded = PhaseResult(
        phase=Phase.STORY_ANALYSIS, status=PhaseStatus.SUCCEEDED, attempt=1
    ).as_state()
    result = g.invoke({"title": "Demo", "phase_results": {"story_analysis": seeded}}, cfg)
    assert result["phases_plan"] == ["test_planning"]
    assert result["phase_results"]["test_planning"]["status"] == "succeeded"
    assert result["phase_results"]["test_planning"]["attempt"] == 1
    # upstream entry untouched: same attempt, no stub activity
    assert result["phase_results"]["story_analysis"]["attempt"] == 1
    assert result["phase_results"]["story_analysis"]["tool_calls"] == []

    # re-running a terminal phase on the same thread bumps the attempt (wholesale replace)
    result = g.invoke({"title": "Demo"}, cfg)
    entry = result["phase_results"]["test_planning"]
    assert entry["attempt"] == 2
    assert entry["status"] == "succeeded"
    assert [t["id"] for t in entry["tool_calls"]] == ["test_planning-a2-r0-stub-lookup"]
    assert entry["transcript_ref"]["uri"] == "memory://transcripts/t8/test_planning/attempt-2"


def test_prereq_violation_raises_value_error() -> None:
    g = compiled()
    cfg = config("t9", phases=["reporting"])
    with pytest.raises(ValueError, match="execution"):
        g.invoke({"title": "Demo"}, cfg)


def test_empty_phase_plan_raises_value_error() -> None:
    g = compiled()
    cfg = config("t-empty", phases=[])
    with pytest.raises(ValueError, match="phase plan is empty"):
        g.invoke({"title": "Demo"}, cfg)


def test_inverted_phase_range_raises_value_error() -> None:
    g = compiled()
    cfg = config("t-inverted", start_phase="execution", stop_after="story_analysis")
    with pytest.raises(ValueError, match="phase plan is empty"):
        g.invoke({"title": "Demo"}, cfg)


def test_custom_events_streamed() -> None:
    g = compiled()
    cfg = config("t10", gates=all_auto(), phases=["story_analysis"])
    events = custom_events(g, {"title": "Demo"}, cfg)
    types = [event["type"] for event in events]
    assert types[0] == "plan_resolved"
    assert events[0]["phases"] == ["story_analysis"]
    assert "phase_status" in types
    assert "tool_call" in types
    assert types[-1] == "phase_status" and events[-1]["status"] == "succeeded"
    assert all(event["schema_version"] == 1 for event in events)

    cfg2 = config("t10b", phases=["story_analysis"])
    events2 = custom_events(g, {"title": "Demo"}, cfg2)
    assert any(
        event["type"] == "gate_opened" and event["gate"] == "prompt_review" for event in events2
    )


def test_unknown_action_reinterrupts_with_error() -> None:
    g = compiled()
    cfg = config("t12", phases=["story_analysis"])
    g.invoke({"title": "Demo"}, cfg)
    result = g.invoke(Command(resume={"action": "bogus"}), cfg)
    payload = pending_interrupt(result)
    assert payload["kind"] == "prompt_review"
    assert "bogus" in payload["error"]
    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    assert pending_interrupt(result)["kind"] == "phase_review"


def test_prompt_override_sets_run_override_origin() -> None:
    g = compiled()
    cfg = config(
        "t13",
        gates=all_auto(),
        phases=["story_analysis"],
        prompt_overrides={"phase/story_analysis": {"content": "Custom system prompt."}},
    )
    result = g.invoke({"title": "Demo"}, cfg)
    entry = result["phase_results"]["story_analysis"]
    assert entry["resolved_prompt"]["system"] == "Custom system prompt."
    assert entry["resolved_prompt_source"]["origin"] == "run_override"
