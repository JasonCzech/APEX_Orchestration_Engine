"""/prompts router: happy paths, error mapping, and role gating.

The DB is replaced wholesale via dependency_overrides (fake-backed catalog
service + synthetic identity); the loopback LangGraph client is monkeypatched.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role
from apex.routers.prompts import get_catalog, router
from apex.services.prompts import PromptCatalogService
from tests.unit.test_prompts_service import FakePromptRepository


def identity(role: Role) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1", name="tester", consumer_type=ConsumerType.INTERNAL, role=role
    )


@pytest.fixture
def repo() -> FakePromptRepository:
    return FakePromptRepository()


@pytest.fixture
def app(repo: FakePromptRepository) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    service = PromptCatalogService(repo)
    app.dependency_overrides[get_catalog] = lambda: service
    app.dependency_overrides[get_current_identity] = lambda: identity(Role.OPERATOR)
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def create_prompt(client: TestClient, **overrides: Any) -> dict[str, Any]:
    body = {
        "namespace": "phase",
        "key": "story_analysis/system",
        "description": "system prompt",
        "content": "v1 content",
        "note": "initial",
    }
    body.update(overrides)
    response = client.post("/v1/prompts", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_then_list_and_get(client: TestClient) -> None:
    created = create_prompt(client)
    assert created["namespace"] == "phase"
    assert created["key"] == "story_analysis/system"
    assert created["content"] == "v1 content"
    assert created["active_version"]["version"] == 1

    listed = client.get("/v1/prompts").json()
    assert [p["key"] for p in listed] == ["story_analysis/system"]
    assert listed[0]["active_version"]["version"] == 1
    assert "content" not in listed[0]  # list is summary-shaped

    detail = client.get(f"/v1/prompts/{created['id']}").json()
    assert detail["content"] == "v1 content"
    assert detail["note"] == "initial"


def test_duplicate_create_conflicts(client: TestClient) -> None:
    create_prompt(client)
    response = client.post(
        "/v1/prompts",
        json={"namespace": "phase", "key": "story_analysis/system", "content": "again"},
    )
    assert response.status_code == 409


def test_save_version_moves_pointer_and_history(client: TestClient) -> None:
    created = create_prompt(client)
    response = client.post(
        f"/v1/prompts/{created['id']}/versions", json={"content": "v2 content", "note": "tweak"}
    )
    assert response.status_code == 201
    v2 = response.json()
    assert v2["version"] == 2
    assert v2["content"] == "v2 content"
    assert v2["parent_version_id"] == created["active_version"]["id"]
    assert v2["created_by"] == "tester"

    detail = client.get(f"/v1/prompts/{created['id']}").json()
    assert detail["active_version"] == {"id": v2["id"], "version": 2}
    assert detail["content"] == "v2 content"

    history = client.get(f"/v1/prompts/{created['id']}/versions").json()
    assert [v["version"] for v in history] == [2, 1]
    assert all("content" not in v for v in history)  # history omits content

    one = client.get(f"/v1/prompts/{created['id']}/versions/{history[1]['id']}").json()
    assert one["content"] == "v1 content"


def test_rollback_and_wrong_prompt_conflict(client: TestClient) -> None:
    created = create_prompt(client)
    v1_id = created["active_version"]["id"]
    client.post(f"/v1/prompts/{created['id']}/versions", json={"content": "v2"})

    response = client.post(f"/v1/prompts/{created['id']}/rollback", json={"version_id": v1_id})
    assert response.status_code == 200
    assert response.json()["active_version"] == {"id": v1_id, "version": 1}
    assert response.json()["content"] == "v1 content"

    other = create_prompt(client, key="story_analysis/user", content="other")
    response = client.post(
        f"/v1/prompts/{created['id']}/rollback",
        json={"version_id": other["active_version"]["id"]},
    )
    assert response.status_code == 409  # version belongs to another prompt

    response = client.post(f"/v1/prompts/{created['id']}/rollback", json={"version_id": "nope"})
    assert response.status_code == 404


def test_archive_unarchive_and_list_filtering(client: TestClient) -> None:
    created = create_prompt(client)
    response = client.post(f"/v1/prompts/{created['id']}/archive")
    assert response.status_code == 200
    assert response.json()["archived_at"] is not None

    assert client.get("/v1/prompts").json() == []
    archived = client.get("/v1/prompts", params={"include_archived": "true"}).json()
    assert [p["id"] for p in archived] == [created["id"]]

    response = client.post(f"/v1/prompts/{created['id']}/unarchive")
    assert response.status_code == 200
    assert response.json()["archived_at"] is None
    assert [p["id"] for p in client.get("/v1/prompts").json()] == [created["id"]]


def test_unknown_prompt_404s(client: TestClient) -> None:
    assert client.get("/v1/prompts/nope").status_code == 404
    assert client.get("/v1/prompts/nope/versions").status_code == 404
    assert client.post("/v1/prompts/nope/archive").status_code == 404
    assert client.post("/v1/prompts/nope/versions", json={"content": "x"}).status_code == 404


def test_viewer_can_read_but_not_mutate(app: FastAPI) -> None:
    app.dependency_overrides[get_current_identity] = lambda: identity(Role.VIEWER)
    with TestClient(app) as client:
        assert client.get("/v1/prompts").status_code == 200
        body = {"namespace": "n", "key": "k", "content": "c"}
        assert client.post("/v1/prompts", json=body).status_code == 403
        assert client.post("/v1/prompts/x/versions", json={"content": "c"}).status_code == 403
        assert client.post("/v1/prompts/x/rollback", json={"version_id": "v"}).status_code == 403
        assert client.post("/v1/prompts/x/archive").status_code == 403
        assert client.post("/v1/prompts/x/unarchive").status_code == 403
        assert client.post("/v1/prompts/x/test", json={}).status_code == 403


class FakeRuns:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, thread_id: Any, assistant_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"thread_id": thread_id, "assistant_id": assistant_id, **kwargs})
        return {"run_id": "run-1", "thread_id": "thread-1", "status": "pending"}


class FakeLoopbackClient:
    def __init__(self) -> None:
        self.runs = FakeRuns()


def test_test_prompt_creates_stateless_playground_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLoopbackClient()
    seen_keys: list[str | None] = []

    def fake_loopback(api_key: str | None = None) -> FakeLoopbackClient:
        seen_keys.append(api_key)
        return fake

    monkeypatch.setattr("apex.routers.prompts.loopback_client", fake_loopback)
    created = create_prompt(client)

    response = client.post(
        f"/v1/prompts/{created['id']}/test",
        json={"sample_input": {"user": "try {title}", "title": "Demo"}},
        headers={"x-api-key": "caller-key"},
    )
    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1", "thread_id": "thread-1"}
    assert seen_keys == ["caller-key"]  # caller's key forwarded to the loopback client

    call = fake.runs.calls[0]
    assert call["thread_id"] is None  # stateless run
    assert call["assistant_id"] == "playground"
    assert call["input"]["prompt"] == {"system": "v1 content", "user": "try {title}"}
    assert call["input"]["sample_input"] == {"user": "try {title}", "title": "Demo"}
    assert call["metadata"]["purpose"] == "prompt_test"
    assert call["metadata"]["prompt_id"] == created["id"]
    assert call["on_completion"] == "keep"


def test_test_prompt_with_explicit_content_and_version(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLoopbackClient()
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)
    v1_id = created["active_version"]["id"]
    client.post(f"/v1/prompts/{created['id']}/versions", json={"content": "v2 content"})

    response = client.post(f"/v1/prompts/{created['id']}/test", json={"content": "inline draft"})
    assert response.status_code == 202
    assert fake.runs.calls[-1]["input"]["prompt"]["system"] == "inline draft"

    response = client.post(f"/v1/prompts/{created['id']}/test", json={"version_id": v1_id})
    assert response.status_code == 202
    assert fake.runs.calls[-1]["input"]["prompt"]["system"] == "v1 content"

    response = client.post(f"/v1/prompts/{created['id']}/test", json={"version_id": "nope"})
    assert response.status_code == 404
    assert client.post("/v1/prompts/nope/test", json={}).status_code == 404


def test_test_prompt_maps_loopback_failure_to_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingRuns:
        async def create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("loopback down")

    class ExplodingClient:
        runs = ExplodingRuns()

    monkeypatch.setattr(
        "apex.routers.prompts.loopback_client", lambda api_key=None: ExplodingClient()
    )
    created = create_prompt(client)
    response = client.post(f"/v1/prompts/{created['id']}/test", json={})
    assert response.status_code == 502
