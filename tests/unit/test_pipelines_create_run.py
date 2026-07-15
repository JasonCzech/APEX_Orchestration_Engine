"""POST /v1/pipelines plumbing: document->packet expansion (scoped) + start_run.

Hermetic: a fake loopback client captures the thread-create / run-start calls, and
a fake documents repository drives the packet expansion. No DB, no LangGraph server.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import HTTPException
from langgraph_sdk.errors import ConflictError

import apex.routers.pipelines as pipelines
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.repositories.documents import DocumentsRepository
from apex.services.pipeline_read import LaunchIdempotencyConflictError, PipelineReadService


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


class FakeCatalogRepo:
    def __init__(self, environments: dict[str, Any] | None = None) -> None:
        self.environments = environments or {}

    async def get_environment(self, environment_id: str) -> Any:
        return self.environments.get(environment_id)


def _environment(
    environment_id: str = "env-1",
    *,
    project_id: str = "proj-1",
    app_id: str = "app-a",
    base_url: str = "https://perf.example.test",
) -> Any:
    return SimpleNamespace(
        id=environment_id,
        application_id=app_id,
        application=SimpleNamespace(id=app_id, project_id=project_id),
        base_url=base_url,
        target_approved=True,
        target_version=3,
        options={},
    )


def _doc(doc_id: str, project_id: str | None, app_id: str | None = None) -> Any:
    return SimpleNamespace(
        id=doc_id,
        project_id=project_id,
        app_id=app_id,
        name=f"name-{doc_id}",
        summary=f"summary-{doc_id}",
        artifact_key=f"key/{doc_id}",
        extracted_text=f"text-{doc_id}",
    )


async def test_documents_to_packets_maps_and_scopes() -> None:
    identity = _identity(["proj-1"])
    repo = FakeDocsRepo({"d1": _doc("d1", "proj-1"), "g1": _doc("g1", None)})
    packets = await pipelines._documents_to_packets(
        cast(DocumentsRepository, repo), identity, ["d1", "g1"]
    )
    assert packets == [
        {
            "id": "document-d1",
            "source": "document",
            "title": "name-d1",
            "summary": "summary-d1",
            "ref": "/v1/artifacts/key/d1",
            "text": "text-d1",
        },
        {
            "id": "document-g1",
            "source": "document",
            "title": "name-g1",
            "summary": "summary-g1",
            "ref": "/v1/artifacts/key/g1",
            "text": "text-g1",
        },
    ]


async def test_documents_to_packets_rejects_out_of_scope() -> None:
    identity = _identity(["proj-1"])
    repo = FakeDocsRepo({"d2": _doc("d2", "proj-2")})
    with pytest.raises(HTTPException) as exc:
        await pipelines._documents_to_packets(cast(DocumentsRepository, repo), identity, ["d2"])
    assert exc.value.status_code == 404


async def test_documents_to_packets_rejects_sibling_app() -> None:
    identity = ConsumerIdentity(
        consumer_id="c1",
        name="c1",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="proj-1", app_id="app-a")],
    )
    repo = FakeDocsRepo({"d2": _doc("d2", "proj-1", "app-b")})

    with pytest.raises(HTTPException) as exc:
        await pipelines._documents_to_packets(cast(DocumentsRepository, repo), identity, ["d2"])

    assert exc.value.status_code == 404


async def test_documents_to_packets_missing_is_404() -> None:
    identity = _identity(["proj-1"])
    repo = FakeDocsRepo({})
    with pytest.raises(HTTPException) as exc:
        await pipelines._documents_to_packets(cast(DocumentsRepository, repo), identity, ["nope"])
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
        self,
        thread_id: str,
        assistant_id: str,
        *,
        input: Any = None,
        config: Any = None,
        **options: Any,
    ) -> dict[str, str]:
        self.args = (thread_id, assistant_id, input, config, options)
        return {"run_id": "run-1"}


class FakeClient:
    def __init__(self) -> None:
        self.threads = FakeThreads()
        self.runs = FakeRuns()


class AtomicThreads:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create(
        self,
        *,
        metadata: dict[str, Any],
        thread_id: str,
        if_exists: str,
    ) -> dict[str, Any]:
        assert if_exists == "do_nothing"
        self.rows.setdefault(thread_id, {"thread_id": thread_id, "metadata": metadata})
        return self.rows[thread_id]


class AtomicRuns:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, str]]] = {}
        self.successful_creates = 0

    async def list(self, thread_id: str, **_: Any) -> list[dict[str, str]]:
        return list(self.rows.get(thread_id, []))

    async def create(self, thread_id: str, *_args: Any, **_kwargs: Any) -> dict[str, str]:
        await asyncio.sleep(0)
        if self.rows.get(thread_id):
            request = httpx.Request("POST", f"http://loopback/threads/{thread_id}/runs")
            raise ConflictError(
                "active run exists",
                response=httpx.Response(409, request=request),
                body=None,
            )
        run = {"run_id": f"run-{len(self.rows) + 1}"}
        self.rows[thread_id] = [run]
        self.successful_creates += 1
        return run


class AtomicClient:
    def __init__(self) -> None:
        self.threads = AtomicThreads()
        self.runs = AtomicRuns()


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

    thread_id, assistant_id, run_input, run_config, options = client.runs.args
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
    assert options == {
        "stream_mode": ("updates", "messages-tuple", "custom"),
        "stream_subgraphs": True,
        "stream_resumable": True,
        "durability": "sync",
        "multitask_strategy": "reject",
    }


async def test_start_run_idempotency_is_atomic_for_concurrent_retries() -> None:
    client = AtomicClient()
    service = PipelineReadService(client)

    first, second = await asyncio.gather(
        service.start_run(
            title="Analyze",
            request="same request",
            project_id="proj-1",
            idempotency_key="key-1",
            principal_id="consumer-1",
        ),
        service.start_run(
            title="Analyze",
            request="same request",
            project_id="proj-1",
            idempotency_key="key-1",
            principal_id="consumer-1",
        ),
    )

    assert first == second
    assert len(client.threads.rows) == 1
    assert client.runs.successful_creates == 1


async def test_start_run_idempotency_is_scope_bound_and_rejects_payload_drift() -> None:
    client = AtomicClient()
    service = PipelineReadService(client)
    first = await service.start_run(
        title="Analyze",
        request="request one",
        project_id="proj-1",
        idempotency_key="key-1",
        principal_id="consumer-1",
    )
    second_scope = await service.start_run(
        title="Analyze",
        request="request one",
        project_id="proj-2",
        idempotency_key="key-1",
        principal_id="consumer-1",
    )

    assert first["thread_id"] != second_scope["thread_id"]
    with pytest.raises(LaunchIdempotencyConflictError):
        await service.start_run(
            title="Analyze",
            request="different request",
            project_id="proj-1",
            idempotency_key="key-1",
            principal_id="consumer-1",
        )

async def test_start_run_preserves_assistant_and_full_configurable() -> None:
    client = FakeClient()
    service = PipelineReadService(client)
    await service.start_run(
        title="Golden run",
        assistant_id="assistant-golden",
        project_id="proj-1",
        configurable={
            "project_id": "proj-1",
            "environment_id": "env-7",
            "engine": "loadrunner",
            "connections": {"execution_engine": "conn-1"},
            "model_by_phase": {"reporting": "claude-sonnet-4-5"},
            "agent_backend": "anthropic",
            "limits": {"max_revise_loops": 7},
            "phases": ["story_analysis"],
            "gates": {"story_analysis": {"prompt_review": "gated", "output_review": "auto"}},
        },
    )

    _thread_id, assistant_id, _input, run_config, _options = client.runs.args
    assert assistant_id == "assistant-golden"
    assert run_config["configurable"]["assistant_id"] == "assistant-golden"
    assert run_config["configurable"]["connections"] == {"execution_engine": "conn-1"}
    assert run_config["configurable"]["environment_id"] == "env-7"
    assert run_config["configurable"]["limits"]["max_revise_loops"] == 7
    assert run_config["recursion_limit"] > 0


async def test_start_run_rejects_unknown_phase() -> None:
    service = PipelineReadService(FakeClient())
    with pytest.raises(ValueError, match="unknown phase"):
        await service.start_run(title="x", phases=["bogus"])


async def test_start_run_rejects_unbounded_controls_before_creating_thread() -> None:
    client = FakeClient()
    service = PipelineReadService(client)

    with pytest.raises(ValueError, match="vusers"):
        await service.start_run(
            title="too large",
            configurable={"load_test": {"vusers": 10_001}},
        )

    assert client.threads.metadata is None


async def test_create_pipeline_infers_single_scope_into_thread_and_run_config() -> None:
    identity = _identity(["proj-1"])
    client = FakeClient()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )

    response = await pipelines.create_pipeline_run(
        pipelines.StartPipelineRequest(title="Scoped run"),
        identity,
        cast(DocumentsRepository, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo()),
        cast(Any, request),
    )

    assert response.thread_id == "thr-1"
    assert client.threads.metadata["project_id"] == "proj-1"
    assert client.runs.args[3]["configurable"]["project_id"] == "proj-1"


async def test_create_pipeline_infers_single_app_scope_into_config() -> None:
    identity = ConsumerIdentity(
        consumer_id="c1",
        name="c1",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="proj-1", app_id="app-a")],
    )
    client = FakeClient()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )

    await pipelines.create_pipeline_run(
        pipelines.StartPipelineRequest(title="App-scoped run"),
        identity,
        cast(DocumentsRepository, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo()),
        cast(Any, request),
    )

    assert client.threads.metadata == {
        "title": "App-scoped run",
        "project_id": "proj-1",
        "app_id": "app-a",
    }
    configurable = client.runs.args[3]["configurable"]
    assert configurable["project_id"] == "proj-1"
    assert configurable["app_id"] == "app-a"


async def test_create_pipeline_rejects_ambiguous_scope_before_thread_create() -> None:
    identity = _identity(["proj-1", "proj-2"])
    client = FakeClient()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )

    with pytest.raises(HTTPException) as exc:
        await pipelines.create_pipeline_run(
            pipelines.StartPipelineRequest(title="Ambiguous run"),
            identity,
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, request),
        )

    assert exc.value.status_code == 403
    assert "project_id metadata is required" in str(exc.value.detail)
    assert client.threads.metadata is None


async def test_create_pipeline_requires_project_when_app_is_explicit() -> None:
    identity = _identity(["proj-1"])
    request = SimpleNamespace(app=SimpleNamespace(dependency_overrides={}))

    with pytest.raises(HTTPException) as exc:
        await pipelines.create_pipeline_run(
            pipelines.StartPipelineRequest(title="Bad app scope", app_id="app-a"),
            identity,
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, request),
        )

    assert exc.value.status_code == 422
    assert "project_id is required" in str(exc.value.detail)


async def test_create_pipeline_resolves_only_environment_owned_by_selected_app() -> None:
    identity = ConsumerIdentity(
        consumer_id="c1",
        name="c1",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="proj-1", app_id="app-a")],
    )
    client = FakeClient()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )
    catalog = FakeCatalogRepo({"env-b": _environment("env-b", app_id="app-b")})

    with pytest.raises(HTTPException) as exc:
        await pipelines.create_pipeline_run(
            pipelines.StartPipelineRequest(
                title="Cross-app target",
                configurable={"environment_id": "env-b", "engine": "apex_load"},
            ),
            identity,
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, catalog),
            cast(Any, request),
        )

    assert exc.value.status_code == 404
    assert client.threads.metadata is None


async def test_create_pipeline_stamps_approved_target_and_version() -> None:
    identity = ConsumerIdentity(
        consumer_id="c1",
        name="c1",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="proj-1", app_id="app-a")],
    )
    client = FakeClient()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )
    environment = _environment(base_url="https://8.8.8.8/load")

    await pipelines.create_pipeline_run(
        pipelines.StartPipelineRequest(
            title="Approved target",
            configurable={"environment_id": environment.id, "engine": "apex_load"},
        ),
        identity,
        cast(DocumentsRepository, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo({environment.id: environment})),
        cast(Any, request),
    )

    configurable = client.runs.args[3]["configurable"]
    assert configurable["environment_target"] == "https://8.8.8.8/load"
    assert configurable["environment_target_version"] == 3


async def test_create_pipeline_rejects_direct_target_url_before_thread_create() -> None:
    identity = _identity(["proj-1"])
    client = FakeClient()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )

    with pytest.raises(HTTPException) as exc:
        await pipelines.create_pipeline_run(
            pipelines.StartPipelineRequest(
                title="Forged target",
                configurable={
                    "engine": "apex_load",
                    "load_test": {"target_environment": "http://169.254.169.254/latest"},
                },
            ),
            identity,
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, request),
        )

    assert exc.value.status_code == 422
    assert "cannot be supplied directly" in str(exc.value.detail)
    assert client.threads.metadata is None
