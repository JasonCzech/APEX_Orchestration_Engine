"""POST /v1/pipelines plumbing: document->packet expansion (scoped) + start_run.

Hermetic: a fake loopback client captures the thread-create / run-start calls, and
a fake documents repository drives the packet expansion. No DB, no LangGraph server.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

import apex.routers.pipelines as pipelines
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.services.pipeline_read import PipelineReadService


def _identity(project_ids: list[str]) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="c1",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id=pid) for pid in project_ids],
    )


class FakeDocsRepo:
    def __init__(self, docs: dict[str, Any]) -> None:
        self._docs = docs

    async def get(self, document_id: str) -> Any:
        return self._docs.get(document_id)


def _doc(doc_id: str, project_id: str | None) -> Any:
    return SimpleNamespace(
        id=doc_id,
        project_id=project_id,
        name=f"name-{doc_id}",
        summary=f"summary-{doc_id}",
        artifact_key=f"key/{doc_id}",
    )


async def test_documents_to_packets_maps_and_scopes() -> None:
    identity = _identity(["proj-1"])
    repo = FakeDocsRepo({"d1": _doc("d1", "proj-1"), "g1": _doc("g1", None)})
    packets = await pipelines._documents_to_packets(repo, identity, ["d1", "g1"])
    assert packets == [
        {
            "id": "document-d1",
            "source": "document",
            "title": "name-d1",
            "summary": "summary-d1",
            "ref": "/v1/artifacts/key/d1",
        },
        {
            "id": "document-g1",
            "source": "document",
            "title": "name-g1",
            "summary": "summary-g1",
            "ref": "/v1/artifacts/key/g1",
        },
    ]


async def test_documents_to_packets_rejects_out_of_scope() -> None:
    identity = _identity(["proj-1"])
    repo = FakeDocsRepo({"d2": _doc("d2", "proj-2")})
    with pytest.raises(HTTPException) as exc:
        await pipelines._documents_to_packets(repo, identity, ["d2"])
    assert exc.value.status_code == 404


async def test_documents_to_packets_missing_is_404() -> None:
    identity = _identity(["proj-1"])
    repo = FakeDocsRepo({})
    with pytest.raises(HTTPException) as exc:
        await pipelines._documents_to_packets(repo, identity, ["nope"])
    assert exc.value.status_code == 404


class FakeThreads:
    def __init__(self) -> None:
        self.metadata: Any = None

    async def create(self, metadata: Any = None) -> dict[str, str]:
        self.metadata = metadata
        return {"thread_id": "thr-1"}


class FakeRuns:
    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()

    async def create(
        self, thread_id: str, assistant_id: str, *, input: Any = None, config: Any = None
    ) -> dict[str, str]:
        self.args = (thread_id, assistant_id, input, config)
        return {"run_id": "run-1"}


class FakeClient:
    def __init__(self) -> None:
        self.threads = FakeThreads()
        self.runs = FakeRuns()


async def test_start_run_builds_config_and_input() -> None:
    client = FakeClient()
    service = PipelineReadService(client)
    result = await service.start_run(
        title="Analyze",
        request="recommend",
        project_id="proj-1",
        phases=["reporting", "postmortem"],
        agent_backend="anthropic",
        external_results={"source": "dash"},
        context_packets=[{"id": "p1", "source": "s", "title": "t"}],
    )

    assert result == {
        "thread_id": "thr-1",
        "run_id": "run-1",
        "stream_url": "/runs/run-1/stream",
    }
    assert client.threads.metadata["project_id"] == "proj-1"

    thread_id, assistant_id, run_input, run_config = client.runs.args
    assert thread_id == "thr-1"
    assert assistant_id == "pipeline"
    assert run_input["title"] == "Analyze"
    assert run_input["external_results"] == {"source": "dash"}
    assert run_input["context_packets"] == [{"id": "p1", "source": "s", "title": "t"}]

    configurable = run_config["configurable"]
    assert configurable["phases"] == ["reporting", "postmortem"]
    assert configurable["agent_backend"] == "anthropic"
    assert configurable["project_id"] == "proj-1"
    # Gates default to auto for the selected phases (headless analysis).
    assert configurable["gates"]["reporting"] == {
        "prompt_review": "auto",
        "output_review": "auto",
    }
    assert "postmortem" in configurable["gates"]
    assert "recursion_limit" in run_config


async def test_start_run_rejects_unknown_phase() -> None:
    service = PipelineReadService(FakeClient())
    with pytest.raises(ValueError, match="unknown phase"):
        await service.start_run(title="x", phases=["bogus"])
