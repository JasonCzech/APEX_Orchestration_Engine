"""Inventory router: latest snapshots, staleness, scoping, and inline rescans."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.integrations import EnvironmentSnapshot as DomainSnapshot
from apex.domain.integrations import EnvRef, ServiceInfo
from apex.persistence.models import Application, Environment, EnvironmentSnapshot
from apex.routers.inventory import router
from apex.services.inventory import (
    STALE_AFTER,
    get_inventory_adapter_resolver,
    get_snapshots_repository,
    is_stale,
)

NOW = datetime.now(UTC)


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeSnapshotsRepository:
    def __init__(self) -> None:
        self.environments: dict[str, Environment] = {}
        self.rows: dict[str, list[EnvironmentSnapshot]] = {}

    async def get_environment(self, environment_id: str) -> Environment | None:
        return self.environments.get(environment_id)

    async def latest(self, environment_id: str) -> EnvironmentSnapshot | None:
        rows = self.rows.get(environment_id, [])
        return max(rows, key=lambda r: r.scanned_at) if rows else None

    async def add(
        self, environment_id: str, *, data: dict[str, Any], scanned_at: datetime
    ) -> EnvironmentSnapshot:
        row = EnvironmentSnapshot(
            id=uuid4().hex, environment_id=environment_id, scanned_at=scanned_at, data=data
        )
        self.rows.setdefault(environment_id, []).append(row)
        return row


class FakeClusterInventoryAdapter:
    def __init__(
        self, snapshot: DomainSnapshot | None = None, error: Exception | None = None
    ) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls: list[EnvRef] = []

    async def scan_environment(self, env_ref: EnvRef) -> DomainSnapshot:
        self.calls.append(env_ref)
        if self.error is not None:
            raise self.error
        assert self.snapshot is not None
        return self.snapshot


def identity(role: Role, *projects: str) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c-test",
        name="test",
        consumer_type=ConsumerType.INTERNAL,
        role=role,
        scopes=[ScopeRef(project_id=p) for p in projects],
    )


ADMIN = identity(Role.ADMIN)
OPERATOR_DEMO = identity(Role.OPERATOR, "demo")
VIEWER_DEMO = identity(Role.VIEWER, "demo")


def make_environment(project_id: str = "demo", name: str = "staging-2") -> Environment:
    app = Application(id=uuid4().hex, project_id=project_id, name="Checkout")
    env = Environment(id=uuid4().hex, application_id=app.id, name=name, kind="k8s", options={})
    env.application = app
    return env


def make_row(
    environment_id: str, scanned_at: datetime, services: list[dict[str, Any]] | None = None
) -> EnvironmentSnapshot:
    services = (
        services
        if services is not None
        else [
            {"name": "checkout-api", "replicas": 3, "image": "registry.internal/checkout-api:2.7.1"}
        ]
    )
    return EnvironmentSnapshot(
        id=uuid4().hex,
        environment_id=environment_id,
        scanned_at=scanned_at,
        data={"services": services, "scanned_at": scanned_at.isoformat()},
    )


def make_client(
    repo: FakeSnapshotsRepository,
    who: ConsumerIdentity,
    adapter: FakeClusterInventoryAdapter | None = None,
    resolver_error: Exception | None = None,
) -> tuple[TestClient, dict[str, Any]]:
    app = FastAPI()
    app.include_router(router)
    captured: dict[str, Any] = {}

    async def _resolve(connection_id: str | None, project_id: str | None) -> Any:
        captured["connection_id"] = connection_id
        captured["project_id"] = project_id
        if resolver_error is not None:
            raise resolver_error
        return adapter

    app.dependency_overrides[get_snapshots_repository] = lambda: repo
    app.dependency_overrides[get_inventory_adapter_resolver] = lambda: _resolve
    app.dependency_overrides[get_current_identity] = lambda: who
    return TestClient(app), captured


@pytest.fixture
def repo() -> FakeSnapshotsRepository:
    return FakeSnapshotsRepository()


# ── GET /inventory/environments/{id} ─────────────────────────────────────────


def test_get_unknown_environment_is_404(repo: FakeSnapshotsRepository) -> None:
    client, _ = make_client(repo, ADMIN)
    assert client.get("/inventory/environments/nope").status_code == 404


def test_get_never_scanned_environment_returns_null_snapshot(
    repo: FakeSnapshotsRepository,
) -> None:
    env = make_environment()
    repo.environments[env.id] = env

    client, _ = make_client(repo, ADMIN)
    response = client.get(f"/inventory/environments/{env.id}")

    assert response.status_code == 200
    assert response.json() == {"environment_id": env.id, "snapshot": None}


def test_get_returns_latest_snapshot_with_services(repo: FakeSnapshotsRepository) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    repo.rows[env.id] = [
        make_row(env.id, NOW - timedelta(days=3), services=[{"name": "old-svc"}]),
        make_row(env.id, NOW - timedelta(hours=1)),
    ]

    client, _ = make_client(repo, ADMIN)
    body = client.get(f"/inventory/environments/{env.id}").json()

    snapshot = body["snapshot"]
    assert snapshot["stale"] is False
    assert [s["name"] for s in snapshot["services"]] == ["checkout-api"]
    assert snapshot["services"][0]["replicas"] == 3


def test_get_flags_snapshot_older_than_seven_days_as_stale(
    repo: FakeSnapshotsRepository,
) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    repo.rows[env.id] = [make_row(env.id, NOW - timedelta(days=8))]

    client, _ = make_client(repo, ADMIN)
    assert client.get(f"/inventory/environments/{env.id}").json()["snapshot"]["stale"] is True


def test_is_stale_boundary_is_strictly_older_than_seven_days() -> None:
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
    assert is_stale(now - STALE_AFTER, now=now) is False  # exactly 7d: not stale
    assert is_stale(now - STALE_AFTER - timedelta(seconds=1), now=now) is True
    assert is_stale(now - STALE_AFTER + timedelta(seconds=1), now=now) is False


def test_get_cross_project_environment_is_404_for_scoped_consumer(
    repo: FakeSnapshotsRepository,
) -> None:
    env = make_environment(project_id="other")
    repo.environments[env.id] = env

    client, _ = make_client(repo, VIEWER_DEMO)
    assert client.get(f"/inventory/environments/{env.id}").status_code == 404


# ── POST /inventory/environments/{id}/rescan ─────────────────────────────────


def fresh_domain_snapshot() -> DomainSnapshot:
    return DomainSnapshot(
        services=[
            ServiceInfo(name="checkout-api", replicas=3, image="registry.internal/co:2.7.1"),
            ServiceInfo(name="cart-svc", replicas=0, image="registry.internal/cart:1.9.3"),
        ],
        scanned_at=NOW.isoformat(),
    )


def test_rescan_persists_exactly_one_new_row_and_returns_it(
    repo: FakeSnapshotsRepository,
) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    repo.rows[env.id] = [make_row(env.id, NOW - timedelta(days=30), services=[{"name": "old"}])]
    adapter = FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot())

    client, captured = make_client(repo, OPERATOR_DEMO, adapter=adapter)
    response = client.post(f"/inventory/environments/{env.id}/rescan")

    assert response.status_code == 200
    body = response.json()
    assert body["environment_id"] == env.id
    assert [s["name"] for s in body["snapshot"]["services"]] == ["checkout-api", "cart-svc"]
    assert body["snapshot"]["stale"] is False

    assert len(repo.rows[env.id]) == 2  # exactly one NEW row appended
    new_row = max(repo.rows[env.id], key=lambda r: r.scanned_at)
    assert [s["name"] for s in new_row.data["services"]] == ["checkout-api", "cart-svc"]

    # adapter got the catalog environment as EnvRef; resolver got the env's project
    assert adapter.calls == [EnvRef(id=env.id, name=env.name)]
    assert captured == {"connection_id": None, "project_id": "demo"}


def test_rescan_forwards_connection_id_query_param(repo: FakeSnapshotsRepository) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot())

    client, captured = make_client(repo, ADMIN, adapter=adapter)
    response = client.post(
        f"/inventory/environments/{env.id}/rescan", params={"connection_id": "conn-k8s-staging"}
    )

    assert response.status_code == 200
    assert captured["connection_id"] == "conn-k8s-staging"


def test_rescan_requires_operator_role(repo: FakeSnapshotsRepository) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot())

    client, _ = make_client(repo, VIEWER_DEMO, adapter=adapter)
    response = client.post(f"/inventory/environments/{env.id}/rescan")

    assert response.status_code == 403
    assert repo.rows.get(env.id, []) == []  # nothing persisted
    assert adapter.calls == []


def test_rescan_unknown_environment_is_404(repo: FakeSnapshotsRepository) -> None:
    client, _ = make_client(
        repo, ADMIN, adapter=FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot())
    )
    assert client.post("/inventory/environments/nope/rescan").status_code == 404


def test_rescan_cross_project_environment_is_404(repo: FakeSnapshotsRepository) -> None:
    env = make_environment(project_id="other")
    repo.environments[env.id] = env

    client, _ = make_client(
        repo, OPERATOR_DEMO, adapter=FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot())
    )
    assert client.post(f"/inventory/environments/{env.id}/rescan").status_code == 404


def test_rescan_adapter_failure_is_502_with_adapter_message(
    repo: FakeSnapshotsRepository,
) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(
        error=RuntimeError("kubernetes API denied GET /apis/...: check the ServiceAccount token")
    )

    client, _ = make_client(repo, OPERATOR_DEMO, adapter=adapter)
    response = client.post(f"/inventory/environments/{env.id}/rescan")

    assert response.status_code == 502
    assert "ServiceAccount token" in response.json()["detail"]
    assert repo.rows.get(env.id, []) == []  # failed scans persist nothing


def test_rescan_resolver_failure_is_502(repo: FakeSnapshotsRepository) -> None:
    env = make_environment()
    repo.environments[env.id] = env

    client, _ = make_client(
        repo, ADMIN, resolver_error=KeyError("unknown connection_id 'conn-ghost'")
    )
    response = client.post(
        f"/inventory/environments/{env.id}/rescan", params={"connection_id": "conn-ghost"}
    )

    assert response.status_code == 502
    assert "conn-ghost" in response.json()["detail"]
