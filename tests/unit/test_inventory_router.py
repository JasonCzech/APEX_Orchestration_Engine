"""Inventory router: latest snapshots, staleness, scoping, and inline rescans."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

import apex.services.inventory as inventory_service
from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.integrations import MAX_INVENTORY_SERVICES, EnvRef, ServiceInfo
from apex.domain.integrations import EnvironmentSnapshot as DomainSnapshot
from apex.persistence.models import Application, Environment, EnvironmentSnapshot
from apex.persistence.repositories.snapshots import (
    SNAPSHOT_HISTORY_LIMIT,
    _expired_snapshots_statement,
    _latest_snapshot_statement,
)
from apex.routers.inventory import router
from apex.services.inventory import (
    MAX_CONCURRENT_INVENTORY_SCANS,
    STALE_AFTER,
    InventoryScanBusyError,
    InventoryService,
    get_inventory_adapter_resolver,
    get_snapshots_repository,
    is_stale,
)

NOW = datetime.now(UTC)


def test_snapshot_retention_prunes_every_row_after_the_bounded_latest_history() -> None:
    statement = _expired_snapshots_statement("environment-1")
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "DELETE FROM apex.environment_snapshots" in sql
    assert f"OFFSET {SNAPSHOT_HISTORY_LIMIT}" in sql
    assert "ORDER BY apex.environment_snapshots.scanned_at DESC" in sql

    latest_sql = str(
        _latest_snapshot_statement("environment-1").compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "scanned_at DESC, apex.environment_snapshots.id DESC" in latest_sql
    assert "LIMIT 1" in latest_sql


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeSnapshotsRepository:
    def __init__(self) -> None:
        self.environments: dict[str, Environment] = {}
        self.rows: dict[str, list[EnvironmentSnapshot]] = {}
        self.environment_get_calls: list[str] = []

    async def get_environment(self, environment_id: str) -> Environment | None:
        self.environment_get_calls.append(environment_id)
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


def app_identity(role: Role, project_id: str, app_id: str) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c-app-test",
        name="app-test",
        consumer_type=ConsumerType.INTERNAL,
        role=role,
        scopes=[ScopeRef(project_id=project_id, app_id=app_id)],
    )


ADMIN = identity(Role.ADMIN)
OPERATOR_DEMO = identity(Role.OPERATOR, "demo")
VIEWER_DEMO = identity(Role.VIEWER, "demo")


def make_environment(
    project_id: str = "demo",
    name: str = "staging-2",
    *,
    application_id: str | None = None,
) -> Environment:
    app = Application(id=application_id or uuid4().hex, project_id=project_id, name="Checkout")
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


def test_get_rejects_oversized_environment_id_before_repository_io(
    repo: FakeSnapshotsRepository,
) -> None:
    client, _ = make_client(repo, ADMIN)

    response = client.get(f"/inventory/environments/{'x' * 33}")

    assert response.status_code == 422
    assert repo.environment_get_calls == []


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


def test_get_sibling_app_environment_is_404_for_app_scoped_consumer(
    repo: FakeSnapshotsRepository,
) -> None:
    env = make_environment(application_id="app-b")
    repo.environments[env.id] = env

    client, _ = make_client(repo, app_identity(Role.VIEWER, "demo", "app-a"))
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


@pytest.mark.parametrize("connection_id", ["x" * 33, "conn\x00k8s"])
def test_rescan_rejects_invalid_connection_id_before_repository_or_provider_io(
    repo: FakeSnapshotsRepository,
    connection_id: str,
) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot())
    client, captured = make_client(repo, ADMIN, adapter=adapter)

    response = client.post(
        f"/inventory/environments/{env.id}/rescan",
        params={"connection_id": connection_id},
    )

    assert response.status_code == 422
    assert repo.environment_get_calls == []
    assert captured == {}
    assert adapter.calls == []


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


def test_rescan_sibling_app_environment_is_404(repo: FakeSnapshotsRepository) -> None:
    env = make_environment(application_id="app-b")
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot())

    client, _ = make_client(repo, app_identity(Role.OPERATOR, "demo", "app-a"), adapter=adapter)
    assert client.post(f"/inventory/environments/{env.id}/rescan").status_code == 404
    assert adapter.calls == []


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
    assert response.json()["detail"] == "environment rescan failed"
    assert "ServiceAccount token" not in response.text
    assert repo.rows.get(env.id, []) == []  # failed scans persist nothing


def test_rescan_adapter_timeout_is_controlled_502_without_persistence(
    repo: FakeSnapshotsRepository,
) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(error=TimeoutError("inventory deadline exceeded"))

    client, _ = make_client(repo, OPERATOR_DEMO, adapter=adapter)
    response = client.post(f"/inventory/environments/{env.id}/rescan")

    assert response.status_code == 502
    assert response.json()["detail"] == "environment rescan failed"
    assert "deadline" not in response.text
    assert repo.rows.get(env.id, []) == []


def test_rescan_detaches_arbitrary_provider_exception(
    repo: FakeSnapshotsRepository,
) -> None:
    canary = "inventory-provider-secret-canary"

    class ProviderFault(Exception):
        pass

    env = make_environment()
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(error=ProviderFault(canary))

    client, _ = make_client(repo, OPERATOR_DEMO, adapter=adapter)
    response = client.post(f"/inventory/environments/{env.id}/rescan")

    assert response.status_code == 502
    assert response.json()["detail"] == "environment rescan failed"
    assert canary not in response.text
    assert repo.rows.get(env.id, []) == []


@pytest.mark.parametrize(
    "snapshot",
    [
        DomainSnapshot.model_construct(services=[], scanned_at="not-an-iso-timestamp"),
        DomainSnapshot.model_construct(
            services=[ServiceInfo.model_construct(name="", replicas=1, image="image:1")],
            scanned_at=NOW.isoformat(),
        ),
        DomainSnapshot.model_construct(
            services=[ServiceInfo(name="service")] * (MAX_INVENTORY_SERVICES + 1),
            scanned_at=NOW.isoformat(),
        ),
        DomainSnapshot.model_construct(
            services=[
                ServiceInfo.model_construct(
                    name="service",
                    replicas=1,
                    image="x" * 100_000 + "inventory-oversized-secret-canary",
                )
            ],
            scanned_at=NOW.isoformat(),
        ),
    ],
)
def test_rescan_revalidates_provider_snapshot_before_persistence(
    repo: FakeSnapshotsRepository, snapshot: DomainSnapshot
) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(snapshot=snapshot)

    client, _ = make_client(repo, OPERATOR_DEMO, adapter=adapter)
    response = client.post(f"/inventory/environments/{env.id}/rescan")

    assert response.status_code == 502
    assert response.json()["detail"] == "environment rescan failed"
    assert repo.rows.get(env.id, []) == []


@pytest.mark.parametrize(
    "service",
    [
        ServiceInfo(
            name="password=inventory-service-secret-canary",
            replicas=1,
            image="registry.internal/service:1",
        ),
        ServiceInfo(
            name="checkout-api",
            replicas=1,
            image="registry.internal/service:1?token=inventory-image-secret-canary",
        ),
        ServiceInfo(
            name="checkout-api",
            replicas=1,
            image="https://user:inventory-image-secret-canary@registry.internal/service:1",
        ),
    ],
)
def test_rescan_rejects_credential_bearing_inventory_before_persistence(
    repo: FakeSnapshotsRepository,
    service: ServiceInfo,
) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(
        snapshot=DomainSnapshot(services=[service], scanned_at=NOW.isoformat())
    )

    client, _ = make_client(repo, OPERATOR_DEMO, adapter=adapter)
    response = client.post(f"/inventory/environments/{env.id}/rescan")

    assert response.status_code == 502
    assert response.json()["detail"] == "environment rescan failed"
    assert "inventory-service-secret-canary" not in response.text
    assert "inventory-image-secret-canary" not in response.text
    assert repo.rows.get(env.id, []) == []


def test_inventory_provider_exception_is_replaced_without_secret_chain(
    repo: FakeSnapshotsRepository,
) -> None:
    secret = "inventory-model-dump-secret-canary"

    class HostileSnapshot(DomainSnapshot):
        def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            raise RuntimeError(secret)

    env = make_environment()
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(snapshot=HostileSnapshot())

    client, _ = make_client(repo, OPERATOR_DEMO, adapter=adapter)
    response = client.post(f"/inventory/environments/{env.id}/rescan")

    assert response.status_code == 502
    assert secret not in response.text
    assert repo.rows.get(env.id, []) == []


def test_inventory_validation_detaches_secret_bearing_provider_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "inventory-provider-validation-secret-canary"

    def explode(_snapshot: object) -> DomainSnapshot:
        raise RuntimeError(secret)

    monkeypatch.setattr(inventory_service, "_validated_provider_snapshot", explode)

    with pytest.raises(RuntimeError, match="invalid snapshot") as exc_info:
        inventory_service.validated_provider_snapshot(object())

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert secret not in repr(exc_info.value)


def test_inventory_rejects_arbitrary_service_without_reading_spoofed_class(
    repo: FakeSnapshotsRepository,
) -> None:
    class HostileService:
        class_called = False

        def __getattribute__(self, name: str) -> Any:
            if name == "__class__":
                type(self).class_called = True
                raise AssertionError("provider __class__ descriptor must not be called")
            return object.__getattribute__(self, name)

    service = HostileService()
    snapshot = DomainSnapshot.model_construct(services=[service], scanned_at=NOW.isoformat())
    env = make_environment()
    repo.environments[env.id] = env

    client, _ = make_client(
        repo,
        OPERATOR_DEMO,
        adapter=FakeClusterInventoryAdapter(snapshot=snapshot),
    )
    response = client.post(f"/inventory/environments/{env.id}/rescan")

    assert response.status_code == 502
    assert service.class_called is False
    assert repo.rows.get(env.id, []) == []


async def test_legacy_credential_inventory_fails_closed_on_read() -> None:
    secret = "legacy-inventory-image-secret-canary"
    repository = FakeSnapshotsRepository()
    repository.rows["env-1"] = [
        make_row(
            "env-1",
            NOW,
            services=[
                {
                    "name": "checkout-api",
                    "replicas": 1,
                    "image": f"registry/service:1?token={secret}",
                }
            ],
        )
    ]

    with pytest.raises(RuntimeError, match="invalid service data") as raised:
        await InventoryService(cast(Any, repository)).latest_inventory("env-1")

    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert secret not in str(raised.value)


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
    assert response.json()["detail"] == "environment rescan failed"
    assert "conn-ghost" not in response.text


def test_rescan_capacity_exhaustion_is_fail_fast_429(
    repo: FakeSnapshotsRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = make_environment()
    repo.environments[env.id] = env
    adapter = FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot())
    monkeypatch.setattr(
        inventory_service,
        "_ACTIVE_INVENTORY_SCANS",
        MAX_CONCURRENT_INVENTORY_SCANS,
    )

    client, _ = make_client(repo, OPERATOR_DEMO, adapter=adapter)
    response = client.post(f"/inventory/environments/{env.id}/rescan")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert adapter.calls == []


async def test_rescan_admission_precedes_lazy_adapter_io_and_releases_after_completion(
    repo: FakeSnapshotsRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inventory_service, "MAX_CONCURRENT_INVENTORY_SCANS", 1)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingAdapter(FakeClusterInventoryAdapter):
        async def scan_environment(self, env_ref: EnvRef) -> DomainSnapshot:
            self.calls.append(env_ref)
            started.set()
            await release.wait()
            return fresh_domain_snapshot()

    service = InventoryService(cast(Any, repo))
    environment = EnvRef(id="env-admission", name="Admission")
    first = asyncio.create_task(
        service.rescan(environment, BlockingAdapter(snapshot=fresh_domain_snapshot()))
    )
    await started.wait()
    resolver_called = False

    async def rejected_adapter_factory() -> FakeClusterInventoryAdapter:
        nonlocal resolver_called
        resolver_called = True
        return FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot())

    with pytest.raises(InventoryScanBusyError):
        await service.rescan(environment, rejected_adapter_factory)
    assert resolver_called is False

    release.set()
    await first
    await service.rescan(
        environment,
        FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot()),
    )


async def test_rescan_admission_releases_after_exception_and_cancellation(
    repo: FakeSnapshotsRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inventory_service, "MAX_CONCURRENT_INVENTORY_SCANS", 1)
    service = InventoryService(cast(Any, repo))
    environment = EnvRef(id="env-release", name="Release")

    with pytest.raises(RuntimeError, match="provider failed"):
        await service.rescan(
            environment,
            FakeClusterInventoryAdapter(error=RuntimeError("provider failed")),
        )

    started = asyncio.Event()
    never_release = asyncio.Event()

    class CancelledAdapter(FakeClusterInventoryAdapter):
        async def scan_environment(self, env_ref: EnvRef) -> DomainSnapshot:
            started.set()
            await never_release.wait()
            return fresh_domain_snapshot()

    cancelled = asyncio.create_task(
        service.rescan(environment, CancelledAdapter(snapshot=fresh_domain_snapshot()))
    )
    await started.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    await service.rescan(
        environment,
        FakeClusterInventoryAdapter(snapshot=fresh_domain_snapshot()),
    )
