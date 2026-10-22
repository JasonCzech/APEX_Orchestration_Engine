"""Ownership keeps phases in the plan and human work never runs an agent."""

from typing import cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from apex.auth.identity import RUNTIME_ACTOR_ID_KEY
from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.domain.prompt_catalog import PHASE_SECTIONS
from apex.graphs.pipeline import phase_subgraph
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.graph import builder
from apex.graphs.pipeline.state import PipelineState
from apex.services.connections import ConnectionResolver
from apex.services.pipeline_public import public_gate


def configurable(**kwargs):
    return PipelineConfigurable.model_validate(kwargs)


def ownership(owner="hitl"):
    return {"schema_version": 1, "owners": {p.value: owner for p in PHASE_ORDER}}


def deliverable(phase):
    return {
        "schema_version": 1,
        "phase": phase.value,
        "disposition": "complete",
        "summary": "Human completed the work",
        "sections": {key: "Verified evidence and completed work" for key in PHASE_SECTIONS[phase]},
        "evidence": ["https://evidence.example.test/run/1"],
        "assumptions": [],
        "gaps": [],
        "blockers": [],
        "workload": [{"scenario_id": "checkout", "percent": 100.0}]
        if phase == Phase.TEST_PLANNING
        else [],
    }


def test_ownership_validates_full_plan_and_preserves_legacy():
    config = configurable(phase_execution=ownership())
    assert config.selected_phases() == list(PHASE_ORDER)
    assert config.agent_phases() == []
    assert configurable(phases=[Phase.REPORTING]).selected_phases() == [Phase.REPORTING]
    with pytest.raises(ValueError, match="full canonical"):
        configurable(phase_execution=ownership(), phases=[Phase.REPORTING])
    with pytest.raises(ValueError, match="all seven"):
        configurable(phase_execution={"schema_version": 1, "owners": {}})


@pytest.mark.parametrize(
    "engine,tool",
    [("apex_load", "lra_engine"), ("loadrunner", "internal_rca"), ("sim", "perfreportforge")],
)
def test_incompatible_analysis_rejected(engine, tool):
    with pytest.raises(ValueError, match="incompatible"):
        configurable(
            engine=engine,
            analysis={"schema_version": 1, "selections": [{"tool": tool, "connection_id": "c"}]},
        )


def test_analysis_requires_selections_only_for_new_real_workflows():
    configurable(engine="loadrunner")
    with pytest.raises(ValueError, match="at least one"):
        configurable(engine="loadrunner", phase_execution=ownership())
    with pytest.raises(ValueError, match="distinct"):
        configurable(
            engine="loadrunner",
            analysis={
                "selections": [
                    {"tool": "lra_engine", "connection_id": "a"},
                    {"tool": "lra_engine", "connection_id": "b"},
                ]
            },
        )


def test_all_human_run_survives_checkpoints_and_rejects_stale_completion(monkeypatch):
    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", lambda: ConnectionResolver())
    agent_events = []
    monkeypatch.setattr(
        phase_subgraph.usage_events,
        "record_agent_event_sync",
        lambda **event: agent_events.append(event),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Human-owned phase invoked an agent")

    monkeypatch.setattr(phase_subgraph, "_run_stub", forbidden, raising=False)
    saver = InMemorySaver()
    graph = builder.compile(checkpointer=saver)
    config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "human-run",
                "phase_execution": ownership(),
                RUNTIME_ACTOR_ID_KEY: "operator-a",
                "gates": {
                    p.value: {"prompt_review": "auto", "output_review": "auto"} for p in PHASE_ORDER
                },
            }
        },
    )
    result = graph.invoke({"title": "Human workflow", "request": "Complete every phase"}, config)
    for phase in PHASE_ORDER:
        payload = result["__interrupt__"][0].value
        assert payload["kind"] == "human_completion"
        assert payload["phase"] == phase.value
        assert (
            public_gate(
                {
                    "interrupt_id": "gate",
                    "kind": "human_completion",
                    "phase": phase.value,
                    "payload": payload,
                },
                include_payload=True,
            )
            is not None
        )
        output = {"deliverable": deliverable(phase)}
        if phase == Phase.EXECUTION:
            output["external_results"] = {
                "source": "Human run",
                "uri": "https://evidence.example.test/run/1",
                "engine": "sim",
                "kpis": {"p95_ms": 42.0},
            }
        decision = {
            "action": "complete",
            "task_id": payload["task_id"],
            "expected_revision": payload["revision"],
            "output": output,
        }
        if phase == Phase.STORY_ANALYSIS:
            invalid = graph.invoke(Command(resume={**decision, "expected_revision": 999}), config)
            assert "changed" in invalid["__interrupt__"][0].value["error"]
        graph = builder.compile(checkpointer=saver)
        result = graph.invoke(Command(resume=decision), config)
    assert result["phases_plan"] == [p.value for p in PHASE_ORDER]
    for phase in PHASE_ORDER:
        entry = result["phase_results"][phase.value]
        assert entry["status"] == "succeeded"
        assert entry["completed_by"] == "operator-a"
        assert not entry.get("tool_calls")
    assert result["phase_results"]["execution"]["test_summary"]["kpis"]["p95_ms"] == 42
    assert agent_events == []


def test_human_reassignment_preserves_real_agent_invocation_events(monkeypatch):
    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", lambda: ConnectionResolver())
    agent_events = []
    monkeypatch.setattr(
        phase_subgraph.usage_events,
        "record_agent_event_sync",
        lambda **event: agent_events.append(event),
    )
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "reporting": {
                    "attempt": 1,
                    "executor": "hitl",
                    "status": "succeeded",
                    "usage_invocations": [
                        {
                            "status": "failed",
                            "model": "test-model",
                            "usage_consumer_id": "operator-a",
                        }
                    ],
                }
            }
        },
    )
    phase_subgraph._make_finalize(Phase.REPORTING)(
        state, {"configurable": {"thread_id": "reassigned-human-run"}}
    )
    assert len(agent_events) == 1
    assert agent_events[0]["status"] == "failed"
    assert agent_events[0]["model"] == "test-model"
    assert agent_events[0]["consumer_id"] == "operator-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "binding", "empty", "valid"])
async def test_native_completion_requires_authoritative_scoped_measurements(monkeypatch, failure):
    from types import SimpleNamespace

    from apex.graphs.pipeline import analysis_phase, human_phase

    closed = []
    bindings = []

    class Adapter:
        async def validate_analysis_source(self, native_id, **kwargs):
            bindings.append((native_id, kwargs))
            if failure == "missing":
                raise KeyError("private native lookup detail")
            if failure == "binding":
                raise RuntimeError("private scope mismatch")

        def native_console_url(self, native_id):
            return "https://native.example.test/run/1"

        async def native_run_summary(self, native_id):
            kpis = {} if failure == "empty" else {"p95_ms": 31}
            return SimpleNamespace(
                kpis=kpis, model_dump=lambda **kwargs: {"kpis": kpis, "passed": True}
            )

        async def close(self):
            closed.append("adapter")

    class Resolver:
        async def resolve_with_connection_id(self, kind, **kwargs):
            assert kwargs["connection_id"] == "native"
            assert kwargs["project_id"] == "project"
            return Adapter(), "native"

        async def close(self):
            closed.append("resolver")

    monkeypatch.setattr(analysis_phase, "_make_analysis_resolver", Resolver)
    cfg = configurable(
        engine="apex_load",
        project_id="project",
        app_id="app",
        load_test={"release_id": "release", "mapping_revision": "2"},
        analysis={"selections": [{"tool": "internal_rca", "connection_id": "native"}]},
    )
    fields = {
        "native_run_id": "native-1",
        "external": True,
        "source_outcome_explicit": False,
        "test_summary": {"kpis": {"p95_ms": 999}},
    }
    output = {"external_results": {"uri": "https://native.example.test/run/1"}}
    if failure == "valid":
        await human_phase.verify_native_execution(fields, output, cfg)
        assert fields["test_summary"]["kpis"] == {"p95_ms": 31}
        assert fields["native_source_verified"] is True
        from apex.domain.outcomes import qualified_execution_summary

        assert qualified_execution_summary(fields)["passed"] is True
    else:
        with pytest.raises(ValueError) as exc:
            await human_phase.verify_native_execution(fields, output, cfg)
        assert "private" not in str(exc.value)
        assert not fields.get("native_source_verified")
    assert bindings == [
        (
            "native-1",
            {"require_application": True, "release_id": "release", "mapping_revision": "2"},
        )
    ]
    assert closed == ["adapter", "resolver"]
    with pytest.raises(ValueError, match="native APEX Load run ID"):
        await human_phase.verify_native_execution({}, output, cfg)


def test_corrupt_human_task_rejected_before_interrupt(monkeypatch):
    from apex.graphs.pipeline import human_phase

    _, complete = human_phase.human_nodes(Phase.STORY_ANALYSIS)
    monkeypatch.setattr(
        human_phase, "interrupt", lambda payload: pytest.fail("Corrupt task exposed")
    )
    state = {
        "phase_results": {
            "story_analysis": {
                "attempt": 1,
                "human_task": {
                    "phase": "reporting",
                    "revision": 1,
                    "task_id": "story_analysis-a1-human",
                    "sections": [],
                },
            }
        }
    }
    with pytest.raises(ValueError, match="Checkpointed human task"):
        complete(cast(PipelineState, state), {"configurable": {"phase_execution": ownership()}})


def test_human_reporting_requires_exact_selected_tool_evidence():
    from apex.graphs.pipeline.human_phase import validate_completion

    cfg = configurable(
        engine="loadrunner",
        analysis={
            "selections": [
                {"tool": "perfreportforge", "connection_id": "forge"},
                {"tool": "other", "connection_id": "other"},
            ]
        },
    )
    payload = {"phase": "reporting", "task_id": "report", "revision": 1}
    output = {
        "deliverable": deliverable(Phase.REPORTING),
        "analysis_results": {"forge": ["https://evidence.example.test/run/1"]},
    }
    decision = {"task_id": "report", "expected_revision": 1, "output": output}
    with pytest.raises(ValueError, match="exactly"):
        validate_completion(payload, decision, {}, cfg)
    output["analysis_results"]["other"] = ["https://evidence.example.test/run/1"]
    assert (
        validate_completion(payload, decision, {}, cfg)["human_analysis_results"]
        == output["analysis_results"]
    )
    output["analysis_results"]["unselected"] = ["https://evidence.example.test/run/1"]
    with pytest.raises(ValueError, match="exactly"):
        validate_completion(payload, decision, {}, cfg)


def test_human_workload_cannot_override_provider_script_binding():
    from apex.graphs.pipeline.human_phase import validate_completion

    cfg = configurable(environment_target="https://approved.example.test")
    payload = {"phase": "script_scenario", "task_id": "script", "revision": 1}
    decision = {
        "task_id": "script",
        "expected_revision": 1,
        "output": {
            "deliverable": deliverable(Phase.SCRIPT_SCENARIO),
            "load_test_spec": {
                "title": "Human workload",
                "target_environment": "https://unapproved.example.test",
                "vusers": 1,
                "ramp_s": 0,
                "duration_s": 60,
                "script_refs": ["unapproved-script"],
                "slas": {},
            },
        },
    }
    with pytest.raises(ValueError, match="approved provider workload"):
        validate_completion(payload, decision, {}, cfg)
    decision["output"]["load_test_spec"]["script_refs"] = []
    assert (
        validate_completion(payload, decision, {}, cfg)["load_test_spec"]["target_environment"]
        == cfg.environment_target
    )


@pytest.mark.parametrize("human_phase", [None, Phase.ENV_TRIAGE])
def test_full_owned_plan_runs_agent_phases_and_resumes_human_handoff(monkeypatch, human_phase):
    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", lambda: ConnectionResolver())
    owners = ownership("langgraph")
    if human_phase:
        owners["owners"][human_phase.value] = "hitl"
    config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "mixed-run",
                "phase_execution": owners,
                "gates": {
                    p.value: {"prompt_review": "auto", "output_review": "auto"} for p in PHASE_ORDER
                },
            }
        },
    )
    saver = InMemorySaver()
    graph = builder.compile(checkpointer=saver)
    result = graph.invoke({"title": "Owned lifecycle", "request": "Test checkout"}, config)
    if human_phase:
        payload = result["__interrupt__"][0].value
        assert payload["phase"] == human_phase.value
        assert payload["kind"] == "human_completion"
        graph = builder.compile(checkpointer=saver)
        result = graph.invoke(
            Command(
                resume={
                    "action": "complete",
                    "task_id": payload["task_id"],
                    "expected_revision": payload["revision"],
                    "output": {"deliverable": deliverable(human_phase)},
                }
            ),
            config,
        )
    assert result["phases_plan"] == [p.value for p in PHASE_ORDER]
    assert all(result["phase_results"][p.value]["status"] == "succeeded" for p in PHASE_ORDER)
