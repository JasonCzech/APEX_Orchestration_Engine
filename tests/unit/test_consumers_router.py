"""/admin/consumers routes against an in-memory fake repository."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import hash_api_key
from apex.persistence.models import ApiConsumer, ConsumerScope
from apex.routers.consumers import get_consumers_repository, router

ADMIN = ConsumerIdentity(
    consumer_id="admin-1", name="root", consumer_type=ConsumerType.INTERNAL, role=Role.ADMIN
)
OPERATOR = ConsumerIdentity(
    consumer_id="op-1", name="op", consumer_type=ConsumerType.DASHBOARD, role=Role.OPERATOR
)


class FakeConsumersRepository:
    """In-memory stand-in matching ConsumersRepository's surface."""

    def __init__(self) -> None:
        self.rows: dict[str, ApiConsumer] = {}

    def seed(self, *, consumer_id: str, name: str, key_hash: str) -> ApiConsumer:
        consumer = ApiConsumer(
            id=consumer_id,
            name=name,
            consumer_type="headless",
            role="viewer",
            key_hash=key_hash,
            enabled=True,
            created_at=datetime.now(UTC),
            last_used_at=None,
        )
        consumer.scopes = []
        self.rows[consumer_id] = consumer
        return consumer

    async def list_all(self) -> list[ApiConsumer]:
        return list(self.rows.values())

    async def get(self, consumer_id: str) -> ApiConsumer | None:
        return self.rows.get(consumer_id)

    async def get_by_name(self, name: str) -> ApiConsumer | None:
        return next((c for c in self.rows.values() if c.name == name), None)

    async def create(
        self,
        *,
        name: str,
        consumer_type: str,
        role: str,
        key_hash: str,
        scopes: Sequence[ScopeRef] = (),
    ) -> ApiConsumer:
        consumer = ApiConsumer(
            id=uuid4().hex,
            name=name,
            consumer_type=consumer_type,
            role=role,
            key_hash=key_hash,
            enabled=True,
            created_at=datetime.now(UTC),
            last_used_at=None,
        )
        consumer.scopes = [
            ConsumerScope(id=uuid4().hex, project_id=s.project_id, app_id=s.app_id) for s in scopes
        ]
        self.rows[consumer.id] = consumer
        return consumer

    async def update(
        self,
        consumer_id: str,
        *,
        name: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
        scopes: Sequence[ScopeRef] | None = None,
    ) -> ApiConsumer | None:
        consumer = self.rows.get(consumer_id)
        if consumer is None:
            return None
        if name is not None:
            consumer.name = name
        if role is not None:
            consumer.role = role
        if enabled is not None:
            consumer.enabled = enabled
        if scopes is not None:
            consumer.scopes = [
                ConsumerScope(id=uuid4().hex, project_id=s.project_id, app_id=s.app_id)
                for s in scopes
            ]
        return consumer

    async def replace_key_hash(self, consumer_id: str, key_hash: str) -> ApiConsumer | None:
        consumer = self.rows.get(consumer_id)
        if consumer is None:
            return None
        consumer.key_hash = key_hash
        consumer.last_used_at = datetime.now(UTC)
        return consumer

    async def delete(self, consumer_id: str) -> bool:
        return self.rows.pop(consumer_id, None) is not None


def make_client(repo: FakeConsumersRepository, identity: ConsumerIdentity = ADMIN) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_consumers_repository] = lambda: repo
    app.dependency_overrides[get_current_identity] = lambda: identity
    return TestClient(app)


CREATE_BODY = {
    "name": "dashboard-ui",
    "consumer_type": "dashboard",
    "role": "operator",
    "scopes": [{"project_id": "proj-a", "app_id": None}],
}


def test_create_returns_raw_key_exactly_once_and_stores_only_hash() -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        response = client.post("/v1/admin/consumers", json=CREATE_BODY)
        assert response.status_code == 201
        body = response.json()
        api_key = body["api_key"]
        assert len(api_key) >= 32
        stored = repo.rows[body["id"]]
        assert stored.key_hash == hash_api_key(api_key)
        assert api_key != stored.key_hash  # raw key is not what's persisted
        assert body["key_fingerprint"] == stored.key_hash[:8]
        assert body["scopes"] == [{"project_id": "proj-a", "app_id": None}]
        # Never retrievable again: subsequent reads expose no api_key.
        read = client.get(f"/v1/admin/consumers/{body['id']}")
        assert read.status_code == 200
        assert "api_key" not in read.json()
        listing = client.get("/v1/admin/consumers")
        assert all("api_key" not in row for row in listing.json())


def test_create_duplicate_name_conflicts() -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        assert client.post("/v1/admin/consumers", json=CREATE_BODY).status_code == 201
        assert client.post("/v1/admin/consumers", json=CREATE_BODY).status_code == 409


def test_rotate_replaces_hash_and_returns_new_key_once() -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        created = client.post("/v1/admin/consumers", json=CREATE_BODY).json()
        old_hash = repo.rows[created["id"]].key_hash
        response = client.post(f"/v1/admin/consumers/{created['id']}/rotate")
        assert response.status_code == 200
        body = response.json()
        assert body["api_key"] != created["api_key"]
        new_hash = repo.rows[created["id"]].key_hash
        assert new_hash == hash_api_key(body["api_key"])
        assert new_hash != old_hash
        assert body["key_fingerprint"] == new_hash[:8]


def test_rotate_unknown_consumer_404() -> None:
    with make_client(FakeConsumersRepository()) as client:
        assert client.post("/v1/admin/consumers/nope/rotate").status_code == 404


def test_update_role_scopes_and_enabled() -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        created = client.post("/v1/admin/consumers", json=CREATE_BODY).json()
        response = client.patch(
            f"/v1/admin/consumers/{created['id']}",
            json={"role": "viewer", "enabled": False, "scopes": [{"project_id": "proj-b"}]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["role"] == "viewer"
        assert body["enabled"] is False
        assert body["scopes"] == [{"project_id": "proj-b", "app_id": None}]


def test_self_delete_conflicts_409() -> None:
    repo = FakeConsumersRepository()
    repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))
    with make_client(repo) as client:
        response = client.delete(f"/v1/admin/consumers/{ADMIN.consumer_id}")
        assert response.status_code == 409
        assert ADMIN.consumer_id in repo.rows


def test_self_disable_conflicts_409() -> None:
    repo = FakeConsumersRepository()
    repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))
    with make_client(repo) as client:
        response = client.patch(f"/v1/admin/consumers/{ADMIN.consumer_id}", json={"enabled": False})
        assert response.status_code == 409
        # Other field updates on yourself remain allowed.
        ok = client.patch(f"/v1/admin/consumers/{ADMIN.consumer_id}", json={"name": "root2"})
        assert ok.status_code == 200


def test_delete_other_consumer_and_404_when_missing() -> None:
    repo = FakeConsumersRepository()
    repo.seed(consumer_id="victim", name="victim", key_hash=hash_api_key("k2"))
    with make_client(repo) as client:
        assert client.delete("/v1/admin/consumers/victim").status_code == 204
        assert "victim" not in repo.rows
        assert client.delete("/v1/admin/consumers/victim").status_code == 404


def test_get_unknown_consumer_404() -> None:
    with make_client(FakeConsumersRepository()) as client:
        assert client.get("/v1/admin/consumers/nope").status_code == 404


def test_all_routes_are_admin_only() -> None:
    repo = FakeConsumersRepository()
    repo.seed(consumer_id="x", name="x", key_hash=hash_api_key("k3"))
    with make_client(repo, identity=OPERATOR) as client:
        assert client.get("/v1/admin/consumers").status_code == 403
        assert client.post("/v1/admin/consumers", json=CREATE_BODY).status_code == 403
        assert client.get("/v1/admin/consumers/x").status_code == 403
        assert client.patch("/v1/admin/consumers/x", json={"enabled": False}).status_code == 403
        assert client.delete("/v1/admin/consumers/x").status_code == 403
        assert client.post("/v1/admin/consumers/x/rotate").status_code == 403
