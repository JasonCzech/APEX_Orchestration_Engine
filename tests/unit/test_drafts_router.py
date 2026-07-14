"""/drafts CRUD + project scoping against an in-memory fake repository."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import Draft
from apex.routers.drafts import get_drafts_repository, router

ADMIN = ConsumerIdentity(
    consumer_id="admin-1", name="root", consumer_type=ConsumerType.INTERNAL, role=Role.ADMIN
)
ALICE = ConsumerIdentity(  # operator scoped to proj-a
    consumer_id="op-alice",
    name="alice",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.OPERATOR,
    scopes=[ScopeRef(project_id="proj-a")],
)
VIEWER = ConsumerIdentity(
    consumer_id="view-1",
    name="viewer",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.VIEWER,
    scopes=[ScopeRef(project_id="proj-a")],
)
APP_ONLY_ALICE = ConsumerIdentity(
    consumer_id="op-app-alice",
    name="alice",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.OPERATOR,
    scopes=[ScopeRef(project_id="proj-a", app_id="app-a")],
)


class FakeDraftsRepository:
    """In-memory stand-in matching DraftsRepository's surface."""

    def __init__(self) -> None:
        self.rows: dict[str, Draft] = {}
        self.for_update_calls: list[str] = []

    def seed(
        self,
        *,
        draft_id: str,
        title: str,
        project_id: str | None,
        created_by: str,
        created_by_consumer_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Draft:
        now = datetime.now(UTC) - timedelta(hours=1)
        draft = Draft(
            id=draft_id,
            title=title,
            project_id=project_id,
            payload=payload or {},
            created_by=created_by,
            created_by_consumer_id=created_by_consumer_id,
            created_at=now,
            updated_at=now,
        )
        self.rows[draft_id] = draft
        return draft

    async def list_all(self, *, project_id: str | None = None) -> list[Draft]:
        drafts = list(self.rows.values())
        if project_id is not None:
            drafts = [d for d in drafts if d.project_id == project_id]
        return drafts

    async def get(self, draft_id: str) -> Draft | None:
        return self.rows.get(draft_id)

    async def get_for_update(self, draft_id: str) -> Draft | None:
        self.for_update_calls.append(draft_id)
        return self.rows.get(draft_id)

    async def create(
        self,
        *,
        title: str,
        project_id: str | None,
        payload: dict[str, Any],
        created_by: str | None,
        created_by_consumer_id: str | None = None,
    ) -> Draft:
        now = datetime.now(UTC)
        draft = Draft(
            id=uuid4().hex,
            title=title,
            project_id=project_id,
            payload=payload,
            created_by=created_by,
            created_by_consumer_id=created_by_consumer_id,
            created_at=now,
            updated_at=now,
        )
        self.rows[draft.id] = draft
        return draft

    async def replace(
        self,
        draft_id: str,
        *,
        title: str,
        project_id: str | None,
        payload: dict[str, Any],
    ) -> Draft | None:
        draft = self.rows.get(draft_id)
        if draft is None:
            return None
        return await self.replace_existing(
            draft, title=title, project_id=project_id, payload=payload
        )

    async def replace_existing(
        self,
        draft: Draft,
        *,
        title: str,
        project_id: str | None,
        payload: dict[str, Any],
    ) -> Draft:
        draft.title = title
        draft.project_id = project_id
        draft.payload = payload
        draft.updated_at = datetime.now(UTC)
        return draft

    async def delete(self, draft_id: str) -> bool:
        return self.rows.pop(draft_id, None) is not None

    async def delete_existing(self, draft: Draft) -> bool:
        return self.rows.pop(draft.id, None) is not None


def make_client(repo: FakeDraftsRepository, identity: ConsumerIdentity) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_drafts_repository] = lambda: repo
    app.dependency_overrides[get_current_identity] = lambda: identity
    return TestClient(app)


def seeded_repo() -> FakeDraftsRepository:
    repo = FakeDraftsRepository()
    repo.seed(draft_id="d-proj-a", title="A", project_id="proj-a", created_by="bob")
    repo.seed(draft_id="d-proj-b", title="B", project_id="proj-b", created_by="bob")
    repo.seed(draft_id="d-global", title="G", project_id=None, created_by="bob")
    repo.seed(
        draft_id="d-own-b",
        title="Mine",
        project_id="proj-b",
        created_by="alice",
        created_by_consumer_id="op-alice",
    )
    return repo


def test_scoped_consumer_sees_only_current_scope_and_global_drafts() -> None:
    with make_client(seeded_repo(), ALICE) as client:
        response = client.get("/v1/drafts")
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert ids == {"d-proj-a", "d-global"}  # creator status cannot bypass revoked scope


def test_unscoped_admin_sees_all_drafts() -> None:
    with make_client(seeded_repo(), ADMIN) as client:
        ids = {row["id"] for row in client.get("/v1/drafts").json()}
    assert ids == {"d-proj-a", "d-proj-b", "d-global", "d-own-b"}


def test_list_with_project_filter() -> None:
    with make_client(seeded_repo(), ALICE) as client:
        rows = client.get("/v1/drafts", params={"project": "proj-a"}).json()
    assert [row["id"] for row in rows] == ["d-proj-a"]


def test_create_sets_created_by_from_identity() -> None:
    repo = FakeDraftsRepository()
    with make_client(repo, ALICE) as client:
        response = client.post(
            "/v1/drafts",
            json={"title": "wizard", "project_id": "proj-a", "payload": {"step": 2}},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["created_by"] == "alice"
    assert body["payload"] == {"step": 2}
    assert repo.rows[body["id"]].project_id == "proj-a"
    assert repo.rows[body["id"]].created_by_consumer_id == "op-alice"


def test_create_outside_scope_403() -> None:
    with make_client(FakeDraftsRepository(), ALICE) as client:
        response = client.post("/v1/drafts", json={"title": "x", "project_id": "proj-b"})
    assert response.status_code == 403


def test_scoped_operator_cannot_create_global_draft() -> None:
    with make_client(FakeDraftsRepository(), ALICE) as client:
        response = client.post("/v1/drafts", json={"title": "global"})
    assert response.status_code == 403


def test_app_only_operator_cannot_create_or_mutate_project_wide_draft() -> None:
    repo = seeded_repo()
    with make_client(repo, APP_ONLY_ALICE) as client:
        assert (
            client.post(
                "/v1/drafts", json={"title": "app draft", "project_id": "proj-a"}
            ).status_code
            == 403
        )
        assert client.get("/v1/drafts/d-proj-a").status_code == 200
        assert (
            client.put("/v1/drafts/d-proj-a", json={"title": "taken", "payload": {}}).status_code
            == 404
        )
        assert client.delete("/v1/drafts/d-proj-a").status_code == 404


def test_global_draft_mutation_requires_unscoped_admin() -> None:
    repo = seeded_repo()
    with make_client(repo, ALICE) as client:
        assert (
            client.put("/v1/drafts/d-global", json={"title": "taken", "payload": {}}).status_code
            == 404
        )
        assert client.delete("/v1/drafts/d-global").status_code == 404

    with make_client(repo, ADMIN) as client:
        assert (
            client.put("/v1/drafts/d-global", json={"title": "admin", "payload": {}}).status_code
            == 200
        )
        assert client.delete("/v1/drafts/d-global").status_code == 204


def test_draft_creator_visibility_uses_consumer_id_not_display_name() -> None:
    same_name = ConsumerIdentity(
        consumer_id="someone-else",
        name="alice",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="proj-a")],
    )
    with make_client(seeded_repo(), same_name) as client:
        assert client.get("/v1/drafts/d-own-b").status_code == 404


def test_draft_creator_loses_read_access_when_project_scope_is_revoked() -> None:
    revoked = ConsumerIdentity(
        consumer_id="op-alice",
        name="alice",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="proj-a")],
    )
    with make_client(seeded_repo(), revoked) as client:
        assert client.get("/v1/drafts/d-own-b").status_code == 404


def test_get_out_of_scope_draft_is_404() -> None:
    with make_client(seeded_repo(), ALICE) as client:
        assert client.get("/v1/drafts/d-proj-b").status_code == 404
        assert client.get("/v1/drafts/d-proj-a").status_code == 200
        assert client.get("/v1/drafts/missing").status_code == 404


def test_update_replaces_title_payload_and_bumps_updated_at() -> None:
    repo = seeded_repo()
    before = repo.rows["d-proj-a"].updated_at
    with make_client(repo, ALICE) as client:
        response = client.put(
            "/v1/drafts/d-proj-a", json={"title": "A2", "payload": {"fresh": True}}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "A2"
    assert body["payload"] == {"fresh": True}
    assert body["project_id"] == "proj-a"
    assert repo.rows["d-proj-a"].updated_at > before
    assert repo.for_update_calls == ["d-proj-a"]


def test_update_project_requires_scope_on_both_old_and_new_project() -> None:
    repo = seeded_repo()
    with make_client(repo, ALICE) as client:
        response = client.put(
            "/v1/drafts/d-proj-a",
            json={"title": "moved", "project_id": "proj-b", "payload": {}},
        )
    assert response.status_code == 403
    assert repo.rows["d-proj-a"].project_id == "proj-a"

    with make_client(repo, ADMIN) as client:
        response = client.put(
            "/v1/drafts/d-proj-a",
            json={"title": "moved", "project_id": "proj-b", "payload": {}},
        )
    assert response.status_code == 200
    assert repo.rows["d-proj-a"].project_id == "proj-b"


def test_update_out_of_scope_draft_is_404() -> None:
    with make_client(seeded_repo(), ALICE) as client:
        response = client.put("/v1/drafts/d-proj-b", json={"title": "x", "payload": {}})
    assert response.status_code == 404


def test_delete_draft() -> None:
    repo = seeded_repo()
    with make_client(repo, ALICE) as client:
        assert client.delete("/v1/drafts/d-proj-a").status_code == 204
        assert "d-proj-a" not in repo.rows
        assert client.delete("/v1/drafts/d-proj-a").status_code == 404
        assert client.delete("/v1/drafts/d-proj-b").status_code == 404  # out of scope
    assert repo.for_update_calls == ["d-proj-a", "d-proj-a", "d-proj-b"]


def test_mutations_require_operator_role() -> None:
    repo = seeded_repo()
    with make_client(repo, VIEWER) as client:
        assert client.get("/v1/drafts").status_code == 200  # reads allowed
        assert client.post("/v1/drafts", json={"title": "x"}).status_code == 403
        put = client.put("/v1/drafts/d-proj-a", json={"title": "x", "payload": {}})
        assert put.status_code == 403
        assert client.delete("/v1/drafts/d-proj-a").status_code == 403
