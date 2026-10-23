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
from langgraph_sdk.errors import APIStatusError, ConflictError, NotFoundError

import apex.routers.pipelines as pipelines
import apex.services.pipeline_read as pipeline_read
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.repositories.documents import DocumentsRepository
from apex.services.langgraph_client import LAUNCH_ROOT_FINGERPRINT_METADATA_KEY
from apex.services.pipeline_read import (
    LaunchIdempotencyConflictError,
    LaunchProviderError,
    PipelineReadService,
)


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
        self.environments = {"env-1": _environment()} if environments is None else environments
        self.environment_get_calls: list[str] = []

    async def get_environment(self, environment_id: str) -> Any:
        self.environment_get_calls.append(environment_id)
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
        application=SimpleNamespace(
            id=app_id, project_ids=[project_id], name=app_id, description=None, archived_at=None
        ),
        name=environment_id,
        kind="service",
        hosts=[],
        base_url=base_url,
        target_approved=True,
        target_version=3,
        options={},
    )


def _scope_request(**kwargs):
    """Explicit topology for the hermetic route fixtures (not production inference)."""
    from apex.domain.test_scope import TestScopeV1

    configurable = kwargs.get("configurable") or {}
    scope = TestScopeV1.model_validate(
        {
            "schema_version": 1,
            "participants": [
                {
                    "id": "backend",
                    "layer": "backend",
                    "project_id": kwargs.get("project_id")
                    or configurable.get("project_id")
                    or "proj-1",
                    "application_id": kwargs.get("app_id") or configurable.get("app_id") or "app-a",
                    "environment_id": configurable.get("environment_id") or "env-1",
                }
            ],
            "primary_participant_id": "backend",
            "traffic_entry_participant_id": "backend",
        }
    )
    return pipelines.StartPipelineRequest(test_scope=scope, **kwargs)


@pytest.fixture(autouse=True)
def _fake_projection_writer(monkeypatch):
    # FakeClient does not run native authorization or own a database. Real atomic
    # scope persistence/revocation is covered separately in test_test_scope.
    from unittest.mock import AsyncMock

    monkeypatch.setattr("apex.services.test_scope.persist_test_scope", AsyncMock())


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


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "unsafe\x00title"},
        {"title": "run", "project_id": "unsafe\x00project"},
        {"title": "run", "configurable": {"nested": "unsafe\x00value"}},
        {"title": "run", "gates": {"phase\x00shadow": {"prompt_review": "auto"}}},
        {"title": "run", "assistant_id": "a" * 257},
        {"title": "run", "phases": ["unknown_phase"]},
        {"title": "run", "phases": ["reporting", "reporting"]},
        {"title": "run", "model_by_phase": {"reporting": "m" * 201}},
        {"title": "run", "model_by_phase": {"unknown_phase": "claude-opus-4-8"}},
        {
            "title": "run",
            "context_packets": [{"id": "packet", "source": "sdk", "title": "unsafe\x00title"}],
        },
    ],
)
def test_start_pipeline_request_rejects_nul_before_durable_run_creation(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        pipelines.StartPipelineRequest.model_validate(payload)


def test_prompt_review_update_rejects_nul_before_checkpoint_persistence() -> None:
    with pytest.raises(ValueError):
        pipelines.PhasePromptReviewUpdate(
            system="safe",
            phase_prompt="unsafe\x00prompt",
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


async def test_documents_to_packets_percent_encodes_artifact_path_segments() -> None:
    identity = _identity(["proj-1"])
    document = _doc("d1", "proj-1")
    document.artifact_key = "documents/d1/spec ?#%.txt"

    packets = await pipelines._documents_to_packets(
        cast(DocumentsRepository, FakeDocsRepo({"d1": document})),
        identity,
        ["d1"],
    )

    assert packets[0]["ref"] == "/v1/artifacts/documents/d1/spec%20%3F%23%25.txt"


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


class FakeAssistants:
    async def get(self, assistant_id):
        return {"graph_id": "pipeline", "config": {"configurable": {}}}


class FakeClient:
    def __init__(self) -> None:
        self.assistants = FakeAssistants()
        self.threads = FakeThreads()
        self.runs = FakeRuns()


class AmbiguousThreads(FakeThreads):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    async def delete(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class AcceptedThenLostRuns(FakeRuns):
    def __init__(self) -> None:
        super().__init__()
        self.accepted: list[str] = []

    async def create(self, thread_id: str, *_args: Any, **_kwargs: Any) -> dict[str, str]:
        self.accepted.append(thread_id)
        raise httpx.ReadTimeout("response lost after run commit")


class AmbiguousClient:
    def __init__(self) -> None:
        self.assistants = FakeAssistants()
        self.threads = AmbiguousThreads()
        self.runs = AcceptedThenLostRuns()


class DefinitivelyRejectedRuns(FakeRuns):
    async def create(self, thread_id: str, *_args: Any, **_kwargs: Any) -> dict[str, str]:
        request = httpx.Request("POST", f"http://loopback/threads/{thread_id}/runs")
        raise NotFoundError(
            "assistant not found",
            response=httpx.Response(404, request=request),
            body=None,
        )


class DefinitivelyRejectedClient:
    def __init__(self) -> None:
        self.assistants = FakeAssistants()
        self.threads = AmbiguousThreads()
        self.runs = DefinitivelyRejectedRuns()


class RetryableClientErrorRuns(FakeRuns):
    def __init__(self, status_code: int) -> None:
        super().__init__()
        self.status_code = status_code

    async def create(self, thread_id: str, *_args: Any, **_kwargs: Any) -> dict[str, str]:
        request = httpx.Request("POST", f"http://loopback/threads/{thread_id}/runs")
        raise APIStatusError(
            "retryable client response after dispatch",
            response=httpx.Response(self.status_code, request=request),
            body=None,
        )


class AtomicThreads:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def get(self, thread_id: str) -> dict[str, Any]:
        if thread_id not in self.rows:
            raise NotFoundError(
                "missing",
                response=httpx.Response(
                    404, request=httpx.Request("GET", "http://runtime/threads")
                ),
                body=None,
            )
        return self.rows[thread_id]

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
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.successful_creates = 0
        self.list_options: list[dict[str, Any]] = []

    async def list(self, thread_id: str, **options: Any) -> list[dict[str, Any]]:
        self.list_options.append(options)
        rows = list(reversed(self.rows.get(thread_id, [])))
        offset = options.get("offset", 0)
        limit = options.get("limit", 10)
        selected = rows[offset : offset + limit]
        select = options.get("select")
        if select:
            return [{key: row[key] for key in select if key in row} for row in selected]
        return selected

    async def create(self, thread_id: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        if self.rows.get(thread_id):
            request = httpx.Request("POST", f"http://loopback/threads/{thread_id}/runs")
            raise ConflictError(
                "active run exists",
                response=httpx.Response(409, request=request),
                body=None,
            )
        run = {
            "run_id": f"run-{len(self.rows) + 1}",
            "metadata": dict(_kwargs.get("metadata") or {}),
            "created_at": f"2026-01-01T00:00:{len(self.rows):02d}+00:00",
        }
        self.rows[thread_id] = [run]
        self.successful_creates += 1
        return run


class AtomicClient:
    def __init__(self) -> None:
        self.assistants = FakeAssistants()
        self.threads = AtomicThreads()
        self.runs = AtomicRuns()


class DuplicateFriendlyRuns:
    """Provider fake that relies on the facade lock instead of rejecting races."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.successful_creates = 0

    async def list(self, thread_id: str, **_: Any) -> list[dict[str, Any]]:
        rows = list(reversed(self.rows.get(thread_id, [])))
        offset = _.get("offset", 0)
        limit = _.get("limit", 10)
        selected = rows[offset : offset + limit]
        select = _.get("select")
        snapshot = (
            [{key: row[key] for key in select if key in row} for row in selected]
            if select
            else selected
        )
        # Without a process-wide lock, independently constructed request
        # services both observe this empty snapshot before either creates.
        await asyncio.sleep(0.02)
        return snapshot

    async def create(self, thread_id: str, *_args: Any, **kwargs: Any) -> dict[str, Any]:
        self.successful_creates += 1
        run = {
            "run_id": f"run-{self.successful_creates}",
            "metadata": dict(kwargs.get("metadata") or {}),
            "created_at": f"2026-01-01T00:00:{self.successful_creates:02d}+00:00",
        }
        self.rows.setdefault(thread_id, []).append(run)
        return run


class DuplicateFriendlyClient:
    def __init__(self) -> None:
        self.assistants = FakeAssistants()
        self.threads = AtomicThreads()
        self.runs = DuplicateFriendlyRuns()


class PreseededThreadClient:
    def __init__(self) -> None:
        self.runs = AtomicRuns()

        class Threads:
            async def create(self, **kwargs: Any) -> dict[str, Any]:
                return {
                    "thread_id": kwargs["thread_id"],
                    "metadata": {"project_id": "proj-1"},
                }

        self.assistants = FakeAssistants()
        self.threads = Threads()


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
        "stream_url": "/threads/thr-1/runs/run-1/stream?stream_mode=custom",
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
        "stream_mode": "custom",
        "stream_subgraphs": True,
        "stream_resumable": True,
        "durability": "sync",
        "multitask_strategy": "reject",
    }


async def test_start_run_runs_pre_start_hook_before_creating_run() -> None:
    events: list[str] = []

    class OrderedRuns(FakeRuns):
        async def create(self, *args: Any, **kwargs: Any) -> dict[str, str]:
            events.append("run")
            return await super().create(*args, **kwargs)

    client = FakeClient()
    client.runs = OrderedRuns()

    async def collect_artifacts(thread_id: str) -> list[dict[str, Any]]:
        assert thread_id == "thr-1"
        events.append("artifacts")
        return [
            {
                "id": "apex-load-artifact-1",
                "source": "apex_load",
                "title": "Native report",
                "ref": "/v1/artifacts/engine-runs/hash/report.json",
            }
        ]

    await PipelineReadService(client).start_run(
        title="Analyze",
        project_id="proj-1",
        context_packets=[{"id": "existing", "source": "operator", "title": "Context"}],
        before_run_start=collect_artifacts,
    )

    assert events == ["artifacts", "run"]
    assert client.runs.args[2]["context_packets"] == [
        {"id": "existing", "source": "operator", "title": "Context"},
        {
            "id": "apex-load-artifact-1",
            "source": "apex_load",
            "title": "Native report",
            "ref": "/v1/artifacts/engine-runs/hash/report.json",
        },
    ]


@pytest.mark.parametrize("boundary", ["thread", "run"])
async def test_start_run_rejects_credential_shaped_native_ids(boundary: str) -> None:
    client = FakeClient()
    canary = "pipeline-native-id-secret-canary"

    if boundary == "thread":

        async def unsafe_thread(metadata: Any = None) -> dict[str, Any]:
            client.threads.metadata = metadata
            return {"thread_id": f"password={canary}"}

        cast(Any, client.threads).create = unsafe_thread
    else:

        async def unsafe_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
            client.runs.args = (*args, kwargs)
            return {"run_id": f"Authorization: Bearer {canary}"}

        cast(Any, client.runs).create = unsafe_run

    with pytest.raises(LaunchProviderError, match="runtime .* creation failed") as excinfo:
        await PipelineReadService(client).start_run(title="Analyze")

    assert canary not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    if boundary == "thread":
        assert client.runs.args == ()
    else:
        assert client.runs.args


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "x" * (pipeline_read.MAX_LAUNCH_TITLE_CHARS + 1)},
        {"title": "Analyze", "request": "x" * 20_001},
        {
            "title": "Analyze",
            "idempotency_key": "x" * (pipeline_read.MAX_LAUNCH_IDEMPOTENCY_KEY_CHARS + 1),
        },
        {"title": "Analyze", "project_id": " project-with-ambiguous-whitespace "},
    ],
)
async def test_start_run_service_bounds_durable_launch_fields_before_provider_io(
    kwargs: dict[str, Any],
) -> None:
    client = FakeClient()

    with pytest.raises(ValueError):
        await PipelineReadService(client).start_run(**kwargs)

    assert client.threads.metadata is None
    assert client.runs.args == ()


async def test_start_run_rejects_polymorphic_thread_response_without_invoking_hooks() -> None:
    hooks: list[str] = []

    class HostileThread(dict[str, Any]):
        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            hooks.append("get")
            raise AssertionError("provider mapping hook ran")

    client = FakeClient()

    async def hostile_thread(metadata: Any = None) -> dict[str, Any]:
        client.threads.metadata = metadata
        return cast(dict[str, Any], HostileThread(thread_id="thr-1"))

    cast(Any, client.threads).create = hostile_thread

    with pytest.raises(LaunchProviderError, match="thread creation failed"):
        await PipelineReadService(client).start_run(title="Analyze")

    assert hooks == []
    assert client.runs.args == ()


async def test_idempotent_start_does_not_invoke_provider_fingerprint_equality() -> None:
    hooks: list[str] = []

    class HostileFingerprint:
        def __eq__(self, _other: object) -> bool:
            hooks.append("eq")
            raise AssertionError("provider equality hook ran")

        def __ne__(self, _other: object) -> bool:
            hooks.append("ne")
            raise AssertionError("provider equality hook ran")

    client = AtomicClient()

    async def hostile_thread(**kwargs: Any) -> dict[str, Any]:
        return {
            "thread_id": kwargs["thread_id"],
            "metadata": {"launch_idempotency_fingerprint": HostileFingerprint()},
        }

    cast(Any, client.threads).create = hostile_thread

    with pytest.raises(LaunchIdempotencyConflictError):
        await PipelineReadService(client).start_run(
            title="Analyze",
            idempotency_key="retry-1",
            principal_id="principal-1",
        )

    assert hooks == []
    assert client.runs.successful_creates == 0


async def test_idempotent_start_rejects_drifted_thread_creation_response() -> None:
    client = AtomicClient()

    async def drifted_thread(**kwargs: Any) -> dict[str, Any]:
        return {"thread_id": "different-thread", "metadata": kwargs["metadata"]}

    cast(Any, client.threads).create = drifted_thread

    with pytest.raises(LaunchIdempotencyConflictError):
        await PipelineReadService(client).start_run(
            title="Analyze",
            idempotency_key="retry-1",
            principal_id="principal-1",
        )

    assert client.runs.successful_creates == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"request": "Authorization: Bearer launch-secret-canary"},
        {"configurable": {"pre_execution_context": ["database_password=launch-secret-canary"]}},
        {
            "context_packets": [
                {
                    "id": "packet-1",
                    "source": "test",
                    "title": "context",
                    "text": "https://user:launch-secret-canary@example.test/path",
                }
            ]
        },
    ],
)
async def test_start_run_rejects_credential_material_before_durable_creation(
    kwargs: dict[str, Any],
) -> None:
    client = FakeClient()

    with pytest.raises(ValueError, match="credential material") as excinfo:
        await PipelineReadService(client).start_run(title="Analyze", **kwargs)

    assert "launch-secret-canary" not in str(excinfo.value)
    assert client.threads.metadata is None
    assert client.runs.args == ()


async def test_launch_does_not_persist_raw_idempotency_key_or_principal() -> None:
    canary = "Bearer launch-idempotency-secret-canary"
    client = AtomicClient()

    await PipelineReadService(client).start_run(
        title="Credential-shaped retry key",
        idempotency_key=canary,
        principal_id="principal-secret-canary",
    )

    metadata = next(iter(client.threads.rows.values()))["metadata"]
    assert metadata.get("launch_idempotency_fingerprint")
    assert "launch_idempotency_key" not in metadata
    assert "launch_principal_id" not in metadata
    assert canary not in repr(metadata)
    assert "principal-secret-canary" not in repr(metadata)
    assert canary not in repr((client.threads.rows, client.runs.rows))
    assert "principal-secret-canary" not in repr((client.threads.rows, client.runs.rows))


async def test_ambiguous_run_create_keeps_thread_for_reconciliation() -> None:
    client = AmbiguousClient()

    with pytest.raises(LaunchProviderError, match="run creation failed") as excinfo:
        await PipelineReadService(client).start_run(title="Ambiguous launch")

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert client.runs.accepted == ["thr-1"]
    assert client.threads.deleted == []


async def test_definitive_run_create_4xx_deletes_fresh_random_thread() -> None:
    client = DefinitivelyRejectedClient()

    with pytest.raises(LaunchProviderError, match="run creation failed") as excinfo:
        await PipelineReadService(client).start_run(title="Rejected launch")

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert client.threads.deleted == ["thr-1"]


@pytest.mark.parametrize("status_code", [408, 429])
async def test_retryable_run_create_4xx_preserves_thread_for_reconciliation(
    status_code: int,
) -> None:
    client: Any = DefinitivelyRejectedClient()
    client.runs = RetryableClientErrorRuns(status_code)

    with pytest.raises(LaunchProviderError, match="run creation failed") as excinfo:
        await PipelineReadService(client).start_run(title="Retryable launch")

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert client.threads.deleted == []


async def test_thread_create_failure_detaches_sdk_request_and_body() -> None:
    canary = "Bearer launch-thread-create-secret-canary"

    class Threads(FakeThreads):
        async def create(self, metadata: Any = None) -> dict[str, str]:
            del metadata
            request = httpx.Request(
                "POST",
                "http://loopback/threads",
                headers={"Authorization": canary},
            )
            raise APIStatusError(
                "thread create failed",
                response=httpx.Response(503, request=request),
                body={"diagnostic": canary},
            )

    client = FakeClient()
    client.threads = Threads()

    with pytest.raises(LaunchProviderError, match="thread creation failed") as excinfo:
        await PipelineReadService(client).start_run(title="Thread failure")

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in repr(excinfo.value)


async def test_launch_conflict_reconciliation_miss_detaches_sdk_request() -> None:
    canary = "Bearer launch-conflict-secret-canary"

    class Runs(AtomicRuns):
        async def create(self, thread_id: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            request = httpx.Request(
                "POST",
                f"http://loopback/threads/{thread_id}/runs",
                headers={"Authorization": canary},
            )
            raise ConflictError(
                "active run exists",
                response=httpx.Response(409, request=request),
                body={"diagnostic": canary},
            )

    client = AtomicClient()
    client.runs = Runs()

    with pytest.raises(LaunchIdempotencyConflictError) as excinfo:
        await PipelineReadService(client).start_run(
            title="Conflict",
            idempotency_key="launch-conflict-1",
            principal_id="consumer-1",
        )

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in repr(excinfo.value)


async def test_start_run_idempotency_is_atomic_for_concurrent_retries() -> None:
    client = DuplicateFriendlyClient()
    first_service = PipelineReadService(client)
    second_service = PipelineReadService(client)

    first, second = await asyncio.gather(
        first_service.start_run(
            title="Analyze",
            request="same request",
            project_id="proj-1",
            idempotency_key="key-1",
            principal_id="consumer-1",
        ),
        second_service.start_run(
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


async def test_idempotent_replay_returns_launch_root_after_trusted_resume_run() -> None:
    client = AtomicClient()
    service = PipelineReadService(client)
    launch = await service.start_run(
        title="Analyze",
        request="same request",
        project_id="proj-1",
        idempotency_key="key-1",
        principal_id="consumer-1",
    )
    client.runs.rows[launch["thread_id"]].append(
        {
            "run_id": "resume-run",
            "metadata": {},
            "created_at": "2026-01-02T00:00:00+00:00",
        }
    )

    replay = await service.start_run(
        title="Analyze",
        request="same request",
        project_id="proj-1",
        idempotency_key="key-1",
        principal_id="consumer-1",
    )

    assert replay == launch
    assert replay["run_id"] != "resume-run"
    assert client.runs.successful_creates == 1


async def test_legacy_idempotent_replay_uses_oldest_run_only_after_complete_scan() -> None:
    client = AtomicClient()
    service = PipelineReadService(client)
    launch = await service.start_run(
        title="Legacy launch",
        project_id="proj-1",
        idempotency_key="legacy-key",
        principal_id="consumer-1",
    )
    root = client.runs.rows[launch["thread_id"]][0]
    root["metadata"].pop(LAUNCH_ROOT_FINGERPRINT_METADATA_KEY)
    client.runs.rows[launch["thread_id"]].append(
        {
            "run_id": "legacy-resume-run",
            "metadata": {},
            "created_at": "2026-01-02T00:00:00+00:00",
        }
    )

    replay = await service.start_run(
        title="Legacy launch",
        project_id="proj-1",
        idempotency_key="legacy-key",
        principal_id="consumer-1",
    )

    assert replay == launch


async def test_legacy_idempotent_replay_fails_closed_when_scan_cap_is_exhausted() -> None:
    client = AtomicClient()
    service = PipelineReadService(client)
    launch = await service.start_run(
        title="Oversized legacy history",
        project_id="proj-1",
        idempotency_key="legacy-key",
        principal_id="consumer-1",
    )
    root = client.runs.rows[launch["thread_id"]][0]
    root["metadata"].pop(LAUNCH_ROOT_FINGERPRINT_METADATA_KEY)
    client.runs.rows[launch["thread_id"]].extend(
        {
            "run_id": f"resume-{index}",
            "metadata": {},
            "created_at": f"2026-01-02T00:00:00.{index:06d}+00:00",
        }
        for index in range(pipeline_read._MAX_LAUNCH_RUN_RECONCILE_RECORDS)
    )

    with pytest.raises(LaunchIdempotencyConflictError, match="bounded reconciliation limit"):
        await service.start_run(
            title="Oversized legacy history",
            project_id="proj-1",
            idempotency_key="legacy-key",
            principal_id="consumer-1",
        )

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
    assert client.runs.list_options
    assert all(
        options["select"] == ["run_id", "status", "metadata", "created_at"]
        for options in client.runs.list_options
    )
    with pytest.raises(LaunchIdempotencyConflictError):
        await service.start_run(
            title="Analyze",
            request="different request",
            project_id="proj-1",
            idempotency_key="key-1",
            principal_id="consumer-1",
        )


async def test_start_run_never_adopts_preseeded_deterministic_thread_without_fingerprint() -> None:
    client = PreseededThreadClient()

    with pytest.raises(LaunchIdempotencyConflictError):
        await PipelineReadService(client).start_run(
            title="Analyze",
            request="victim request",
            project_id="proj-1",
            idempotency_key="predictable-key",
            principal_id="consumer-1",
        )

    assert client.runs.successful_creates == 0


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
    canary = "ghp_" + "A" * 24
    with pytest.raises(ValueError, match="unknown pipeline phase") as excinfo:
        await service.start_run(title="x", configurable={"phases": [canary]})

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in str(excinfo.value)


async def test_create_pipeline_detaches_raw_service_validation_error() -> None:
    canary = "bare-pipeline-validation-canary"

    class FailingService:
        async def assistant_launch_defaults(self, assistant_id):
            return {}

        async def start_run(self, **_kwargs: Any) -> dict[str, Any]:
            raise ValueError(canary)

    identity = _identity(["proj-1"])
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: FailingService()}
        )
    )

    with pytest.raises(HTTPException, match="invalid pipeline configuration") as excinfo:
        await pipelines.create_pipeline_run(
            _scope_request(title="Invalid run"),
            identity,
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, request),
        )

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in str(excinfo.value)


async def test_create_pipeline_maps_detached_provider_failure_to_502() -> None:
    canary = "pipeline-provider-boundary-secret-canary"

    class FailingService:
        async def assistant_launch_defaults(self, assistant_id):
            return {}

        async def start_run(self, **_kwargs: Any) -> dict[str, Any]:
            error = LaunchProviderError("pipeline runtime run creation failed")
            error.__context__ = RuntimeError(canary)
            raise error

    identity = _identity(["proj-1"])
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: FailingService()}
        )
    )

    with pytest.raises(HTTPException, match="pipeline runtime unavailable") as excinfo:
        await pipelines.create_pipeline_run(
            _scope_request(title="Provider failure"),
            identity,
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, request),
        )

    assert excinfo.value.status_code == 502
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in repr(excinfo.value)


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
        _scope_request(title="Scoped run"),
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
        _scope_request(title="App-scoped run"),
        identity,
        cast(DocumentsRepository, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo()),
        cast(Any, request),
    )

    assert client.threads.metadata == {
        "title": "App-scoped run",
        "project_id": "proj-1",
        "app_id": "app-a",
        "test_scope": cast(Any, _scope_request(title="fixture").test_scope).model_dump(mode="json"),
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

    assert exc.value.status_code == 422
    assert "test_scope is required" in exc.value.detail
    assert client.threads.metadata is None


async def test_create_pipeline_rejects_oversized_environment_id_before_catalog_io() -> None:
    identity = _identity(["proj-1"])
    client = FakeClient()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )
    catalog = FakeCatalogRepo()

    with pytest.raises(ValueError):
        await pipelines.create_pipeline_run(
            _scope_request(
                title="Oversized environment",
                configurable={"environment_id": "e" * 33},
            ),
            identity,
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, catalog),
            cast(Any, request),
        )

    assert catalog.environment_get_calls == []
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
    assert "test_scope is required" in str(exc.value.detail)


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
            _scope_request(
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


async def test_create_pipeline_planning_only_does_not_authorize_traffic() -> None:
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
        _scope_request(
            title="Approved target",
            phases=["story_analysis"],
            configurable={"environment_id": environment.id, "engine": "apex_load"},
        ),
        identity,
        cast(DocumentsRepository, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo({environment.id: environment})),
        cast(Any, request),
    )

    configurable = client.runs.args[3]["configurable"]
    assert configurable.get("environment_target") is None
    assert configurable.get("environment_target_version") is None


async def test_create_pipeline_environment_derives_authoritative_app_scope() -> None:
    identity = _identity(["proj-1"])
    client = FakeClient()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )
    environment = _environment(app_id="app-a", base_url="https://8.8.8.8/load")

    await pipelines.create_pipeline_run(
        _scope_request(
            title="Environment-owned run",
            phases=["story_analysis"],
            project_id="proj-1",
            configurable={"environment_id": environment.id, "engine": "apex_load"},
        ),
        identity,
        cast(DocumentsRepository, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo({environment.id: environment})),
        cast(Any, request),
    )

    assert client.threads.metadata["project_id"] == "proj-1"
    assert client.threads.metadata["app_id"] == "app-a"
    configurable = client.runs.args[3]["configurable"]
    assert configurable["project_id"] == "proj-1"
    assert configurable["app_id"] == "app-a"


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
            _scope_request(
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
    assert "target_environment cannot be supplied directly" in str(exc.value.detail)
    assert client.threads.metadata is None


def test_http_pipeline_launch_cannot_forge_cycle_dispatch_binding() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apex.app.dependencies import get_current_identity
    from apex.persistence.db import get_session

    app = FastAPI()
    app.include_router(pipelines.router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: _identity(["project"])
    app.dependency_overrides[get_session] = lambda: object()
    with TestClient(app) as client:
        response = client.post(
            "/v1/pipelines",
            json={
                "title": "Forged cycle",
                "project_id": "project",
                "configurable": {"cycle_dispatch_id": "forged"},
            },
        )
    assert response.status_code == 422
    assert "server-owned" in response.json()["detail"]


async def test_cycle_reconciliation_adopts_existing_thread_without_current_catalog_create():
    client = AtomicClient()
    service = PipelineReadService(client)
    kwargs: dict[str, Any] = {
        "title": "Exact Cycle execution",
        "project_id": "project",
        "app_id": "app",
        "configurable": {"cycle_dispatch_id": "dispatch"},
        "idempotency_key": "cycle-dispatch:dispatch",
        "principal_id": "original-operator",
    }
    first = await service.start_run(**kwargs)

    async def denied_create(**_kwargs):
        raise AssertionError("existing thread must not be recreated through changed catalog")

    client.threads.create = denied_create
    replay = await service.start_run(**kwargs, reuse_existing_thread=True)
    assert replay == first
    assert client.runs.successful_creates == 1
    client.threads.rows[first["thread_id"]]["metadata"]["launch_idempotency_fingerprint"] = (
        "different"
    )
    with pytest.raises(LaunchIdempotencyConflictError):
        await service.start_run(**kwargs, reuse_existing_thread=True)


@pytest.mark.parametrize("response", [None, [], {}, {"thread_id": "other", "metadata": {}}])
async def test_cycle_reconcile_malformed_existing_thread_never_falls_back_to_create(response):
    from unittest.mock import AsyncMock

    client = AtomicClient()
    client.threads.get = AsyncMock(return_value=response)
    client.threads.create = AsyncMock(
        side_effect=AssertionError("must not create on malformed read")
    )
    with pytest.raises((LaunchProviderError, LaunchIdempotencyConflictError)):
        await PipelineReadService(client).start_run(
            title="Cycle",
            idempotency_key="intent",
            principal_id="operator",
            reuse_existing_thread=True,
        )
    client.threads.create.assert_not_awaited()
    assert client.runs.successful_creates == 0


@pytest.mark.parametrize("engine, expected_status", [("sim", None), ("apex_load", 422)])
async def test_unapproved_environment_is_context_only_for_simulation(
    engine: str, expected_status: int | None
) -> None:
    identity = _identity(["proj-1"])
    client = FakeClient()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )
    env = _environment()
    env.target_approved = False
    env.base_url = None
    body = _scope_request(
        title="Selected environment",
        project_id="proj-1",
        configurable={
            "engine": engine,
            "environment_id": env.id,
            "environment_target": "http://untrusted",
            "environment_target_version": 99,
        },
    )

    async def launch() -> Any:
        return await pipelines.create_pipeline_run(
            body,
            identity,
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo({env.id: env})),
            cast(Any, request),
        )

    if expected_status:
        with pytest.raises(HTTPException) as caught:
            await launch()
        assert caught.value.status_code == expected_status
        assert "approve" in caught.value.detail
        assert client.threads.metadata is None
    else:
        await launch()
        config = client.runs.args[3]["configurable"]
        assert config["environment_id"] == env.id
        assert config["app_id"] == env.application_id
        assert config.get("environment_target") is None
        assert config.get("environment_target_version") is None


@pytest.mark.parametrize("status_code", [400, 422])
async def test_definitively_invalid_launch_preserves_422_class_and_detaches_provider_body(
    status_code: int,
) -> None:
    client: Any = DefinitivelyRejectedClient()
    client.runs = RetryableClientErrorRuns(status_code)
    with pytest.raises(pipeline_read.LaunchValidationError) as caught:
        await PipelineReadService(client).start_run(title="Invalid configuration")
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert client.threads.deleted == ["thr-1"]


async def test_create_pipeline_maps_native_validation_to_422_without_provider_input() -> None:
    class FailingService:
        async def assistant_launch_defaults(self, assistant_id):
            return {}

        async def start_run(self, **_kwargs: Any) -> Any:
            raise pipeline_read.LaunchValidationError("unsafe-provider-detail")

    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: FailingService()}
        )
    )
    with pytest.raises(HTTPException) as caught:
        await pipelines.create_pipeline_run(
            _scope_request(title="Invalid configuration"),
            _identity(["proj-1"]),
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, request),
        )
    assert caught.value.status_code == 422
    assert "unsafe-provider-detail" not in caught.value.detail
    assert caught.value.__context__ is None


async def test_launch_readiness_rejection_cleans_only_unclaimed_thread() -> None:
    from apex.services.execution_readiness import LaunchReadinessError

    client = FakeClient()
    client.threads = AmbiguousThreads()

    async def unavailable(_thread_id: str) -> None:
        raise LaunchReadinessError("Select an application")

    with pytest.raises(LaunchReadinessError):
        await PipelineReadService(client).start_run(
            title="Missing app", before_run_start=unavailable
        )
    assert client.threads.deleted == ["thr-1"]
    assert client.runs.args == ()


async def test_existing_root_replay_ignores_changed_execution_readiness() -> None:
    from apex.services.execution_readiness import LaunchReadinessError

    client = AtomicClient()
    service = PipelineReadService(client)
    kwargs: dict[str, Any] = {
        "title": "Replay",
        "idempotency_key": "stable-intent",
        "project_id": "p1",
    }
    first = await service.start_run(**kwargs)

    async def changed(_thread_id: str) -> None:
        raise LaunchReadinessError("Connection changed after original launch")

    assert await service.start_run(**kwargs, before_run_start=changed) == first
    assert client.runs.successful_creates == 1


async def test_explicit_empty_phase_plan_overrides_inherited_execution() -> None:
    client = FakeClient()
    await PipelineReadService(client).start_run(
        title="No execution", phases=[], configurable={"phases": ["execution"]}
    )
    config = client.runs.args[3]["configurable"]
    assert config["phases"] == []
    assert config["gates"] == {}


async def test_explicit_empty_gate_map_overrides_inherited_policy() -> None:
    client = FakeClient()
    await PipelineReadService(client).start_run(
        title="Default gate policy",
        gates={},
        configurable={"gates": {"execution": {"prompt_review": "auto"}}},
    )
    assert client.runs.args[3]["configurable"]["gates"] == {}


async def test_explicit_empty_model_map_overrides_inherited_models() -> None:
    client = FakeClient()
    await PipelineReadService(client).start_run(
        title="Default models",
        model_by_phase={},
        configurable={"model_by_phase": {"story_analysis": "claude-sonnet-4-5"}},
    )
    assert client.runs.args[3]["configurable"]["model_by_phase"] == {}


async def test_readiness_uses_effective_launch_overrides_not_replaced_inherited_values() -> None:
    client = FakeClient()
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={
                pipelines.get_pipeline_read_service: lambda: PipelineReadService(client)
            }
        )
    )
    await pipelines.create_pipeline_run(
        _scope_request(
            title="Cleared inherited values",
            phases=[],
            gates={},
            model_by_phase={},
            agent_backend="stub",
            configurable={
                "engine": "sim",
                "gates": {"invalid-phase": {}},
                "model_by_phase": {"invalid-phase": "legacy-model"},
                "agent_backend": "legacy-invalid",
                "phases": ["execution"],
            },
        ),
        _identity(["proj-1"]),
        cast(Any, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo()),
        cast(Any, request),
    )
    actual = client.runs.args[3]["configurable"]
    assert actual["phases"] == []
    assert actual["gates"] == actual["model_by_phase"] == {}
    assert actual["agent_backend"] == "stub"


@pytest.mark.parametrize("load_test", [{"vusers": 0}, {"duration_s": -1}])
async def test_explicit_launch_invalid_configuration_remains_safe_422(load_test) -> None:
    client = FakeClient()
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={
                pipelines.get_pipeline_read_service: lambda: PipelineReadService(client)
            }
        )
    )
    with pytest.raises(HTTPException) as caught:
        await pipelines.create_pipeline_run(
            _scope_request(title="Invalid", configurable={"engine": "sim", "load_test": load_test}),
            _identity(["proj-1"]),
            cast(Any, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, request),
        )
    assert caught.value.status_code == 422
    assert caught.value.detail == "invalid pipeline configuration"
    assert caught.value.__context__ is None
    assert client.threads.metadata is None


async def test_explicit_launch_maps_readiness_rejection_to_actionable_422(monkeypatch) -> None:
    from apex.services.execution_readiness import LaunchReadinessError

    client = FakeClient()
    client.threads = AmbiguousThreads()
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={
                pipelines.get_pipeline_read_service: lambda: PipelineReadService(client)
            }
        )
    )

    async def missing_app(config):
        assert config.project_id == "proj-1"
        assert "execution" in config.selected_phases()
        raise LaunchReadinessError("Select a project and application before execution.")

    monkeypatch.setattr(pipelines, "require_execution_readiness", missing_app)
    with pytest.raises(HTTPException) as caught:
        await pipelines.create_pipeline_run(
            _scope_request(title="Missing app", configurable={"engine": "sim"}),
            _identity(["proj-1"]),
            cast(Any, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, request),
        )
    assert caught.value.status_code == 422
    assert "Select a project and application" in caught.value.detail
    assert caught.value.__context__ is None
    assert client.runs.args == ()


@pytest.mark.parametrize("primary", ["front", "middle-a", "back"])
async def test_combined_scope_launch_keeps_owner_independent_of_traffic(primary, monkeypatch):
    from unittest.mock import AsyncMock

    from apex.domain.test_scope import TestScopeV1

    participants = [
        {
            "id": name,
            "project_id": f"project-{name}",
            "application_id": f"app-{name}",
            "environment_id": f"env-{name}",
            "layer": layer,
        }
        for name, layer in [
            ("front", "frontend"),
            ("middle-a", "middle"),
            ("middle-b", "middle"),
            ("back", "backend"),
        ]
    ]
    scope = TestScopeV1(
        participants=cast(Any, participants),
        primary_participant_id=primary,
        traffic_entry_participant_id="front",
    )
    catalog = FakeCatalogRepo(
        {
            p["environment_id"]: _environment(
                p["environment_id"],
                project_id=p["project_id"],
                app_id=p["application_id"],
                base_url="https://8.8.8.8/load",
            )
            for p in participants
        }
    )
    # Only the traffic destination needs load-target approval.
    for name, environment in catalog.environments.items():
        environment.target_approved = name == "env-front"
    client = FakeClient()
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={
                pipelines.get_pipeline_read_service: lambda: PipelineReadService(client)
            }
        )
    )
    monkeypatch.setattr(pipelines, "require_execution_readiness", AsyncMock())
    monkeypatch.setattr("apex.services.analysis_readiness.require_analysis_readiness", AsyncMock())
    await pipelines.create_pipeline_run(
        pipelines.StartPipelineRequest(
            title="Combined end-to-end system",
            test_scope=scope,
            phases=["execution"],
            configurable={"engine": "apex_load"},
        ),
        _identity([p["project_id"] for p in participants]),
        cast(Any, FakeDocsRepo({})),
        cast(Any, catalog),
        cast(Any, request),
    )
    config = client.runs.args[3]["configurable"]
    assert client.threads.metadata["project_id"] == f"project-{primary}"
    assert config["app_id"] == f"app-{primary}"
    assert config["environment_id"] == "env-front"
    assert config["environment_target"] == "https://8.8.8.8/load"
    assert config["environment_target_version"] == 3
    assert config["test_scope"] == scope.model_dump(mode="json")
    assert len(config["test_scope_snapshot"]["participants"]) == 4


async def test_combined_scope_rejects_partial_access_before_catalog_or_thread_io():
    from apex.domain.test_scope import TestScopeV1

    scope = TestScopeV1(
        participants=cast(
            Any,
            [
                {
                    "id": "front",
                    "project_id": "proj-1",
                    "application_id": "app-a",
                    "environment_id": "env-1",
                    "layer": "frontend",
                },
                {
                    "id": "back",
                    "project_id": "proj-2",
                    "application_id": "app-b",
                    "environment_id": "env-2",
                    "layer": "backend",
                },
            ],
        ),
        primary_participant_id="front",
        traffic_entry_participant_id="front",
    )
    catalog = FakeCatalogRepo()
    with pytest.raises(HTTPException) as exc:
        await pipelines.create_pipeline_run(
            pipelines.StartPipelineRequest(title="Forbidden combined scope", test_scope=scope),
            _identity(["proj-1"]),
            cast(Any, FakeDocsRepo({})),
            cast(Any, catalog),
            cast(Any, SimpleNamespace(app=SimpleNamespace(dependency_overrides={}))),
        )
    assert exc.value.status_code == 403
    assert catalog.environment_get_calls == []


async def test_legacy_retry_recovers_without_creating_or_revalidating_catalog():
    client = AtomicClient()
    service = PipelineReadService(client)
    old_config = {"engine": "sim", "environment_id": "env-1"}
    original = await service.start_run(
        title="Legacy retry",
        idempotency_key="old-intent",
        project_id="proj-1",
        app_id="app-a",
        configurable=old_config,
        principal_id="c1",
        phases=["story_analysis"],
    )

    async def old_state(_thread_id):
        return {"values": {"run_config": {**old_config, "project_id": "proj-1", "app_id": "app-a"}}}

    cast(Any, client.threads).get_state = old_state
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )
    catalog = FakeCatalogRepo({})  # Even a removed catalog row cannot create a duplicate.
    body = pipelines.StartPipelineRequest(
        title="Legacy retry",
        idempotency_key="old-intent",
        project_id="proj-1",
        app_id="app-a",
        configurable=old_config,
        phases=["story_analysis"],
    )
    recovered = await pipelines.create_pipeline_run(
        body,
        _identity(["proj-1"]),
        cast(Any, FakeDocsRepo({})),
        cast(Any, catalog),
        cast(Any, request),
    )
    assert recovered == original
    assert client.runs.successful_creates == 1
    assert catalog.environment_get_calls == []
    with pytest.raises(HTTPException) as conflict:
        await pipelines.create_pipeline_run(
            body.model_copy(update={"title": "Changed intent"}),
            _identity(["proj-1"]),
            cast(Any, FakeDocsRepo({})),
            cast(Any, catalog),
            cast(Any, request),
        )
    assert conflict.value.status_code == 409
    assert client.runs.successful_creates == 1


async def test_legacy_retry_with_missing_claim_never_creates_thread_or_run():
    client = AtomicClient()
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={
                pipelines.get_pipeline_read_service: lambda: PipelineReadService(client)
            }
        )
    )
    with pytest.raises(HTTPException) as exc:
        await pipelines.create_pipeline_run(
            pipelines.StartPipelineRequest(
                title="No old run", idempotency_key="unknown", project_id="proj-1", app_id="app-a"
            ),
            _identity(["proj-1"]),
            cast(Any, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, request),
        )
    assert exc.value.status_code == 422
    assert client.threads.rows == {}
    assert client.runs.successful_creates == 0


@pytest.mark.parametrize("assistant_id", [None, "pipeline", "custom"])
async def test_assistant_inherited_live_execution_requires_approved_entry(
    monkeypatch, assistant_id
):
    from unittest.mock import AsyncMock

    client = FakeClient()
    cast(Any, client).assistants = SimpleNamespace(
        get=AsyncMock(
            return_value={
                "graph_id": "pipeline",
                "config": {"configurable": {"engine": "apex_load", "phases": ["execution"]}},
            }
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={
                pipelines.get_pipeline_read_service: lambda: PipelineReadService(client)
            }
        )
    )
    environment = _environment()
    environment.target_approved = False
    with pytest.raises(HTTPException) as exc:
        await pipelines.create_pipeline_run(
            _scope_request(title="Inherited live", assistant_id=assistant_id),
            _identity(["proj-1"]),
            cast(Any, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo({"env-1": environment})),
            cast(Any, request),
        )
    assert exc.value.status_code == 422
    assert "approved target" in str(exc.value.detail)
    assert client.threads.metadata is None


async def test_assistant_inherited_human_execution_does_not_require_load_approval(monkeypatch):
    from unittest.mock import AsyncMock

    from apex.domain.pipeline import PHASE_ORDER

    ownership = {"schema_version": 1, "owners": {phase.value: "hitl" for phase in PHASE_ORDER}}
    monkeypatch.setattr("apex.services.analysis_readiness.require_analysis_readiness", AsyncMock())
    client = FakeClient()
    cast(Any, client).assistants = SimpleNamespace(
        get=AsyncMock(
            return_value={
                "graph_id": "pipeline",
                "config": {
                    "configurable": {
                        "engine": "apex_load",
                        "phases": [phase.value for phase in PHASE_ORDER],
                        "phase_execution": ownership,
                        "analysis": {
                            "selections": [{"tool": "internal_rca", "connection_id": "rca"}]
                        },
                    }
                },
            }
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={
                pipelines.get_pipeline_read_service: lambda: PipelineReadService(client)
            }
        )
    )
    environment = _environment()
    environment.target_approved = False
    await pipelines.create_pipeline_run(
        _scope_request(title="Inherited human", assistant_id="custom"),
        _identity(["proj-1"]),
        cast(Any, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo({"env-1": environment})),
        cast(Any, request),
    )
    config = client.runs.args[3]["configurable"]
    assert config["engine"] == "apex_load"
    assert config["phase_execution"] == ownership
    assert config.get("environment_target") is None


async def test_scope_retry_recovers_original_intent_before_mutable_catalog(monkeypatch):
    client = AtomicClient()
    service = PipelineReadService(client)
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )
    environment = _environment()
    body = _scope_request(
        title="Stable scope retry",
        idempotency_key="scope-intent",
        phases=["story_analysis"],
        configurable={"engine": "sim"},
    )
    catalog = FakeCatalogRepo({"env-1": environment})
    first = await pipelines.create_pipeline_run(
        body,
        _identity(["proj-1"]),
        cast(Any, FakeDocsRepo({})),
        cast(Any, catalog),
        cast(Any, request),
    )
    environment.application.description = "changed after launch"
    environment.name = "renamed"
    catalog.environment_get_calls.clear()
    repeated = await pipelines.create_pipeline_run(
        body,
        _identity(["proj-1"]),
        cast(Any, FakeDocsRepo({})),
        cast(Any, catalog),
        cast(Any, request),
    )
    assert repeated == first
    assert catalog.environment_get_calls == []
    assert client.runs.successful_creates == 1
    with pytest.raises(HTTPException) as exc:
        await pipelines.create_pipeline_run(
            body.model_copy(update={"title": "Changed intent"}),
            _identity(["proj-1"]),
            cast(Any, FakeDocsRepo({})),
            cast(Any, catalog),
            cast(Any, request),
        )
    assert exc.value.status_code == 409
    assert client.runs.successful_creates == 1


async def test_legacy_live_queued_retry_uses_original_run_target_before_checkpoint():
    from unittest.mock import AsyncMock

    client = AtomicClient()
    service = PipelineReadService(client)
    config = {
        "engine": "apex_load",
        "environment_id": "env-1",
        "environment_target": "https://8.8.8.8/load",
        "environment_target_version": 3,
    }
    original = await service.start_run(
        title="Queued legacy",
        idempotency_key="queued",
        project_id="proj-1",
        app_id="app-a",
        configurable=config,
        principal_id="c1",
        phases=["execution"],
    )
    cast(Any, client.threads).get_state = AsyncMock(return_value={"values": {}})
    cast(Any, client.runs).get = AsyncMock(
        return_value={
            "thread_id": original["thread_id"],
            "run_id": original["run_id"],
            "kwargs": {
                "config": {"configurable": {**config, "project_id": "proj-1", "app_id": "app-a"}}
            },
        }
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )
    body = pipelines.StartPipelineRequest(
        title="Queued legacy",
        idempotency_key="queued",
        project_id="proj-1",
        app_id="app-a",
        configurable={"engine": "apex_load", "environment_id": "env-1"},
        phases=["execution"],
    )
    recovered = await pipelines.create_pipeline_run(
        body,
        _identity(["proj-1"]),
        cast(Any, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo({})),
        cast(Any, request),
    )
    assert recovered == original
    assert client.runs.successful_creates == 1
    with pytest.raises(HTTPException) as conflict:
        await pipelines.create_pipeline_run(
            body.model_copy(update={"request": "Different load"}),
            _identity(["proj-1"]),
            cast(Any, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo({})),
            cast(Any, request),
        )
    assert conflict.value.status_code == 409
    assert client.runs.successful_creates == 1


async def test_scope_launch_pins_defaults_against_later_assistant_edits():
    from apex.graphs.pipeline.configurable import PipelineConfigurable

    client = FakeClient()
    request = SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={
                pipelines.get_pipeline_read_service: lambda: PipelineReadService(client)
            }
        )
    )
    await pipelines.create_pipeline_run(
        _scope_request(title="Pinned default engine", phases=["execution"]),
        _identity(["proj-1"]),
        cast(Any, FakeDocsRepo({})),
        cast(Any, FakeCatalogRepo()),
        cast(Any, request),
    )
    outgoing = client.runs.args[3]["configurable"]
    assert outgoing["engine"] == "sim"
    # The frozen contract plus the launcher's attribution (a runtime key).
    assert set(outgoing) == set(PipelineConfigurable.model_fields) | {"apex_actor_id"}
    assert outgoing["apex_actor_id"] == "c1"
    # Runtime overlays request fields over newly edited assistant defaults.
    effective = {"engine": "apex_load", "phase_execution": {"changed": True}, **outgoing}
    assert effective["engine"] == "sim"
    assert effective["phase_execution"] is None


async def test_launch_attributes_the_acting_consumer_without_persisting_the_claim() -> None:
    """The actor is a runtime key on the run; the idempotency principal is never stored."""
    client = FakeClient()

    await PipelineReadService(client).start_run(
        title="Attributed",
        principal_id="claim-principal",
        actor_id="acting-operator",
    )

    configurable = client.runs.args[3]["configurable"]
    assert configurable["apex_actor_id"] == "acting-operator"
    assert "claim-principal" not in repr((client.threads.metadata, client.runs.args))
    assert "acting-operator" not in repr(client.threads.metadata)
    assert "acting-operator" not in repr(client.runs.args[4].get("metadata"))


async def test_an_idempotent_retry_by_another_actor_adopts_the_original_run() -> None:
    client = AtomicClient()
    launch: dict[str, Any] = {"title": "Retried", "idempotency_key": "k", "principal_id": "p"}

    first = await PipelineReadService(client).start_run(**launch, actor_id="operator-a")
    second = await PipelineReadService(client).start_run(**launch, actor_id="operator-b")

    assert first == second
    assert client.runs.successful_creates == 1


async def test_launch_without_an_actor_stays_unattributed() -> None:
    client = FakeClient()
    await PipelineReadService(client).start_run(title="Background", principal_id="reconciler")
    assert "apex_actor_id" not in client.runs.args[3]["configurable"]
