"""/context routes with a fake loopback client injected via dependency_overrides."""

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.routers.context import get_loopback_client, router

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


def make_client(
    identity: ConsumerIdentity, loopback: FakeLoopbackClient | None = None
) -> tuple[TestClient, FakeLoopbackClient]:
    loopback = loopback or FakeLoopbackClient()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: identity
    app.dependency_overrides[get_loopback_client] = lambda: loopback
    return TestClient(app), loopback


def test_create_summary_returns_202_with_run_id_and_stream_hint() -> None:
    client, loopback = make_client(SCOPED_OPERATOR)
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


def test_create_summary_requires_operator() -> None:
    client, _ = make_client(VIEWER)
    with client:
        response = client.post("/v1/context/summaries", json={"subject": "x"})
    assert response.status_code == 403


def test_create_summary_out_of_scope_project_403() -> None:
    client, loopback = make_client(SCOPED_OPERATOR)
    with client:
        response = client.post(
            "/v1/context/summaries", json={"subject": "x", "project_id": "proj-b"}
        )
    assert response.status_code == 403
    assert loopback.runs.calls == []


def test_create_summary_validates_body() -> None:
    client, _ = make_client(ADMIN)
    with client:
        response = client.post("/v1/context/summaries", json={"subject": ""})
    assert response.status_code == 422


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
    client, _ = make_client(ADMIN, FakeLoopbackClient(threads))
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
    client, _ = make_client(ADMIN)
    with client:
        response = client.get("/v1/context/evidence", params={"thread_id": "missing"})
    assert response.status_code == 404


def test_list_evidence_out_of_scope_project_403() -> None:
    client, _ = make_client(SCOPED_OPERATOR)
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
