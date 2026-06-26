"""Admin connections router: CRUD, provider validation, host mappings, probe."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.integrations import SecretValue
from apex.persistence.models import Connection, HostMapping
from apex.persistence.repositories.connections import DuplicateConnectionNameError
from apex.routers.connections import get_connections_repository, router


def _now() -> datetime:
    return datetime.now(UTC)


def _make_connection(**kwargs: Any) -> Connection:
    conn = Connection(
        id=uuid4().hex,
        kind=kwargs["kind"],
        provider=kwargs["provider"],
        name=kwargs["name"],
        project_id=kwargs.get("project_id"),
        base_url=kwargs.get("base_url"),
        options=dict(kwargs.get("options") or {}),
        secret_ref=kwargs.get("secret_ref"),
    )
    conn.enabled = True
    conn.created_at = _now()
    conn.updated_at = _now()
    conn.host_mappings = []
    return conn


class FakeConnectionsRepository:
    """In-memory stand-in mirroring ConnectionsRepository semantics."""

    def __init__(self) -> None:
        self.connections: dict[str, Connection] = {}

    async def list_connections(
        self, *, kind: str | None = None, project: str | None = None
    ) -> list[Connection]:
        rows = list(self.connections.values())
        if kind is not None:
            rows = [r for r in rows if r.kind == kind]
        if project is not None:
            rows = [r for r in rows if r.project_id == project]
        return sorted(rows, key=lambda r: (r.kind, r.name))

    async def get(self, connection_id: str) -> Connection | None:
        return self.connections.get(connection_id)

    async def create(self, **fields: Any) -> Connection:
        if any(r.name == fields["name"] for r in self.connections.values()):
            raise DuplicateConnectionNameError(fields["name"])
        conn = _make_connection(**fields)
        self.connections[conn.id] = conn
        return conn

    async def update(self, conn: Connection, changes: dict[str, Any]) -> Connection:
        new_name = changes.get("name")
        if new_name is not None and any(
            r.id != conn.id and r.name == new_name for r in self.connections.values()
        ):
            raise DuplicateConnectionNameError(new_name)
        for field, value in changes.items():
            setattr(conn, field, value)
        conn.updated_at = _now()
        return conn

    async def set_enabled(self, conn: Connection, enabled: bool) -> Connection:
        conn.enabled = enabled
        conn.updated_at = _now()
        return conn

    async def delete(self, conn: Connection) -> None:
        self.connections.pop(conn.id, None)

    async def replace_host_mappings(
        self, conn: Connection, mappings: list[dict[str, Any]]
    ) -> Connection:
        conn.host_mappings = [
            HostMapping(
                id=uuid4().hex,
                pattern=m["pattern"],
                target=m["target"],
                enabled=bool(m.get("enabled", True)),
            )
            for m in mappings
        ]
        return conn


def identity(role: Role, scopes: list[ScopeRef] | None = None) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c-test",
        name="test",
        consumer_type=ConsumerType.INTERNAL,
        role=role,
        scopes=scopes or [],
    )


def make_client(repo: FakeConnectionsRepository, who: ConsumerIdentity) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_connections_repository] = lambda: repo
    app.dependency_overrides[get_current_identity] = lambda: who
    return TestClient(app)


@pytest.fixture
def repo() -> FakeConnectionsRepository:
    return FakeConnectionsRepository()


@pytest.fixture
def admin(repo: FakeConnectionsRepository) -> TestClient:
    return make_client(repo, identity(Role.ADMIN))


# ── role gating: every route is admin-only ───────────────────────────────────


def test_admin_connections_rejects_non_admin(repo: FakeConnectionsRepository) -> None:
    operator = make_client(repo, identity(Role.OPERATOR))
    assert operator.get("/admin/connections").status_code == 403  # even GET
    assert (
        operator.post(
            "/admin/connections",
            json={"kind": "work_tracking", "provider": "stub", "name": "x"},
        ).status_code
        == 403
    )
    viewer = make_client(repo, identity(Role.VIEWER))
    assert viewer.get("/admin/connections").status_code == 403


# ── CRUD ─────────────────────────────────────────────────────────────────────


def test_create_list_get_connection(admin: TestClient) -> None:
    created = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "jira-demo",
            "project_id": "demo",
            "secret_ref": "env:JIRA_TOKEN",
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["enabled"] is True
    # secret_ref is a REFERENCE string ("env:NAME"), never a raw secret — safe to return
    assert body["secret_ref"] == "env:JIRA_TOKEN"

    listed = admin.get("/admin/connections").json()
    assert [c["name"] for c in listed] == ["jira-demo"]
    assert (
        admin.get("/admin/connections", params={"kind": "work_tracking", "project": "demo"}).json()
        == listed
    )
    assert admin.get("/admin/connections", params={"kind": "log_search"}).json() == []
    assert admin.get(f"/admin/connections/{body['id']}").json()["name"] == "jira-demo"


def test_create_unknown_provider_is_422_with_registered_list(admin: TestClient) -> None:
    response = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "definitely-not-real", "name": "x"},
    )
    assert response.status_code == 422
    detail = response.text
    assert "definitely-not-real" in detail
    assert "stub" in detail  # the registered providers are listed back


def test_patch_validates_provider_against_existing_kind(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "execution_engine", "provider": "sim", "name": "engine-1"},
    ).json()["id"]
    assert (
        admin.patch(f"/admin/connections/{conn_id}", json={"provider": "nope"}).status_code == 422
    )
    patched = admin.patch(f"/admin/connections/{conn_id}", json={"options": {"duration_s": 1.0}})
    assert patched.status_code == 200
    assert patched.json()["options"] == {"duration_s": 1.0}


def test_duplicate_connection_name_is_409(admin: TestClient) -> None:
    payload = {"kind": "work_tracking", "provider": "stub", "name": "dupe"}
    assert admin.post("/admin/connections", json=payload).status_code == 201
    assert admin.post("/admin/connections", json=payload).status_code == 409


def test_scoped_admin_cannot_create_global_or_out_of_scope_connection(
    repo: FakeConnectionsRepository,
) -> None:
    client = make_client(repo, identity(Role.ADMIN, [ScopeRef(project_id="demo")]))

    assert (
        client.post(
            "/admin/connections",
            json={"kind": "work_tracking", "provider": "stub", "name": "global"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/admin/connections",
            json={
                "kind": "work_tracking",
                "provider": "stub",
                "name": "other",
                "project_id": "other",
            },
        ).status_code
        == 403
    )


def test_scoped_admin_lists_only_in_scope_connections(repo: FakeConnectionsRepository) -> None:
    client = make_client(repo, identity(Role.ADMIN, [ScopeRef(project_id="demo")]))
    repo.connections["global"] = _make_connection(
        id="global", kind="work_tracking", provider="stub", name="global"
    )
    repo.connections["demo"] = _make_connection(
        id="demo", kind="work_tracking", provider="stub", name="demo", project_id="demo"
    )
    repo.connections["other"] = _make_connection(
        id="other", kind="work_tracking", provider="stub", name="other", project_id="other"
    )

    listed = client.get("/admin/connections").json()
    assert [row["name"] for row in listed] == ["demo"]
    assert client.get("/admin/connections/demo").status_code == 200
    assert client.get("/admin/connections/global").status_code == 403
    assert client.get("/admin/connections/other").status_code == 403


def test_create_rejects_private_base_url(admin: TestClient) -> None:
    response = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "private-create",
            "base_url": "http://127.0.0.1:9200",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "private adapter hosts are disabled"


def test_create_rejects_metadata_hostname(admin: TestClient) -> None:
    response = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "metadata-create",
            "base_url": "http://metadata.google.internal/computeMetadata/v1",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "private adapter hosts are disabled"


def test_create_rejects_hostname_resolving_private(
    admin: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [(None, None, None, "", ("127.0.0.1", 9200))]

    monkeypatch.setattr("apex.services.connections.socket.getaddrinfo", fake_getaddrinfo)

    response = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "dns-private-create",
            "base_url": "https://internal.example.test",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "private adapter hosts are disabled"


def test_patch_rejects_private_base_url(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "patch-private"},
    ).json()["id"]

    response = admin.patch(
        f"/admin/connections/{conn_id}", json={"base_url": "http://169.254.169.254"}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "private adapter hosts are disabled"


def test_enable_disable_and_delete(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "toggle-me"},
    ).json()["id"]
    assert admin.post(f"/admin/connections/{conn_id}/disable").json()["enabled"] is False
    assert admin.post(f"/admin/connections/{conn_id}/enable").json()["enabled"] is True
    assert admin.delete(f"/admin/connections/{conn_id}").status_code == 204
    assert admin.get(f"/admin/connections/{conn_id}").status_code == 404


def test_host_mappings_put_replaces_full_list(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "mapped"},
    ).json()["id"]
    assert admin.get(f"/admin/connections/{conn_id}/host-mappings").json() == []

    put_two = admin.put(
        f"/admin/connections/{conn_id}/host-mappings",
        json=[
            {"pattern": "*.staging.local", "target": "10.0.0.1"},
            {"pattern": "db.staging.local", "target": "10.0.0.2", "enabled": False},
        ],
    )
    assert put_two.status_code == 200
    assert [m["target"] for m in put_two.json()] == ["10.0.0.1", "10.0.0.2"]
    assert put_two.json()[1]["enabled"] is False

    put_one = admin.put(
        f"/admin/connections/{conn_id}/host-mappings",
        json=[{"pattern": "*", "target": "10.9.9.9"}],
    )
    assert [m["target"] for m in put_one.json()] == ["10.9.9.9"]
    assert len(admin.get(f"/admin/connections/{conn_id}/host-mappings").json()) == 1


# ── probe ────────────────────────────────────────────────────────────────────


def test_probe_ok_for_stub_connection(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "probe-wt"},
    ).json()["id"]
    response = admin.post(f"/admin/connections/{conn_id}/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["latency_ms"] >= 0
    assert "PHX-241" in body["detail"]


def test_probe_ok_for_sim_engine(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "execution_engine", "provider": "sim", "name": "probe-sim"},
    ).json()["id"]
    body = admin.post(f"/admin/connections/{conn_id}/test").json()
    assert body["ok"] is True


@pytest.fixture
def broken_provider() -> Iterator[str]:
    """Temporarily register a provider whose probe call always explodes."""
    provider = "test-broken"

    class BrokenAdapter:
        def __init__(
            self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
        ) -> None:
            self._conn = conn

        async def get_item(self, key: str) -> Any:
            raise RuntimeError("backend exploded")

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(BrokenAdapter)
    try:
        yield provider
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)


def test_probe_failure_reports_ok_false_not_5xx(admin: TestClient, broken_provider: str) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": broken_provider, "name": "probe-broken"},
    ).json()["id"]
    response = admin.post(f"/admin/connections/{conn_id}/test")
    assert response.status_code == 200  # failures are inline, never 5xx
    body = response.json()
    assert body["ok"] is False
    assert body["detail"] == "connection probe failed; check server logs for details"
    assert "backend exploded" not in body["detail"]


def test_probe_rejects_private_base_url(admin: TestClient, repo: FakeConnectionsRepository) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "probe-private",
        },
    ).json()["id"]
    repo.connections[conn_id].base_url = "http://127.0.0.1:9200"

    response = admin.post(f"/admin/connections/{conn_id}/test")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["detail"] == "private adapter hosts are disabled"


def test_probe_unknown_connection_is_404(admin: TestClient) -> None:
    assert admin.post("/admin/connections/nope/test").status_code == 404
