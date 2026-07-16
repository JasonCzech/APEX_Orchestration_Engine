"""/prompts router: happy paths, error mapping, and role gating.

The DB is replaced wholesale via dependency_overrides (fake-backed catalog
service + synthetic identity); the loopback LangGraph client is monkeypatched.
"""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langgraph_sdk.errors import UnprocessableEntityError

from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.input_limits import MAX_DESCRIPTION_CHARS
from apex.routers.prompts import (
    RollbackRequest,
    get_catalog,
    router,
)
from apex.routers.prompts import (
    TestPromptRequest as PromptTestRequest,
)
from apex.services.prompts import PromptCatalogService
from apex.services.run_validation import MAX_PROMPT_PART_CHARS_HARD
from tests.unit.test_prompts_service import FakePromptRepository


@pytest.mark.parametrize("model", [RollbackRequest, PromptTestRequest])
def test_prompt_version_body_models_reject_nul(model: type[Any]) -> None:
    with pytest.raises(ValueError, match="string_pattern_mismatch"):
        model.model_validate({"version_id": "version\x00id"})


def identity(role: Role, scopes: list[ScopeRef] | None = None) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="tester",
        consumer_type=ConsumerType.INTERNAL,
        role=role,
        scopes=scopes or [],
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
    app.dependency_overrides[get_current_identity] = lambda: identity(Role.ADMIN)
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


def test_prompt_lists_reject_huge_offset_before_repository(
    client: TestClient, repo: FakePromptRepository
) -> None:
    created = create_prompt(client)
    repo.search_calls = 0
    repo.list_version_calls = 0

    prompts = client.get("/v1/prompts", params={"offset": 10_001})
    versions = client.get(f"/v1/prompts/{created['id']}/versions", params={"offset": 10_001})

    assert prompts.status_code == 422
    assert versions.status_code == 422
    assert repo.search_calls == 0
    assert repo.list_version_calls == 0


def test_duplicate_create_conflicts(client: TestClient) -> None:
    create_prompt(client)
    response = client.post(
        "/v1/prompts",
        json={"namespace": "phase", "key": "story_analysis/system", "content": "again"},
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "override",
    [
        {"namespace": "n" * 256},
        {"key": "k" * 256},
        {"description": "d" * 20_001},
        {"content": "c" * 100_001},
        {"note": "n" * 20_001},
    ],
)
def test_create_rejects_oversized_catalog_fields_before_write(
    client: TestClient,
    repo: FakePromptRepository,
    override: dict[str, str],
) -> None:
    body = {"namespace": "phase", "key": "key", "content": "content", **override}

    response = client.post("/v1/prompts", json=body)

    assert response.status_code == 422
    assert repo.prompts == {}


def test_prompt_catalog_mutations_reject_credentials_without_reflection(
    client: TestClient,
    repo: FakePromptRepository,
) -> None:
    canary = "prompt-router-secret-canary"
    create = client.post(
        "/v1/prompts",
        json={
            "namespace": "phase",
            "key": "unsafe",
            "content": f"Authorization: Bearer {canary}",
        },
    )
    safe = create_prompt(client, key="safe", content="safe")
    version = client.post(
        f"/v1/prompts/{safe['id']}/versions",
        json={"content": f"database_uri={canary}"},
    )

    assert create.status_code == 422
    assert version.status_code == 422
    assert canary not in create.text
    assert canary not in version.text
    assert len(repo.prompts) == 1
    assert len(repo.versions) == 1


def test_prompt_reads_redact_legacy_credential_material(
    client: TestClient,
    repo: FakePromptRepository,
) -> None:
    canary = "legacy-prompt-read-secret-canary"
    created = create_prompt(client, key="safe", content="safe")
    version_id = created["active_version"]["id"]
    repo.versions[version_id].content = f"Authorization: Bearer {canary}"
    repo.versions[version_id].note = f"password={canary}"

    detail = client.get(f"/v1/prompts/{created['id']}")
    version = client.get(f"/v1/prompts/{created['id']}/versions/{version_id}")

    assert detail.status_code == 200
    assert version.status_code == 200
    assert canary not in detail.text
    assert canary not in version.text
    assert "[REDACTED]" in detail.text
    assert "[REDACTED]" in version.text


def test_prompt_reads_bound_oversized_legacy_text_fields(
    client: TestClient,
    repo: FakePromptRepository,
) -> None:
    canary = "oversized-legacy-prompt-secret-canary"
    created = create_prompt(client, key="safe", content="safe")
    prompt = repo.prompts[created["id"]]
    version_id = created["active_version"]["id"]
    version = repo.versions[version_id]
    prompt.namespace = f"password={canary}" + "n" * 1_000
    prompt.key = "k" * 1_000
    prompt.description = "d" * (MAX_DESCRIPTION_CHARS + 1_000)
    version.content = "c" * (MAX_PROMPT_PART_CHARS_HARD + 1_000)
    version.note = "n" * (MAX_DESCRIPTION_CHARS + 1_000)
    version.created_by = "u" * 1_000

    listed = client.get("/v1/prompts")
    detail = client.get(f"/v1/prompts/{created['id']}")
    versions = client.get(f"/v1/prompts/{created['id']}/versions")
    version_detail = client.get(f"/v1/prompts/{created['id']}/versions/{version_id}")

    assert all(
        response.status_code == 200 for response in (listed, detail, versions, version_detail)
    )
    summary = listed.json()[0]
    assert len(summary["namespace"]) <= 255
    assert len(summary["key"]) <= 255
    assert len(summary["description"]) <= MAX_DESCRIPTION_CHARS
    assert len(detail.json()["content"]) <= MAX_PROMPT_PART_CHARS_HARD
    assert len(detail.json()["note"]) <= MAX_DESCRIPTION_CHARS
    assert len(versions.json()[0]["created_by"]) <= 255
    assert len(version_detail.json()["content"]) <= MAX_PROMPT_PART_CHARS_HARD
    rendered = "".join(response.text for response in (listed, detail, versions, version_detail))
    assert canary not in rendered
    assert "[REDACTED]" in rendered


def test_prompt_and_version_lists_are_paginated(client: TestClient) -> None:
    first = create_prompt(client, key="a")
    create_prompt(client, key="b")
    create_prompt(client, key="c")

    listed = client.get("/v1/prompts", params={"limit": 1, "offset": 1})
    client.post(f"/v1/prompts/{first['id']}/versions", json={"content": "v2"})
    client.post(f"/v1/prompts/{first['id']}/versions", json={"content": "v3"})
    versions = client.get(f"/v1/prompts/{first['id']}/versions", params={"limit": 1, "offset": 1})

    assert [row["key"] for row in listed.json()] == ["b"]
    assert [row["version"] for row in versions.json()] == [2]


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


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.ADMIN])
def test_scoped_identity_cannot_mutate_global_prompt_catalog(app: FastAPI, role: Role) -> None:
    app.dependency_overrides[get_current_identity] = lambda: identity(
        role, [ScopeRef(project_id="p1", app_id="a1")]
    )
    body = {"namespace": "n", "key": "k", "content": "c"}
    with TestClient(app) as client:
        assert client.get("/v1/prompts").status_code == 200
        assert client.post("/v1/prompts", json=body).status_code == 403
        assert client.post("/v1/prompts/x/versions", json={"content": "c"}).status_code == 403
        assert client.post("/v1/prompts/x/rollback", json={"version_id": "v"}).status_code == 403
        assert client.post("/v1/prompts/x/archive").status_code == 403
        assert client.post("/v1/prompts/x/unarchive").status_code == 403


@pytest.mark.parametrize(
    ("role", "scopes"),
    [
        (Role.VIEWER, []),
        (Role.OPERATOR, []),
        (Role.VIEWER, [ScopeRef(project_id="p1", app_id="a1")]),
        (Role.OPERATOR, [ScopeRef(project_id="p1", app_id="a1")]),
        (Role.ADMIN, [ScopeRef(project_id="p1", app_id="a1")]),
    ],
)
def test_application_prompt_catalog_reads_are_hidden_from_non_platform_admins(
    client: TestClient,
    app: FastAPI,
    role: Role,
    scopes: list[ScopeRef],
) -> None:
    phase = create_prompt(client, key="story_analysis/system")
    application = create_prompt(
        client,
        namespace="application",
        key="a1",
        content="application-only requirements",
    )
    version_id = application["active_version"]["id"]
    app.dependency_overrides[get_current_identity] = lambda: identity(role, scopes)

    listed = client.get("/v1/prompts")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [phase["id"]]
    assert client.get("/v1/prompts", params={"namespace": "application"}).json() == []
    assert client.get(f"/v1/prompts/{application['id']}").status_code == 404
    assert client.get(f"/v1/prompts/{application['id']}/versions").status_code == 404
    assert client.get(f"/v1/prompts/{application['id']}/versions/{version_id}").status_code == 404

    # Deployment-global phase templates retain the existing authenticated-read policy.
    assert client.get(f"/v1/prompts/{phase['id']}").status_code == 200
    assert client.get(f"/v1/prompts/{phase['id']}/versions").status_code == 200


def test_unscoped_platform_admin_can_read_application_prompt_catalog(
    client: TestClient,
) -> None:
    application = create_prompt(
        client,
        namespace="application",
        key="a1",
        content="application-only requirements",
    )
    version_id = application["active_version"]["id"]

    listed = client.get("/v1/prompts", params={"namespace": "application"})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [application["id"]]
    assert client.get(f"/v1/prompts/{application['id']}").status_code == 200
    assert client.get(f"/v1/prompts/{application['id']}/versions").status_code == 200
    version = client.get(f"/v1/prompts/{application['id']}/versions/{version_id}")
    assert version.status_code == 200
    assert version.json()["content"] == "application-only requirements"


class FakeRuns:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.result: dict[str, Any] = {
            "run_id": "run-1",
            "thread_id": "thread-1",
            "status": "pending",
        }

    async def create(self, thread_id: Any, assistant_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"thread_id": thread_id, "assistant_id": assistant_id, **kwargs})
        if self.error is not None:
            raise self.error
        return self.result


class FakeThreads:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []
        self.result: dict[str, Any] = {"thread_id": "thread-1"}

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        return {**self.result, "metadata": kwargs.get("metadata") or {}}

    async def delete(self, thread_id: str) -> None:
        self.delete_calls.append(thread_id)


class FakeLoopbackClient:
    def __init__(self) -> None:
        self.runs = FakeRuns()
        self.threads = FakeThreads()


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
    assert call["thread_id"] == "thread-1"
    assert call["assistant_id"] == "playground"
    assert call["input"]["prompt"] == {"system": "v1 content", "user": "try {title}"}
    assert call["input"]["sample_input"] == {"user": "try {title}", "title": "Demo"}
    assert call["metadata"]["purpose"] == "prompt_test"
    assert call["metadata"]["prompt_id"] == created["id"]
    thread_call = fake.threads.create_calls[0]
    assert thread_call["metadata"]["purpose"] == "prompt_test"
    assert thread_call["ttl"] == {"strategy": "delete", "ttl": 1440}


@pytest.mark.parametrize("boundary", ["thread", "run"])
def test_test_prompt_rejects_credential_shaped_native_ids(
    boundary: str,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLoopbackClient()
    canary = "prompt-native-id-secret-canary"
    if boundary == "thread":
        fake.threads.result = {"thread_id": f"password={canary}"}
    else:
        fake.runs.result = {"run_id": f"Authorization: Bearer {canary}"}
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)

    response = client.post(f"/v1/prompts/{created['id']}/test", json={})

    assert response.status_code == 502
    assert canary not in response.text
    assert len(fake.runs.calls) == (0 if boundary == "thread" else 1)


@pytest.mark.parametrize("boundary", ["thread", "run"])
def test_test_prompt_rejects_hostile_native_mapping_subclasses_without_hooks(
    boundary: str,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HostileMapping(dict[str, Any]):
        called = False

        def get(self, key: str, default: Any = None) -> Any:
            self.called = True
            raise AssertionError("provider mapping hooks must not run")

    hostile = HostileMapping(
        thread_id="thread-1" if boundary == "thread" else None,
        run_id="run-1" if boundary == "run" else None,
    )

    class Threads(FakeThreads):
        async def create(self, **kwargs: Any) -> dict[str, Any]:
            if boundary == "thread":
                return hostile
            return await super().create(**kwargs)

    class Runs(FakeRuns):
        async def create(self, thread_id: Any, assistant_id: str, **kwargs: Any) -> dict[str, Any]:
            if boundary == "run":
                return hostile
            return await super().create(thread_id, assistant_id, **kwargs)

    fake = FakeLoopbackClient()
    fake.threads = Threads()
    fake.runs = Runs()
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)

    response = client.post(f"/v1/prompts/{created['id']}/test", json={})

    assert response.status_code == 502
    assert hostile.called is False


def test_test_prompt_does_not_trust_provider_http_exception(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "prompt-provider-http-secret-canary"
    fake = FakeLoopbackClient()
    fake.runs.error = HTTPException(status_code=418, detail=canary)
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)

    response = client.post(f"/v1/prompts/{created['id']}/test", json={})

    assert response.status_code == 502
    assert response.json()["detail"] == "failed to create playground run"
    assert canary not in response.text


def test_test_prompt_does_not_read_hostile_exception_type_metadata(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "prompt-exception-metaclass-secret-canary"

    class HostileExceptionMeta(type):
        @property
        def __name__(cls) -> str:  # type: ignore[reportIncompatibleVariableOverride]
            raise AssertionError(secret)

    class HostileProviderError(Exception, metaclass=HostileExceptionMeta):
        pass

    class Threads(FakeThreads):
        async def create(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            raise HostileProviderError("provider failed")

    fake = FakeLoopbackClient()
    fake.threads = Threads()
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)

    response = client.post(f"/v1/prompts/{created['id']}/test", json={})

    assert response.status_code == 502
    assert response.json()["detail"] == "failed to create playground thread"
    assert secret not in response.text


def test_test_prompt_rejects_credentials_before_scratch_thread_creation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLoopbackClient()
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)

    response = client.post(
        f"/v1/prompts/{created['id']}/test",
        json={"content": "Authorization: Bearer prompt-test-secret-canary"},
    )

    assert response.status_code == 422
    assert "prompt-test-secret-canary" not in response.text
    assert fake.threads.create_calls == []
    assert fake.runs.calls == []


def test_test_prompt_scoped_operator_stamps_exact_thread_and_run_scope(
    client: TestClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLoopbackClient()
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)
    app.dependency_overrides[get_current_identity] = lambda: identity(
        Role.OPERATOR,
        [ScopeRef(project_id="p1", app_id="a1")],
    )

    response = client.post(f"/v1/prompts/{created['id']}/test", json={})

    assert response.status_code == 202
    thread_call = fake.threads.create_calls[0]
    assert thread_call["metadata"]["project_id"] == "p1"
    assert thread_call["metadata"]["app_id"] == "a1"
    run_call = fake.runs.calls[0]
    assert run_call["metadata"]["project_id"] == "p1"
    assert run_call["metadata"]["app_id"] == "a1"
    assert run_call["input"]["project_id"] == "p1"
    assert run_call["input"]["app_id"] == "a1"
    assert run_call["config"]["configurable"] == {"project_id": "p1", "app_id": "a1"}


def test_test_prompt_rejects_ambiguous_scoped_operator_before_loopback(
    client: TestClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLoopbackClient()
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)
    app.dependency_overrides[get_current_identity] = lambda: identity(
        Role.OPERATOR,
        [ScopeRef(project_id="p1"), ScopeRef(project_id="p2")],
    )

    response = client.post(f"/v1/prompts/{created['id']}/test", json={})

    assert response.status_code == 422
    assert fake.threads.create_calls == []
    assert fake.runs.calls == []


def test_test_prompt_rejects_out_of_scope_selection_before_loopback(
    client: TestClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLoopbackClient()
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)
    app.dependency_overrides[get_current_identity] = lambda: identity(
        Role.OPERATOR,
        [ScopeRef(project_id="p1", app_id="a1")],
    )

    response = client.post(
        f"/v1/prompts/{created['id']}/test",
        json={"project_id": "p2", "app_id": "a2"},
    )

    assert response.status_code == 403
    assert fake.threads.create_calls == []
    assert fake.runs.calls == []


def test_test_prompt_cleans_scratch_thread_after_definitive_run_rejection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLoopbackClient()
    request = httpx.Request("POST", "http://langgraph/threads/thread-1/runs")
    fake.runs.error = UnprocessableEntityError(
        "invalid run",
        response=httpx.Response(422, request=request),
        body={"detail": "invalid"},
    )
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)

    response = client.post(f"/v1/prompts/{created['id']}/test", json={})

    assert response.status_code == 502
    assert fake.threads.delete_calls == ["thread-1"]


def test_test_prompt_rejects_render_amplification_before_run_creation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLoopbackClient()
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    created = create_prompt(client)

    response = client.post(
        f"/v1/prompts/{created['id']}/test",
        json={
            "content": "{value}" * 1_000,
            "sample_input": {"value": "x" * 100},
        },
    )

    assert response.status_code == 422
    assert fake.runs.calls == []


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


def test_scoped_operator_cannot_test_application_catalog_prompt(
    client: TestClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLoopbackClient()
    monkeypatch.setattr("apex.routers.prompts.loopback_client", lambda api_key=None: fake)
    application = create_prompt(
        client,
        namespace="application",
        key="a1",
        content="application-only requirements",
    )
    app.dependency_overrides[get_current_identity] = lambda: identity(
        Role.OPERATOR,
        [ScopeRef(project_id="p1", app_id="a1")],
    )

    response = client.post(f"/v1/prompts/{application['id']}/test", json={})

    assert response.status_code == 404
    assert fake.runs.calls == []


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
