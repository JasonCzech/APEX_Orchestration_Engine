"""Replay the serialized run contract from the commit before Cycle dispatch.

The fixture was emitted by PipelineConfigurable at 10c7eee, not by today's
model with a field removed, so future schema changes cannot silently change it.
"""

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, StateSnapshot

from apex.adapters.stubs import MemoryArtifactStore
from apex.domain.pipeline import Phase
from apex.graphs.pipeline import phase_subgraph
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.graph import builder
from apex.services.connections import ConnectionResolver
from apex.services.pipeline_read import RerunConfigurationConflictError, _run_config_from_state
from tests.unit.test_pipelines_resume import (
    FakeClient,
    FakeRuns,
    FakeThreads,
    make_app,
    rerun_state,
    state_with_interrupt,
)


@pytest.fixture
def old_snapshot() -> dict[str, Any]:
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/pipeline-config-10c7eee.json").read_text()
    )


def test_prior_complete_snapshot_migrates_without_mutating_saved_data(old_snapshot):
    original = copy.deepcopy(old_snapshot)
    state = {"values": {"run_config": old_snapshot}}
    migrated = _run_config_from_state(state)
    assert migrated == {
        **original,
        "cycle_dispatch_id": None,
        "prompt_selections": None,
        "prompt_manifest": None,
        "phase_execution": None,
        "analysis": None,
        "test_scope": None,
        "test_scope_snapshot": None,
        "rerun_phases": None,
    }
    assert old_snapshot == original
    assert (
        PipelineConfigurable.from_state(
            {"run_config": old_snapshot}, {"configurable": migrated}
        ).snapshot()
        == migrated
    )
    assert (
        PipelineConfigurable.from_state_for_phase(
            {"run_config": old_snapshot, "phases_plan": ["story_analysis"]},
            {"configurable": old_snapshot},
            Phase.STORY_ANALYSIS,
        ).snapshot()
        == migrated
    )


@pytest.mark.parametrize("boundary", ["facade", "graph"])
@pytest.mark.parametrize("change", ["missing", "unknown", "noncanonical", "binding_drift"])
def test_legacy_migration_never_defaults_other_fields_or_changes_authority(
    old_snapshot, boundary, change
):
    snapshot = copy.deepcopy(old_snapshot)
    current = copy.deepcopy(old_snapshot)
    if change == "missing":
        snapshot.pop("connections")
    elif change == "unknown":
        snapshot["unexpected"] = "value"
    elif change == "noncanonical":
        snapshot["environment_target_version"] = 1.0
        current["environment_target_version"] = 1
    else:
        current["cycle_dispatch_id"] = "new-binding"
        if boundary == "facade":
            # A current Cycle snapshot retains its exact binding; it is never
            # mistaken for the old absent-binding shape or downgraded to None.
            assert (
                _run_config_from_state({"values": {"run_config": current}})["cycle_dispatch_id"]
                == "new-binding"
            )
            return
    with pytest.raises((ValueError, RerunConfigurationConflictError)):
        if boundary == "facade":
            _run_config_from_state({"values": {"run_config": snapshot}})
        else:
            PipelineConfigurable.from_state({"run_config": snapshot}, {"configurable": current})


@pytest.mark.parametrize("operation", ["resume", "rerun", "abort"])
def test_prior_snapshot_http_lifecycle_remains_available(old_snapshot, operation):
    original = copy.deepcopy(old_snapshot)
    state = (
        rerun_state(old_snapshot)
        if operation == "rerun"
        else state_with_interrupt("int-1", run_config=old_snapshot)
    )
    runs = FakeRuns(
        runs=[{"run_id": "active", "status": "running"}] if operation == "abort" else []
    )
    threads = FakeThreads({"t-1": state}, {"t-1": {"project_id": "project-a", "app_id": "app-a"}})
    with TestClient(make_app(FakeClient(threads, runs))) as client:
        if operation == "resume":
            response = client.post(
                "/v1/pipelines/t-1/gates/int-1/resume", json={"action": "approve"}
            )
        elif operation == "rerun":
            response = client.post(
                "/v1/pipelines/t-1/rerun",
                json={
                    "phases": ["story_analysis"],
                    "gates_mode": "auto",
                    "idempotency_key": "upgrade-rerun",
                },
            )
        else:
            response = client.post("/v1/pipelines/t-1/abort")
    assert response.status_code == 202, response.text
    assert old_snapshot == original
    if operation == "abort":
        assert runs.cancelled == ["active"]
    else:
        assert len(runs.create_calls) == 1
        replay = runs.create_calls[0]["config"]["configurable"]
        assert replay["cycle_dispatch_id"] is None
        assert replay["project_id"] == original["project_id"]
        assert replay["connections"] == original["connections"]


def test_paused_graph_replays_prior_parent_and_phase_checkpoints(old_snapshot, monkeypatch):
    MemoryArtifactStore.clear()
    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", lambda: ConnectionResolver())
    saver = InMemorySaver()
    graph = builder.compile(checkpointer=saver)
    config = cast(RunnableConfig, {"configurable": {**old_snapshot, "thread_id": "legacy-run"}})
    first = graph.invoke({"title": "Before upgrade", "request": "Review the request"}, config)
    assert first["__interrupt__"][0].value["kind"] == "prompt_review"
    paused = graph.get_state(config, subgraphs=True)
    child = paused.tasks[0].state
    assert isinstance(child, StateSnapshot)
    # Restore both checkpoint namespaces exactly as the previous release wrote
    # them. Replaying the paused node must not re-enter plan_resolver first.
    for checkpoint_config in (paused.config, child.config):
        record = saver.get_tuple(checkpoint_config)
        assert record is not None
        checkpoint = copy.deepcopy(record.checkpoint)
        checkpoint["channel_values"]["run_config"] = copy.deepcopy(old_snapshot)
        # Re-serialize the old release's field shape without update_state,
        # which would create a new task and discard the pending phase task.
        saver.put(
            record.parent_config or record.config,
            checkpoint,
            record.metadata,
            checkpoint["channel_versions"],
        )
    stored = graph.get_state(config, subgraphs=True)
    saved = stored.tasks[0].state
    assert isinstance(saved, StateSnapshot)
    assert "cycle_dispatch_id" not in saved.values["run_config"]
    reviewed = graph.invoke(Command(resume={"action": "approve"}), config)
    assert reviewed["__interrupt__"][0].value["kind"] == "phase_review"
    final = graph.invoke(Command(resume={"action": "approve"}), config)
    assert final["phase_results"]["story_analysis"]["status"] == "succeeded"
    assert final["phase_results"]["story_analysis"]["approvals"][-1]["action"] == "approve"
    assert graph.get_state(saved.config).values["run_config"] == old_snapshot
    assert "cycle_dispatch_id" not in old_snapshot
    MemoryArtifactStore.clear()
