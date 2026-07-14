"""/context routes with a fake loopback client injected via dependency_overrides."""

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.adapters.registry import PortKind
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.routers.context import get_loopback_client, router
from apex.services.documents import get_documents_repository
from apex.services.work_tracking import get_work_tracking_resolver

ADMIN = ConsumerIdentity(
    consumer_id="admin-1", name="root", consumer_type=ConsumerType.INTERNAL, role=Role.ADMIN
)
SCOPED_OPERATOR = ConsumerIdentity(
    consumer_id="op-1",
    name="alice",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.OPERATOR,
    scopes=[ScopeRef(project_id="proj-a")],
)
VIEWER = ConsumerIdentity(
    consumer_id="view-1", name="viewer", consumer_type=ConsumerType.DASHBOARD, role=Role.VIEWER
)


class FakeRuns:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(
        self, thread_id: str | None, assistant_id: str, *, input: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append({"thread_id": thread_id, "assistant_id": assistant_id, "input": input})
        return {"run_id": "run-ctx-1"}


class FakeThreads:
    def __init__(self, threads: list[dict[str, Any]]) -> None:
        self._threads = threads

    async def search(
        self, *, metadata: Any = None, limit: int = 10, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return self._threads[:limit]

    async def get(self, thread_id: str) -> dict[str, Any]:
        for thread in self._threads:
            if thread["thread_id"] == thread_id:
                return thread
        raise KeyError(thread_id)


class FakeLoopbackClient:
    def __init__(self, threads: list[dict[str, Any]] | None = None) -> None:
        self.runs = FakeRuns()
        self.threads = FakeThreads(threads or [])


class FakeWorkAdapter:
    def __init__(self, provider: str = "stub", project_id: str | None = None) -> None:
        self.provider = provider
        self.project_id = project_id
        self.apex_project_id = project_id


class FakeDocumentsRepository:
    def __init__(self, documents: dict[str, Any] | None = None) -> None:
        self.documents = documents or {}

    async def get(self, document_id: str) -> Any:
        return self.documents.get(document_id)


class FakeResolver:
    def __init__(self, adapter: Any | None = None) -> None:
        self.adapter = adapter or FakeWorkAdapter()
        self.calls: list[tuple[PortKind, str | None, str | None]] = []

    async def resolve(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> Any:
        self.calls.append((kind, connection_id, project_id))
        return self.adapter


def make_client(
    identity: ConsumerIdentity,
    loopback: FakeLoopbackClient | None = None,
    resolver: FakeResolver | None = None,
    documents: FakeDocumentsRepository | None = None,
) -> tuple[TestClient, FakeLoopbackClient, FakeResolver]:
    loopback = loopback or FakeLoopbackClient()
    resolver = resolver or FakeResolver()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: identity
    app.dependency_overrides[get_loopback_client] = lambda: loopback
    app.dependency_overrides[get_work_tracking_resolver] = lambda: resolver
    app.dependency_overrides[get_documents_repository] = lambda: (
        documents or FakeDocumentsRepository()
    )
    return TestClient(app), loopback, resolver


def test_create_summary_returns_202_with_run_id_and_stream_hint() -> None:
    client, loopback, resolver = make_client(SCOPED_OPERATOR)
    with client:
        response = client.post(
            "/v1/context/summaries",
            json={"subject": "Checkout latency", "work_item_keys": ["PHX-241"]},
        )
    assert response.status_code == 202
    assert response.json() == {
        "run_id": "run-ctx-1",
        "stream_url": "/runs/run-ctx-1/stream",
    }
    [call] = loopback.runs.calls
    assert call["thread_id"] is None
    assert call["assistant_id"] == "context"
    assert call["input"]["project_id"] == "proj-a"
    assert resolver.calls == [(PortKind.WORK_TRACKING, None, "proj-a")]


def test_create_summary_requires_operator() -> None:
    client, _, _ = make_client(VIEWER)
    with client:
        response = client.post("/v1/context/summaries", json={"subject": "x"})
    assert response.status_code == 403


def test_create_summary_passes_authorized_uploaded_document_evidence() -> None:
    documents = FakeDocumentsRepository(
        {
            "doc-a": SimpleNamespace(
                id="doc-a",
                project_id="proj-a",
                app_id=None,
                name="checkout-runbook.md",
                summary="Checkout troubleshooting",
                artifact_key="documents/doc-a/checkout-runbook.md",
                extracted_text="Use this uploaded runbook as evidence.",
            )
        }
    )
    client, loopback, resolver = make_client(SCOPED_OPERATOR, documents=documents)

    with client:
        response = client.post(
            "/v1/context/summaries",
            json={"subject": "Checkout latency", "document_ids": ["doc-a"]},
        )

    assert response.status_code == 202
    [call] = loopback.runs.calls
    assert call["input"]["document_packets"] == [
        {
            "id": "document-doc-a",
            "source": "document",
            "title": "checkout-runbook.md",
            "summary": "Checkout troubleshooting",
            "ref": "/v1/artifacts/documents/doc-a/checkout-runbook.md",
            "text": "Use this uploaded runbook as evidence.",
        }
    ]
    assert "document_ids" not in call["input"]
    assert resolver.calls == []


def test_create_summary_hides_sibling_app_document() -> None:
    app_operator = SCOPED_OPERATOR.model_copy(
        update={"scopes": [ScopeRef(project_id="proj-a", app_id="app-a")]}
    )
    documents = FakeDocumentsRepository(
        {
            "doc-b": SimpleNamespace(
                id="doc-b",
                project_id="proj-a",
                app_id="app-b",
                name="secret.md",
                summary=None,
                artifact_key="documents/doc-b/secret.md",
                extracted_text="sibling evidence",
            )
        }
    )
    client, loopback, _ = make_client(app_operator, documents=documents)

    with client:
        response = client.post(
            "/v1/context/summaries",
            json={"subject": "x", "document_ids": ["doc-b"]},
        )

    assert response.status_code == 404
    assert loopback.runs.calls == []


def test_create_summary_out_of_scope_project_403() -> None:
    client, loopback, _ = make_client(SCOPED_OPERATOR)
    with client:
        response = client.post(
            "/v1/context/summaries", json={"subject": "x", "project_id": "proj-b"}
        )
    assert response.status_code == 403
    assert loopback.runs.calls == []


def test_create_summary_rejects_work_adapter_bound_to_sibling_project() -> None:
    resolver = FakeResolver(FakeWorkAdapter(provider="jira", project_id="proj-b"))
    client, loopback, _ = make_client(SCOPED_OPERATOR, resolver=resolver)
    with client:
        response = client.post(
            "/v1/context/summaries",
            json={"subject": "x", "work_item_keys": ["PROJ-A-1"]},
        )
    assert response.status_code == 403
    assert loopback.runs.calls == []


def test_create_summary_multi_project_scope_requires_selection() -> None:
    identity = SCOPED_OPERATOR.model_copy(
        update={"scopes": [ScopeRef(project_id="proj-a"), ScopeRef(project_id="proj-b")]}
    )
    client, loopback, resolver = make_client(identity)
    with client:
        response = client.post("/v1/context/summaries", json={"subject": "x"})
    assert response.status_code == 403
    assert loopback.runs.calls == []
    assert resolver.calls == []


def test_create_summary_validates_body() -> None:
    client, _, _ = make_client(ADMIN)
    with client:
        response = client.post("/v1/context/summaries", json={"subject": ""})
    assert response.status_code == 422


def test_create_summary_rejects_provider_fanout_before_resolution() -> None:
    client, loopback, resolver = make_client(SCOPED_OPERATOR)
    with client:
        response = client.post(
            "/v1/context/summaries",
            json={
                "subject": "incident",
                "work_item_keys": [f"ITEM-{index}" for index in range(51)],
            },
        )
    assert response.status_code == 422
    assert loopback.runs.calls == []
    assert resolver.calls == []


def test_create_summary_rejects_document_fanout_before_reads() -> None:
    client, loopback, resolver = make_client(SCOPED_OPERATOR)
    with client:
        response = client.post(
            "/v1/context/summaries",
            json={
                "subject": "incident",
                "document_ids": [f"doc-{index}" for index in range(33)],
            },
        )
    assert response.status_code == 422
    assert loopback.runs.calls == []
    assert resolver.calls == []


def test_list_evidence_aggregates_packets() -> None:
    threads = [
        {
            "thread_id": "t1",
            "values": {
                "context_packets": [
                    {"id": "p1", "source": "work_tracking", "title": "PHX-241", "ref": "url"}
                ]
            },
        }
    ]
    client, _, _ = make_client(ADMIN, FakeLoopbackClient(threads))
    with client:
        response = client.get("/v1/context/evidence")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "p1",
            "source": "work_tracking",
            "title": "PHX-241",
            "summary": None,
            "ref": "url",
            "thread_id": "t1",
        }
    ]


def test_list_evidence_unknown_thread_404() -> None:
    client, _, _ = make_client(ADMIN)
    with client:
        response = client.get("/v1/context/evidence", params={"thread_id": "missing"})
    assert response.status_code == 404


def test_list_evidence_out_of_scope_project_403() -> None:
    client, _, _ = make_client(SCOPED_OPERATOR)
    with client:
        response = client.get("/v1/context/evidence", params={"project": "proj-b"})
    assert response.status_code == 403


def test_list_evidence_requires_authentication_dependency() -> None:
    # No identity override: the real dependency runs and rejects the keyless request.
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_loopback_client] = lambda: FakeLoopbackClient()
    with TestClient(app) as client:
        assert client.get("/v1/context/evidence").status_code == 401
