"""Facade launches freeze the prompt manifest before the run exists.

The in-process SDK transport bypasses LangGraph auth, so the native run-create
hook never prepares the manifest for /v1 launches. These tests pin the facade
path that does: without it every wizard launch fails in the plan resolver.
"""

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException

import apex.routers.pipelines as pipelines
from apex.persistence.repositories.documents import DocumentsRepository
from apex.services.execution_readiness import LaunchReadinessError
from apex.services.pipeline_read import LaunchValidationError, PipelineReadService
from tests.unit.test_pipelines_create_run import (
    AtomicClient,
    FakeCatalogRepo,
    FakeClient,
    FakeDocsRepo,
    _identity,
    _scope_request,
)


@pytest.fixture(autouse=True)
def _fake_projection_writer(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("apex.services.test_scope.persist_test_scope", AsyncMock())


def _ref() -> dict[str, str]:
    return {"prompt_id": uuid4().hex, "version_id": uuid4().hex}


def _selections(*phases: str) -> dict[str, Any]:
    return {"system": _ref(), "phases": {phase: _ref() for phase in phases}}


def _manifest_marker(configurable: dict[str, Any]) -> dict[str, Any]:
    return {"frozen_for": sorted(configurable["prompt_selections"]["phases"])}


async def test_start_run_rejects_prompt_selections_without_a_preparer() -> None:
    client = FakeClient()

    with pytest.raises(LaunchValidationError, match="prepared manifest"):
        await PipelineReadService(client).start_run(
            title="Unprepared",
            phases=["reporting"],
            configurable={"prompt_selections": _selections("reporting")},
        )

    assert client.threads.metadata is None
    assert client.runs.args == ()


@pytest.mark.parametrize("key", ["prompt_manifest", "apex_actor_id"])
async def test_start_run_rejects_server_owned_configurable(key: str) -> None:
    client = FakeClient()

    with pytest.raises(ValueError, match="server-owned"):
        await PipelineReadService(client).start_run(
            title="Forged", configurable={key: {"forged": True}}
        )

    assert client.runs.args == ()


async def test_start_run_prepares_configurable_before_creating_the_run() -> None:
    client = FakeClient()
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def prepare(configurable: dict[str, Any], run_input: dict[str, Any]) -> None:
        assert client.runs.args == (), "preparation must precede run creation"
        calls.append((dict(configurable), dict(run_input)))
        configurable["prompt_manifest"] = _manifest_marker(configurable)

    await PipelineReadService(client).start_run(
        title="Prepared",
        request="check the peak",
        phases=["reporting"],
        configurable={"prompt_selections": _selections("reporting")},
        prepare_configurable=prepare,
    )

    assert len(calls) == 1
    assert calls[0][1]["title"] == "Prepared"
    assert calls[0][1]["request"] == "check the peak"
    configurable = client.runs.args[3]["configurable"]
    assert configurable["prompt_manifest"] == {"frozen_for": ["reporting"]}


async def test_idempotent_replay_neither_reprepares_nor_drifts_the_fingerprint() -> None:
    client = AtomicClient()
    prepared: list[int] = []

    async def prepare(configurable: dict[str, Any], _run_input: dict[str, Any]) -> None:
        prepared.append(1)
        # A different manifest on each call would break replay if it were
        # part of the caller-request fingerprint.
        configurable["prompt_manifest"] = {"call": len(prepared)}

    launch = {
        "title": "Retried",
        "idempotency_key": "retry-1",
        "principal_id": "c1",
        "phases": ["reporting"],
        "configurable": {"prompt_selections": _selections("reporting")},
        "prepare_configurable": prepare,
    }
    first = await PipelineReadService(client).start_run(**launch)
    second = await PipelineReadService(client).start_run(**launch)

    assert first == second
    assert prepared == [1]
    assert client.runs.successful_creates == 1


async def test_preparation_refusal_removes_the_fresh_thread_without_a_run() -> None:
    class TrackingThreads:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def create(self, metadata: Any = None) -> dict[str, str]:
            return {"thread_id": "thr-1"}

        async def delete(self, thread_id: str) -> None:
            self.deleted.append(thread_id)

    client = FakeClient()
    client.threads = cast(Any, TrackingThreads())

    async def refuse(_configurable: dict[str, Any], _run_input: dict[str, Any]) -> None:
        raise LaunchReadinessError("Selected prompt or version is unavailable for this test")

    with pytest.raises(LaunchReadinessError):
        await PipelineReadService(client).start_run(
            title="Refused",
            phases=["reporting"],
            configurable={"prompt_selections": _selections("reporting")},
            prepare_configurable=refuse,
        )

    assert client.threads.deleted == ["thr-1"]
    assert client.runs.args == ()


def _route_request(service: PipelineReadService) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )


async def test_create_pipeline_freezes_the_manifest_for_the_caller(monkeypatch) -> None:
    identity = _identity(["proj-1"])
    seen: list[Any] = []

    async def fake_prepare(configurable, who, run_input=None) -> None:
        seen.append(who)
        configurable["prompt_manifest"] = _manifest_marker(configurable)

    monkeypatch.setattr("apex.services.prompt_selections.prepare_prompt_manifest", fake_prepare)
    client = FakeClient()

    await pipelines.create_pipeline_run(
        _scope_request(
            title="Wizard launch",
            phases=["reporting"],
            configurable={"prompt_selections": _selections("reporting")},
        ),
        identity,
        cast(DocumentsRepository, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo()),
        cast(Any, _route_request(PipelineReadService(client))),
    )

    assert seen == [identity]
    assert client.runs.args[3]["configurable"]["prompt_manifest"] == {"frozen_for": ["reporting"]}


async def test_create_pipeline_reports_which_prompt_selection_failed(monkeypatch) -> None:
    async def unavailable(configurable, who, run_input=None) -> None:
        raise HTTPException(
            422,
            [
                {
                    "loc": ["prompt_selections", "phases", "reporting"],
                    "msg": "Selected prompt or version is unavailable for this test",
                    "type": "value_error",
                }
            ],
        )

    monkeypatch.setattr("apex.services.prompt_selections.prepare_prompt_manifest", unavailable)
    client = FakeClient()

    with pytest.raises(HTTPException) as excinfo:
        await pipelines.create_pipeline_run(
            _scope_request(
                title="Wizard launch",
                phases=["reporting"],
                configurable={"prompt_selections": _selections("reporting")},
            ),
            _identity(["proj-1"]),
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, _route_request(PipelineReadService(client))),
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == (
        "Selected prompt or version is unavailable for this test "
        "(prompt_selections.phases.reporting)"
    )
    assert client.runs.args == ()


async def test_create_pipeline_maps_an_invalid_manifest_to_a_prompt_refusal(monkeypatch) -> None:
    async def malformed(configurable, who, run_input=None) -> None:
        raise ValueError("manifest entry is invalid")

    monkeypatch.setattr("apex.services.prompt_selections.prepare_prompt_manifest", malformed)
    client = FakeClient()

    with pytest.raises(HTTPException) as excinfo:
        await pipelines.create_pipeline_run(
            _scope_request(
                title="Wizard launch",
                phases=["reporting"],
                configurable={"prompt_selections": _selections("reporting")},
            ),
            _identity(["proj-1"]),
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, _route_request(PipelineReadService(client))),
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == "Selected prompts could not be prepared for this test"
    assert client.runs.args == ()


async def test_create_pipeline_records_the_preset_it_started_from(monkeypatch) -> None:
    async def fake_prepare(configurable, who, run_input=None) -> None:
        configurable["prompt_manifest"] = _manifest_marker(configurable)

    recorded: list[Any] = []

    async def fake_record(thread_id, preset, configurable, identity) -> None:
        recorded.append((thread_id, preset, configurable["project_id"]))

    monkeypatch.setattr("apex.services.prompt_selections.prepare_prompt_manifest", fake_prepare)
    monkeypatch.setattr(pipelines, "record_preset_usage", fake_record)
    request = _scope_request(
        title="From preset",
        phases=["reporting"],
        configurable={"prompt_selections": _selections("reporting")},
    )
    request = request.model_copy(
        update={
            "preset": pipelines.PresetProvenance(
                kind="team", id="preset-1", version=3, name="ACC soak", modified=True
            )
        }
    )
    await pipelines.create_pipeline_run(
        request,
        _identity(["proj-1"]),
        cast(DocumentsRepository, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo()),
        cast(Any, _route_request(PipelineReadService(FakeClient()))),
    )

    [(thread_id, preset, project_id)] = recorded
    assert thread_id == "thr-1"
    assert (preset.kind, preset.id, preset.version, preset.modified) == (
        "team",
        "preset-1",
        3,
        True,
    )
    assert project_id == "proj-1"
