"""Report assembly consumes complete, run-bound analysis artifacts."""

import hashlib
import json
from typing import cast

import pytest
from langchain_core.runnables import RunnableConfig

from apex.adapters.registry import PortKind
from apex.auth.identity import RUNTIME_ACTOR_ID_KEY
from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.domain.prompt_catalog import PHASE_SECTIONS
from apex.graphs.pipeline import analysis_phase, human_phase
from apex.graphs.pipeline import phase_subgraph as module
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.state import PipelineState, merge_phase_results


def fixture(monkeypatch):
    config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "report-evidence",
                "project_id": "project-1",
                "engine": "loadrunner",
                "phases": ["reporting"],
                "connections": {"artifact_store": "new-default-store"},
                "analysis": {"selections": [{"tool": "perfreportforge", "connection_id": "forge"}]},
            }
        },
    )
    result = {
        "schema_version": 1,
        "summary": "Tool assessment",
        "tool": "perfreportforge",
        "tool_version": "1",
        "input_digest": "a" * 64,
        "findings": [
            {"detail": "Early supporting evidence " * 1000},
            {"detail": "LATE CONFLICT: archive measurements contradict the initial hypothesis"},
        ],
        "artifacts": [{"ref": "https://evidence.example.test/assessment"}],
        "warnings": ["LATE WARNING: noisy neighbor not ruled out"],
        "gaps": ["LATE GAP: database traces unavailable"],
    }
    content = json.dumps(result).encode()
    key = "b" * 64
    row = {
        "tool": "perfreportforge",
        "connection_id": "forge",
        "status": "succeeded",
        "input_digest": result["input_digest"],
        "idempotency_key": key,
        "result_artifact_id": f"analysis-{key}",
        "result_content_sha256": hashlib.sha256(content).hexdigest(),
        "result_preview": json.dumps(result)[0:2000],
    }
    entry = {
        "attempt": 1,
        "analysis_ledger": [row],
        "artifact_ids": [row["result_artifact_id"]],
    }
    artifact = {
        "id": row["result_artifact_id"],
        "kind": "analysis",
        "name": "PerfReportForge results",
        "key": f"analysis/report-evidence/{key}.json",
        "uri": f"apex-artifact:///analysis/report-evidence/{key}.json",
        "artifact_connection_id": "original-store",
        "media_type": "application/json",
    }
    state = cast(PipelineState, {"phase_results": {"reporting": entry}, "artifacts": [artifact]})
    reads = []
    closed = []
    data = {"content": content}

    class Store:
        async def get(self, artifact_key):
            reads.append(artifact_key)
            return data["content"]

        async def close(self):
            closed.append("store")

    class Resolver:
        async def resolve_with_connection_id(self, kind, **kwargs):
            assert kind is PortKind.ARTIFACT_STORE
            assert kwargs == {"connection_id": "original-store", "project_id": "project-1"}
            return Store(), "original-store"

        async def close(self):
            closed.append("resolver")

    monkeypatch.setattr(module, "_make_artifact_resolver", Resolver)
    return state, entry, config, row, artifact, data, reads, closed


def test_report_model_receives_late_findings_warnings_and_gaps(monkeypatch):
    import langchain_anthropic

    state, entry, config, _row, artifact, _data, reads, closed = fixture(monkeypatch)
    prompts = []
    monkeypatch.setattr(
        module, "_review_source_for_phase", lambda *_: ({}, {"user": "Assemble the report"})
    )

    class Model:
        def __init__(self, **_kwargs):
            pass

        def invoke(self, messages):
            prompts.append(messages[1].content)
            raise RuntimeError("test captured model input")

    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", Model)
    cfg = PipelineConfigurable.from_config(config)
    with pytest.raises(RuntimeError, match="test captured model input"):
        module._llm_agent_body(Phase.REPORTING, state, config, cfg, entry, 1)
    prompt = prompts[0]
    for text in ("LATE CONFLICT", "LATE WARNING", "LATE GAP", "tool_version"):
        assert text in prompt
    assert "Consult result artifacts" not in prompt
    assert reads == [artifact["key"]]
    assert closed == ["store", "resolver"]


def test_oversize_complete_report_evidence_fails_before_model_creation(monkeypatch):
    import langchain_anthropic

    state, entry, config, *_ = fixture(monkeypatch)
    settings = module.get_settings().model_copy(deep=True)
    settings.runs.max_model_input_chars = 10_000
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        module, "_review_source_for_phase", lambda *_: ({}, {"user": "Assemble the report"})
    )
    monkeypatch.setattr(
        langchain_anthropic,
        "ChatAnthropic",
        lambda **_: pytest.fail("Incomplete evidence must not reach the model"),
    )
    with pytest.raises(ValueError, match="rendered model input exceeds"):
        module._llm_agent_body(
            Phase.REPORTING, state, config, PipelineConfigurable.from_config(config), entry, 1
        )


@pytest.mark.parametrize(
    "problem",
    ["pending", "missing_ref", "wrong_run", "changed_bytes", "wrong_tool", "wrong_evidence"],
)
async def test_report_artifact_validation_fails_closed(monkeypatch, problem):
    state, entry, config, row, artifact, data, reads, closed = fixture(monkeypatch)
    if problem == "pending":
        row["status"] = "running"
    elif problem == "missing_ref":
        entry["artifact_ids"] = []
    elif problem == "wrong_run":
        artifact["key"] = artifact["key"].replace("report-evidence", "another-run")
    elif problem == "changed_bytes":
        data["content"] += b" "
    else:
        result = json.loads(data["content"])
        result["tool" if problem == "wrong_tool" else "input_digest"] = (
            "lra_engine" if problem == "wrong_tool" else "c" * 64
        )
        data["content"] = json.dumps(result).encode()
        # Old checkpoints lack a content hash; still enforce tool/evidence identity.
        row.pop("result_content_sha256")
    with pytest.raises(ValueError):
        await module._load_analysis_report_evidence(state, entry, config)
    assert bool(reads) is (problem in {"changed_bytes", "wrong_tool", "wrong_evidence"})
    assert sorted(closed) == (["resolver", "store"] if reads else ["resolver"])


@pytest.mark.parametrize("has_analysis", [False, True])
@pytest.mark.parametrize("failure", ["llm", "input_budget"])
def test_failed_report_can_be_reassigned_and_completed_by_authenticated_human(
    monkeypatch, has_analysis, failure
):
    state, entry, config, row, artifact, *_ = fixture(monkeypatch)
    saved_row = dict(row)
    configurable = config.get("configurable", {})
    configurable.update(agent_backend="anthropic", **{RUNTIME_ACTOR_ID_KEY: "operator-a"})
    if not has_analysis:
        configurable.pop("analysis")
        configurable.pop("phases")
        configurable["engine"] = "sim"
        configurable["phase_execution"] = {
            "schema_version": 1,
            "owners": {phase.value: "langgraph" for phase in PHASE_ORDER},
        }
        entry.pop("analysis_ledger")
    settings = module.get_settings().model_copy(deep=True)
    settings.llm.anthropic_api_key = "configured-test-key"
    settings.runs.max_model_input_chars = 10_000
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        module, "_review_source_for_phase", lambda *_: ({}, {"user": "report request " * 1000})
    )
    if failure == "llm":

        def fail(*_args):
            raise RuntimeError("Report model is temporarily unavailable")

        monkeypatch.setattr(module, "_llm_agent_body", fail)
    update = module._make_agent(Phase.REPORTING)(state, config)
    state["phase_results"] = merge_phase_results(
        state.get("phase_results", {}), update["phase_results"]
    )
    assert state["phase_results"]["reporting"]["status"] == "running"
    assert state["phase_results"]["reporting"]["report_failure"] is True
    assert state["phase_results"]["reporting"]["usage_invocations"][0]["status"] == "failed"
    assert module._route_report_output(state, config) == "analysis_recovery"

    decisions = iter(
        [
            {"action": "assign_hitl", "actor": "forged", "note": "Use manual assessment"},
            {"action": "assign_hitl", "note": "Use manual assessment"},
        ]
    )
    payloads = []

    def resume(payload):
        payloads.append(dict(payload))
        return next(decisions)

    monkeypatch.setattr(analysis_phase, "interrupt", resume)
    reassigned = analysis_phase.analysis_recovery(state, config)
    assert reassigned.goto == "open_human_task"
    assert isinstance(reassigned.update, dict)
    assert "unsupported fields" in payloads[-1]["error"]
    override = reassigned.update["phase_results"]["reporting"]["ownership_override"]
    assert override["actor"] == "operator-a"
    assert override["reason"] == "Use manual assessment"
    assert override["at"]
    state["phase_results"] = merge_phase_results(
        state["phase_results"], reassigned.update["phase_results"]
    )
    open_task, complete = human_phase.human_nodes(Phase.REPORTING)
    opened = open_task(state, config)
    state["phase_results"] = merge_phase_results(state["phase_results"], opened["phase_results"])
    task = state["phase_results"]["reporting"]["human_task"]
    output = {
        "deliverable": {
            "schema_version": 1,
            "phase": "reporting",
            "disposition": "complete",
            "summary": "Human report assembled from preserved analysis results",
            "sections": {
                section: "Verified assessment" for section in PHASE_SECTIONS[Phase.REPORTING]
            },
            "evidence": [],
            "assumptions": [],
            "gaps": [],
            "blockers": [],
            "workload": [],
        },
        "artifact_ids": [artifact["id"]],
    }
    if has_analysis:
        output["analysis_results"] = {"forge": [artifact["id"]]}
        assert state["phase_results"]["reporting"]["analysis_ledger"] == [saved_row]
    monkeypatch.setattr(
        human_phase,
        "interrupt",
        lambda _payload: {
            "action": "complete",
            "task_id": task["task_id"],
            "expected_revision": task["revision"],
            "output": output,
        },
    )
    completed = complete(state, config)
    assert completed.goto == "finalize"
    assert isinstance(completed.update, dict)
    assert completed.update["phase_results"]["reporting"]["completed_by"] == "operator-a"
    assert completed.update["phase_results"]["reporting"]["status"] == "succeeded"


@pytest.mark.parametrize("has_analysis", [False, True])
def test_report_retry_preserves_successful_tools_and_starts_new_worker_revision(
    monkeypatch, has_analysis
):
    state, entry, config, row, *_ = fixture(monkeypatch)
    if not has_analysis:
        config.get("configurable", {}).pop("analysis")
        entry.pop("analysis_ledger")
    entry.update(status="running", report_failure=True, errors=["Report failed"], revise_count=1)
    saved_row = dict(row)
    monkeypatch.setattr(analysis_phase, "interrupt", lambda _: {"action": "retry"})
    command = analysis_phase.analysis_recovery(state, config)
    assert command.goto == "report_agent"
    assert isinstance(command.update, dict)
    assert command.update["phase_results"]["reporting"] == {
        "attempt": 1,
        "status": "running",
        "report_failure": False,
        "revise_count": 2,
    }
    merged = merge_phase_results(state.get("phase_results", {}), command.update["phase_results"])
    assert merged["reporting"]["report_failure"] is False
    assert row == saved_row


def test_legacy_report_failure_keeps_existing_output_review():
    state = cast(
        PipelineState, {"phase_results": {"reporting": {"status": "failed", "attempt": 1}}}
    )
    assert (
        module._route_report_output(state, {"configurable": {"phases": ["reporting"]}})
        == "open_output_gate"
    )
