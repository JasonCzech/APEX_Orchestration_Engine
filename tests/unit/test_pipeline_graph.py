"""Tests for the 7-phase master pipeline graph: routing, gates, events, attempts.

All runs are driven through InMemorySaver-compiled graphs with Command(resume=...)
cycles against the deterministic M1 stub agents — fast and fully offline.
"""

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from apex.adapters.stubs import MemoryArtifactStore
from apex.domain.pipeline import PHASE_ORDER, Phase, PhaseStatus
from apex.graphs.pipeline import phase_subgraph
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.graph import builder, graph, plan_resolver, route_next_phase
from apex.graphs.pipeline.state import PipelineState
from apex.ports.artifact_store import StoredArtifact, transcript_artifact_key
from apex.services import engine_runs
from apex.services.connections import ConnectionResolver

AUTO = {"prompt_review": "auto", "output_review": "auto"}


@pytest.fixture(autouse=True)
def _static_artifact_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    MemoryArtifactStore.clear()
    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", lambda: ConnectionResolver())
    monkeypatch.setattr(engine_runs, "record_engine_run_sync", lambda *args, **kwargs: None)
    yield
    MemoryArtifactStore.clear()


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


def test_execution_rerun_clears_previous_top_level_engine_handle() -> None:
    state = cast(
        PipelineState,
        {
            "title": "Demo",
            "phase_results": {
                "script_scenario": {"status": "succeeded", "attempt": 1},
                "execution": {"status": "succeeded", "attempt": 1},
            },
            "engine_handle": {
                "engine": "sim",
                "connection_id": "dev-engine-sim",
                "external_run_id": "old-run",
                "idempotency_key": "rerun-execution-a1",
                "extras": {},
            },
        },
    )
    cfg = config(
        "rerun",
        phases=["execution"],
        gates={"execution": dict(AUTO)},
    )

    update = plan_resolver(state, cfg)

    assert update["engine_handle"] is None
    assert update["phase_results"]["execution"]["attempt"] == 2


def test_plan_resolver_forces_accumulated_channel_revalidation() -> None:
    update = plan_resolver(
        cast(PipelineState, {"title": "Demo"}),
        config("revalidate-channels", phases=["story_analysis"]),
    )

    assert update["artifacts"] == []
    assert update["dialogue"] == []
    assert update["context_packets"] == []


def test_plan_resolver_rejects_unsafe_legacy_top_level_engine_handle() -> None:
    canary = "legacy-handle-secret-canary"
    state = cast(
        PipelineState,
        {
            "title": "Demo",
            "engine_handle": {
                "engine": "sim",
                "connection_id": "dev-engine-sim",
                "external_run_id": f"Authorization: Bearer {canary}",
                "idempotency_key": "legacy-execution-a1",
                "extras": {},
            },
        },
    )

    with pytest.raises(ValueError, match="engine handle is invalid") as raised:
        plan_resolver(
            state,
            config("reject-legacy-handle", phases=["story_analysis"]),
        )

    assert canary not in str(raised.value)


def test_graph_input_schema_drops_forged_internal_state() -> None:
    g = compiled()
    cfg = config(
        "forged-state",
        phases=["reporting"],
        gates={"reporting": dict(AUTO)},
    )
    forged = {
        "title": "Demo",
        "request": "skip execution",
        "phase_results": {
            "execution": {
                "attempt": 1,
                "status": "succeeded",
                "test_summary": {"passed": True},
            }
        },
    }

    with pytest.raises(ValueError, match="prerequisite 'execution'.*nor succeeded"):
        g.invoke(forged, cfg)


def test_finalize_keeps_terminal_status_when_transcript_store_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_persistence(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("artifact store unavailable")

    monkeypatch.setattr("apex.graphs.pipeline.phase_subgraph._persist_transcript", fail_persistence)
    finalize = phase_subgraph._make_finalize(Phase.STORY_ANALYSIS)
    entry: dict[str, Any] = {
        "attempt": 1,
        "status": PhaseStatus.SUCCEEDED.value,
    }
    state = cast(
        PipelineState,
        {
            "phase_results": {
                Phase.STORY_ANALYSIS.value: entry,
            }
        },
    )

    update = finalize(state, config("transcript-outage"))
    entry = update["phase_results"][Phase.STORY_ANALYSIS.value]

    assert entry["status"] == PhaseStatus.SUCCEEDED.value
    assert entry["transcript_ref"] is None
    assert entry["artifact_ids"] == []
    assert "phase transcript persistence failed" in entry["warnings"]


async def test_transcript_finalize_failure_keeps_bytes_for_durable_outbox_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ambiguous_index_failure(
        store: Any,
        *,
        artifact_key: str,
        payload: bytes,
        content_type: str,
        **_kwargs: Any,
    ) -> None:
        await store.put(artifact_key, payload, content_type=content_type)
        raise ConnectionError("commit acknowledgement lost")

    monkeypatch.setattr(
        phase_subgraph,
        "persist_artifact_with_intent",
        ambiguous_index_failure,
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "status": PhaseStatus.SUCCEEDED.value,
    }
    state = cast(
        PipelineState,
        {
            "phase_results": {
                Phase.STORY_ANALYSIS.value: entry,
            }
        },
    )
    key = transcript_artifact_key("transcript-index-ambiguous", "story_analysis", 1)

    with pytest.raises(ConnectionError, match="acknowledgement lost"):
        await phase_subgraph._persist_transcript(
            state,
            Phase.STORY_ANALYSIS,
            entry,
            config("transcript-index-ambiguous"),
            attempt=1,
            status=PhaseStatus.SUCCEEDED.value,
        )

    assert await MemoryArtifactStore().get(key)


async def test_transcript_resolver_close_failure_does_not_overturn_durable_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexed: list[str] = []

    class CloseFailResolver:
        async def resolve_with_connection_id(self, *_args: Any, **_kwargs: Any) -> tuple[Any, str]:
            return MemoryArtifactStore(), "dev-artifact-store-memory"

        async def close(self) -> None:
            raise RuntimeError("dispose failed after commit")

    async def record_reference(
        store: Any,
        *,
        artifact_key: str,
        payload: bytes,
        content_type: str,
        **_kwargs: Any,
    ) -> StoredArtifact:
        indexed.append(artifact_key)
        return await store.put(artifact_key, payload, content_type=content_type)

    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", CloseFailResolver)
    monkeypatch.setattr(phase_subgraph, "persist_artifact_with_intent", record_reference)
    entry: dict[str, Any] = {
        "attempt": 1,
        "status": PhaseStatus.SUCCEEDED.value,
    }
    state = cast(
        PipelineState,
        {
            "phase_results": {
                Phase.STORY_ANALYSIS.value: entry,
            }
        },
    )
    key = transcript_artifact_key("transcript-close-failure", "story_analysis", 1)

    stored, connection_id = await phase_subgraph._persist_transcript(
        state,
        Phase.STORY_ANALYSIS,
        entry,
        config("transcript-close-failure"),
        attempt=1,
        status=PhaseStatus.SUCCEEDED.value,
    )

    assert stored.key == key
    assert connection_id == "dev-artifact-store-memory"
    assert indexed == [key]
    assert await MemoryArtifactStore().get(key)


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
        transcript_key = transcript_artifact_key("t1", phase.value, 1)
        assert entry["transcript_ref"]["uri"] == f"apex-artifact:///{transcript_key}"
        assert entry["transcript_ref"]["key"] == transcript_key
        assert entry["transcript_ref"]["artifact_connection_id"] == ("dev-artifact-store-memory")
        assert entry["transcript_ref"]["id"] in entry["artifact_ids"]
        transcript = asyncio.run(MemoryArtifactStore().get(transcript_key)).decode()
        assert f'"phase": "{phase.value}"' in transcript
        assert '"status": "succeeded"' in transcript
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


def test_finalize_tolerates_malformed_legacy_start_timestamp() -> None:
    finalize = phase_subgraph._make_finalize(Phase.STORY_ANALYSIS)
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "story_analysis": {
                    "attempt": 1,
                    "status": PhaseStatus.RUNNING.value,
                    "started_at": "not-a-timestamp",
                    "summary": "completed output",
                }
            }
        },
    )

    update = finalize(state, config("legacy-started-at"))
    entry = update["phase_results"]["story_analysis"]

    assert entry["status"] == PhaseStatus.SUCCEEDED.value
    assert entry["duration_s"] is None
    assert entry["ended_at"]


def test_script_scenario_preserves_explicit_zero_ramp() -> None:
    state = cast(PipelineState, {"title": "Immediate ramp"})
    cfg = config("zero-ramp", load_test={"ramp_s": 0})

    spec = phase_subgraph._script_scenario_load_test_spec(state, cfg, 1)

    assert spec["ramp_s"] == 0


def test_script_scenario_rejects_credential_bearing_checkpointed_title() -> None:
    canary = "checkpointed-title-secret-canary"
    state = cast(PipelineState, {"title": f"api_key={canary}"})

    with pytest.raises(ValueError, match="credential material") as raised:
        phase_subgraph._script_scenario_load_test_spec(
            state,
            config("unsafe-title"),
            1,
        )

    assert canary not in str(raised.value)


def test_finalize_fails_closed_for_unknown_checkpointed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_persistence(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("transcript unavailable")

    monkeypatch.setattr(phase_subgraph, "_persist_transcript", fail_persistence)
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "story_analysis": {
                    "attempt": 1,
                    "status": "unknown-terminal-state",
                }
            }
        },
    )

    update = phase_subgraph._make_finalize(Phase.STORY_ANALYSIS)(
        state,
        config("invalid-terminal-status"),
    )
    entry = update["phase_results"]["story_analysis"]

    assert entry["status"] == PhaseStatus.FAILED.value
    assert "checkpointed phase terminal transition was invalid" in entry["errors"]


def test_execution_finalize_rejects_malformed_collection_witness_and_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_persistence(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("transcript unavailable")

    monkeypatch.setattr(phase_subgraph, "_persist_transcript", fail_persistence)
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "status": PhaseStatus.RUNNING.value,
                    "engine_collection_settled": "true",
                    "engine_collection_final_status": PhaseStatus.SUCCEEDED.value,
                    "engine_collection_next": "finalize",
                }
            }
        },
    )

    update = phase_subgraph._make_finalize(Phase.EXECUTION)(
        state,
        config("invalid-collection-witness"),
    )
    entry = update["phase_results"]["execution"]

    assert entry["status"] == PhaseStatus.FAILED.value
    assert entry["engine_collection_settled"] is False
    assert entry["engine_collection_final_status"] is None
    assert entry["engine_collection_next"] is None


def test_finalize_validates_diagnostics_before_observational_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def persistence(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("transcript")
        raise AssertionError("invalid diagnostics must be rejected first")

    monkeypatch.setattr(phase_subgraph, "_persist_transcript", persistence)
    monkeypatch.setattr(
        phase_subgraph.usage_events,
        "record_phase_usage_sync",
        lambda *_args, **_kwargs: calls.append("usage"),
    )
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "story_analysis": {
                    "attempt": 1,
                    "status": PhaseStatus.RUNNING.value,
                    "warnings": "not-a-list",
                }
            }
        },
    )

    with pytest.raises(ValueError, match="phase warnings are invalid"):
        phase_subgraph._make_finalize(Phase.STORY_ANALYSIS)(
            state,
            config("invalid-finalize-diagnostics"),
        )

    assert calls == []


def test_router_rejects_plan_that_differs_from_durable_selection() -> None:
    durable = PipelineConfigurable(phases=[Phase.STORY_ANALYSIS])
    state = cast(
        PipelineState,
        {
            "run_config": durable.snapshot(),
            "phases_plan": [Phase.EXECUTION.value],
            "phase_results": {},
        },
    )

    with pytest.raises(ValueError, match="phase plan does not match"):
        route_next_phase(
            state,
            config("poisoned-phase-plan", phases=[Phase.STORY_ANALYSIS.value]),
        )


async def test_transcript_resource_closes_survive_repeated_cancellation() -> None:
    started: set[str] = set()
    both_started = asyncio.Event()
    release = asyncio.Event()
    closed: set[str] = set()

    async def close(name: str) -> None:
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        closed.add(name)

    class Store:
        async def aclose(self) -> None:
            await close("store")

    class Resolver:
        async def close(self) -> None:
            await close("resolver")

    task = asyncio.create_task(
        phase_subgraph._close_transcript_resources_definitively(Store(), Resolver())
    )
    await both_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == {"store", "resolver"}


def test_finalize_rejects_live_config_drift_before_best_effort_persistence() -> None:
    finalize = phase_subgraph._make_finalize(Phase.STORY_ANALYSIS)
    durable = PipelineConfigurable(project_id="project-a", app_id="app-a")
    state = cast(
        PipelineState,
        {
            "run_config": durable.snapshot(),
            "phase_results": {
                "story_analysis": {
                    "attempt": 1,
                    "status": PhaseStatus.RUNNING.value,
                }
            },
        },
    )

    with pytest.raises(ValueError, match="does not match its durable checkpoint"):
        finalize(
            state,
            config("finalize-config-drift", project_id="project-b", app_id="app-b"),
        )


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
        Command(
            resume={
                "action": "modify",
                "prompt": {"system": "You are edited."},
                "note": "Operator context.",
            }
        ),
        cfg,
    )
    payload = pending_interrupt(result)
    assert payload["kind"] == "prompt_review"
    assert payload["prompt"]["system"] == "You are edited."
    assert payload["prompt"]["source"]["origin"] == "gate_edit"
    assert payload["prompt"]["user"].startswith("Title: Demo")  # system-only edit
    assert payload["additional_context"] == "Operator context."

    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    assert pending_interrupt(result)["kind"] == "phase_review"
    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    entry = result["phase_results"]["story_analysis"]
    assert entry["status"] == "succeeded"
    assert entry["resolved_prompt"]["system"] == "You are edited."
    assert "Operator context." in entry["resolved_prompt"]["user"]
    assert entry["resolved_prompt_source"]["origin"] == "gate_edit"
    assert result["prompt_reviews"]["story_analysis"]["system"] == "You are edited."
    assert result["prompt_reviews"]["story_analysis"]["additional_context"] == "Operator context."


def test_prompt_review_approve_with_context_marks_gate_edit() -> None:
    g = compiled()
    cfg = config("t3-context", phases=["story_analysis"])
    g.invoke({"title": "Demo"}, cfg)

    result = g.invoke(Command(resume={"action": "approve", "note": "Operator context."}), cfg)
    assert pending_interrupt(result)["kind"] == "phase_review"
    result = g.invoke(Command(resume={"action": "approve"}), cfg)

    entry = result["phase_results"]["story_analysis"]
    assert entry["resolved_prompt_source"]["origin"] == "gate_edit"
    assert "Operator context." in entry["resolved_prompt"]["user"]
    assert result["prompt_reviews"]["story_analysis"]["additional_context"] == "Operator context."


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

    # Cap reached: the gate fails closed and requires an explicit decision.
    result = g.invoke(Command(resume={"action": "revise", "instructions": "more"}), cfg)
    payload = pending_interrupt(result)
    assert "choose approve or abort explicitly" in payload["error"]

    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    assert "__interrupt__" not in result
    entry = result["phase_results"]["story_analysis"]
    assert entry["status"] == "succeeded"
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


def test_max_length_discussion_remains_checkpointable() -> None:
    g = compiled()
    gates = {"story_analysis": {"prompt_review": "auto", "output_review": "gated"}}
    cfg = config("dialogue-max", gates=gates, phases=["story_analysis"])
    g.invoke({"title": "Demo"}, cfg)
    message = "m" * 20_000

    result = g.invoke(Command(resume={"action": "discuss", "message": message}), cfg)

    tail = pending_interrupt(result)["dialogue_tail"]
    assert tail[0]["content"] == message
    assert tail[1]["content"].endswith(message)
    assert len(tail[1]["content"]) <= 50_000


def test_new_attempt_gets_fresh_dialogue_budget_and_prunes_old_attempt() -> None:
    g = compiled()
    gates = {"story_analysis": {"prompt_review": "auto", "output_review": "gated"}}
    cfg = config(
        "dialogue-rerun",
        gates=gates,
        phases=["story_analysis"],
        limits={"max_dialogue_turns": 1},
    )
    g.invoke({"title": "Demo"}, cfg)
    first = g.invoke(Command(resume={"action": "discuss", "message": "attempt one"}), cfg)
    assert pending_interrupt(first).get("error") is None
    g.invoke(Command(resume={"action": "approve"}), cfg)

    second = g.invoke({"title": "Demo"}, cfg)
    assert pending_interrupt(second)["kind"] == "phase_review"
    assert subgraph_values(g, cfg)["phase_results"]["story_analysis"]["attempt"] == 2
    second = g.invoke(Command(resume={"action": "discuss", "message": "attempt two"}), cfg)
    payload = pending_interrupt(second)

    assert payload.get("error") is None
    assert [
        entry["content"] for entry in payload["dialogue_tail"] if entry["role"] == "operator"
    ] == ["attempt two"]
    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    dialogue = result["dialogue"]
    assert {entry["attempt"] for entry in dialogue} == {2}


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
    seed_cfg = config("t8", gates=all_auto(), phases=["story_analysis"])
    seeded_result = g.invoke({"title": "Demo"}, seed_cfg)
    seeded = seeded_result["phase_results"]["story_analysis"]
    assert seeded["status"] == "succeeded"
    assert seeded["attempt"] == 1
    seeded_tool_calls = seeded["tool_calls"]

    cfg = config("t8", gates=all_auto(), phases=["test_planning"])
    result = g.invoke({"title": "Demo"}, cfg)
    assert result["phases_plan"] == ["test_planning"]
    assert result["phase_results"]["test_planning"]["status"] == "succeeded"
    assert result["phase_results"]["test_planning"]["attempt"] == 1
    # Upstream entry is preserved without another agent invocation.
    assert result["phase_results"]["story_analysis"]["attempt"] == 1
    assert result["phase_results"]["story_analysis"]["tool_calls"] == seeded_tool_calls

    # re-running a terminal phase on the same thread bumps the attempt (wholesale replace)
    result = g.invoke({"title": "Demo"}, cfg)
    entry = result["phase_results"]["test_planning"]
    assert entry["attempt"] == 2
    assert entry["status"] == "succeeded"
    assert [t["id"] for t in entry["tool_calls"]] == ["test_planning-a2-r0-stub-lookup"]
    transcript_key = transcript_artifact_key("t8", "test_planning", 2)
    assert entry["transcript_ref"]["uri"] == f"apex-artifact:///{transcript_key}"
    assert entry["transcript_ref"]["key"] == transcript_key


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
    assert payload["error"].startswith("unknown action; expected one of")
    assert "bogus" not in payload["error"]
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


def test_prompt_review_state_drives_prepare_and_preserves_existing_review() -> None:
    g = compiled()
    cfg = config("t14", gates=all_auto(), phases=["story_analysis"])
    g.invoke({"title": "Demo", "request": "r"}, cfg)
    review = {
        "system": "Run scoped system.",
        "phase_prompt": "Run scoped phase prompt.",
        "application": None,
        "additional_context": "Use checkout build 17.",
        "source": {"origin": "run_override", "ref": "manual", "editor": "op"},
        "updated_at": "2026-06-01T00:00:00+00:00",
        "updated_by": "op",
    }
    # Internal state edits are only accepted when attributed to a trusted graph
    # node. Public run input cannot supply prompt_reviews (see forged-state test).
    g.update_state(
        cfg,
        {"prompt_reviews": {"story_analysis": review}},
        as_node="plan_resolver",
    )

    result = g.invoke({"title": "Demo", "request": "r"}, cfg)
    entry = result["phase_results"]["story_analysis"]
    assert entry["resolved_prompt"]["system"] == "Run scoped system."
    assert entry["resolved_prompt"]["user"].startswith("Run scoped phase prompt.")
    assert "Use checkout build 17." in entry["resolved_prompt"]["user"]
    assert entry["resolved_prompt_source"]["origin"] == "run_override"
    assert result["prompt_reviews"]["story_analysis"]["updated_by"] == "op"
    assert len(result["prompt_reviews"]) == len(PHASE_ORDER)


def test_application_review_is_app_wide_across_phases() -> None:
    g = compiled()
    cfg = config(
        "t15",
        gates={
            phase: {"prompt_review": "gated", "output_review": "auto"}
            for phase in ("story_analysis", "test_planning")
        },
        phases=["story_analysis", "test_planning"],
        project_id="p1",
        app_id="a1",
    )
    g.invoke({"title": "Demo", "request": "r"}, cfg)
    modified = g.invoke(
        Command(
            resume={
                "action": "modify",
                "prompt": {"application": "App-wide requirements."},
            }
        ),
        cfg,
    )
    assert pending_interrupt(modified)["prompt"]["application"] == "App-wide requirements."
    next_phase = g.invoke(Command(resume={"action": "approve"}), cfg)
    assert pending_interrupt(next_phase)["phase"] == "test_planning"
    assert pending_interrupt(next_phase)["prompt"]["application"] == "App-wide requirements."
    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    # A single run-scoped override resolves into every phase's application prompt.
    for phase in ("story_analysis", "test_planning"):
        entry = result["phase_results"][phase]
        assert entry["resolved_prompt"]["application"] == "App-wide requirements."


def test_new_run_resets_application_review_before_a_different_app_edit() -> None:
    g = compiled()
    first_cfg = config(
        "application-review-reset",
        gates=all_auto(),
        phases=["story_analysis"],
        project_id="p1",
        app_id="a1",
    )
    g.invoke({"title": "First", "request": "r"}, first_cfg)
    g.update_state(
        first_cfg,
        {
            "application_reviews": {
                "a1": {
                    "content": "First app requirements.",
                    "source": {"origin": "run_override", "editor": "op"},
                    "updated_at": "2026-06-01T00:00:00+00:00",
                    "updated_by": "op",
                }
            }
        },
        as_node="plan_resolver",
    )

    second_cfg = config(
        "application-review-reset",
        gates=all_auto(),
        phases=["story_analysis"],
        project_id="p1",
        app_id="a2",
    )
    result = g.invoke({"title": "Second", "request": "r"}, second_cfg)

    assert result["application_reviews"] == {}
    g.update_state(
        second_cfg,
        {
            "application_reviews": {
                "a2": {
                    "content": "Second app requirements.",
                    "source": {"origin": "run_override", "editor": "op"},
                    "updated_at": "2026-06-02T00:00:00+00:00",
                    "updated_by": "op",
                }
            }
        },
        as_node="plan_resolver",
    )
    assert g.get_state(second_cfg).values["application_reviews"] == {
        "a2": {
            "content": "Second app requirements.",
            "source": {"origin": "run_override", "editor": "op"},
            "updated_at": "2026-06-02T00:00:00+00:00",
            "updated_by": "op",
        }
    }
