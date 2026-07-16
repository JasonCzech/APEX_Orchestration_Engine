"""Admin connections router: CRUD, provider validation, host mappings, probe."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.integrations import (
    FileContent,
    LogEntry,
    LogQuery,
    LogSearchResult,
    Page,
    SecretValue,
    TimeWindow,
    WorkItem,
)
from apex.persistence.models import Connection, HostMapping
from apex.persistence.repositories.connections import (
    ConnectionsRepository,
    DuplicateConnectionNameError,
)
from apex.ports.artifact_store import StoredArtifact
from apex.routers.connections import (
    PROBE_CALLS,
    _probe_artifact_store,
    _probe_documents,
    _probe_log_search,
    _probe_source_control,
    _probe_work_tracking,
    get_connections_repository,
    router,
)
from apex.services.connection_credentials import (
    connection_options_require_repair,
    reject_raw_secret_options,
    sanitize_connection_options_for_output,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _make_connection(**kwargs: Any) -> Connection:
    now = _now()
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
    conn.created_at = now
    conn.updated_at = now
    conn.runtime_version = now
    conn.host_mappings = []
    return conn


@pytest.mark.asyncio
async def test_artifact_connection_guard_includes_pipeline_artifact_index() -> None:
    class ScalarSession:
        def __init__(self) -> None:
            self.values = iter([None, None, "artifact-ref-1"])

        async def scalar(self, _statement: object) -> object:
            return next(self.values)

    repo = ConnectionsRepository(cast(Any, ScalarSession()))
    conn = _make_connection(kind="artifact_store", provider="stub", name="artifact-store")

    assert await repo.durable_reference_reason(conn) == "stored pipeline artifacts"


@pytest.mark.asyncio
async def test_artifact_connection_guard_includes_pending_upload_outbox() -> None:
    class ScalarSession:
        def __init__(self) -> None:
            self.values = iter([None, None, None, "upload-intent-1"])

        async def scalar(self, _statement: object) -> object:
            return next(self.values)

    repo = ConnectionsRepository(cast(Any, ScalarSession()))
    conn = _make_connection(kind="artifact_store", provider="stub", name="artifact-store")

    assert await repo.durable_reference_reason(conn) == "pending pipeline artifact uploads"


@pytest.mark.asyncio
async def test_execution_connection_guard_uses_active_foreign_key_lease() -> None:
    class ScalarSession:
        async def scalar(self, _statement: object) -> str:
            return "engine-run-1"

    repo = ConnectionsRepository(cast(Any, ScalarSession()))
    conn = _make_connection(kind="execution_engine", provider="sim", name="engine")

    assert await repo.durable_reference_reason(conn) == "active engine runs"


@pytest.mark.asyncio
async def test_execution_connection_legacy_guard_filters_json_in_sql_and_stops_at_one() -> None:
    class ScalarSession:
        def __init__(self) -> None:
            self.values = iter([None, "legacy-engine-run-1"])
            self.statements: list[Any] = []

        async def scalar(self, statement: Any) -> object:
            self.statements.append(statement)
            return next(self.values)

    session = ScalarSession()
    repo = ConnectionsRepository(cast(Any, session))
    conn = _make_connection(kind="execution_engine", provider="sim", name="engine")

    assert await repo.durable_reference_reason(conn) == "active engine runs"
    legacy_sql = str(
        session.statements[1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "engine_runs.handle ->> 'connection_id'" in legacy_sql
    assert "LIMIT 1" in legacy_sql


@pytest.mark.asyncio
async def test_work_tracking_connection_guard_includes_pending_mutations() -> None:
    class ScalarSession:
        async def scalar(self, _statement: object) -> str:
            return "mutation-1"

    repo = ConnectionsRepository(cast(Any, ScalarSession()))
    conn = _make_connection(kind="work_tracking", provider="stub", name="tracker")

    assert await repo.durable_reference_reason(conn) == "pending work-item mutations"


@pytest.mark.asyncio
async def test_work_tracking_connection_guard_includes_retained_live_results() -> None:
    class ScalarSession:
        def __init__(self) -> None:
            self.values = iter([None, "mutation-1"])

        async def scalar(self, _statement: object) -> object:
            return next(self.values)

    repo = ConnectionsRepository(cast(Any, ScalarSession()))
    conn = _make_connection(kind="work_tracking", provider="stub", name="tracker")

    assert await repo.durable_reference_reason(conn) == "retained work-item idempotency records"


@pytest.mark.asyncio
async def test_connections_repository_rejects_nul_from_non_http_writers() -> None:
    repo = ConnectionsRepository(cast(Any, object()))
    connection = _make_connection(kind="work_tracking", provider="stub", name="tracker")

    with pytest.raises(ValueError, match="U\\+0000"):
        await repo.create(kind="work_tracking", provider="stub", name="bad\x00name")
    with pytest.raises(ValueError, match="U\\+0000"):
        await repo.update(connection, {"base_url": "https://bad\x00.example"})
    with pytest.raises(ValueError, match="U\\+0000"):
        await repo.replace_host_mappings(
            connection,
            [{"pattern": "bad\x00pattern", "target": "10.0.0.1"}],
        )

    assert connection.base_url is None
    assert connection.host_mappings == []


@pytest.mark.asyncio
async def test_connections_repository_rejects_credentials_in_scalar_labels() -> None:
    repo = ConnectionsRepository(cast(Any, object()))
    connection = _make_connection(kind="work_tracking", provider="stub", name="tracker")
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"

    with pytest.raises(ValueError, match="credential material"):
        await repo.create(kind="work_tracking", provider="stub", name=credential)
    with pytest.raises(ValueError, match="credential material"):
        await repo.update(connection, {"name": credential})
    with pytest.raises(ValueError, match="credential material"):
        await repo.replace_host_mappings(
            connection,
            [{"pattern": credential, "target": "10.0.0.1"}],
        )

    assert connection.name == "tracker"
    assert connection.host_mappings == []


@pytest.mark.asyncio
async def test_connections_repository_rejects_unknown_invalid_and_unbounded_updates() -> None:
    repo = ConnectionsRepository(cast(Any, object()))
    connection = _make_connection(kind="work_tracking", provider="stub", name="tracker")

    for changes in ({"id": "forged"}, {"unknown": "value"}):
        with pytest.raises(ValueError, match="unsupported connection fields"):
            await repo.update(connection, changes)
    with pytest.raises(ValueError, match="must be a boolean"):
        await repo.update(connection, {"enabled": "yes"})
    with pytest.raises(ValueError, match="1-255 character"):
        await repo.update(connection, {"name": "x" * 256})
    with pytest.raises(ValueError, match="1-255 character"):
        await repo.create(kind="work_tracking", provider="stub", name="x" * 256)
    with pytest.raises(ValueError, match="1-64 character"):
        await repo.update(connection, {"provider": ""})
    with pytest.raises(ValueError, match="must be a boolean"):
        await repo.set_enabled(connection, cast(Any, 1))

    assert connection.id != "forged"
    assert connection.name == "tracker"
    assert connection.provider == "stub"
    assert connection.enabled is True


@pytest.mark.asyncio
async def test_connections_repository_never_executes_hostile_change_or_mapping_hooks() -> None:
    class HostileDict(dict[Any, Any]):
        called = False

        def __iter__(self) -> Iterator[Any]:
            self.called = True
            raise AssertionError("custom dictionary iteration must not execute")

        def keys(self) -> Any:
            self.called = True
            raise AssertionError("custom dictionary keys must not execute")

    repo = ConnectionsRepository(cast(Any, object()))
    connection = _make_connection(kind="work_tracking", provider="stub", name="tracker")
    changes = HostileDict(name="forged")
    mapping = HostileDict(pattern="*.example.test", target="10.0.0.1")

    with pytest.raises(ValueError, match="unsupported connection fields"):
        await repo.update(connection, changes)
    with pytest.raises(ValueError, match="host mapping must contain only"):
        await repo.replace_host_mappings(connection, [mapping])

    assert changes.called is False
    assert mapping.called is False
    assert connection.name == "tracker"
    assert connection.host_mappings == []


@pytest.mark.asyncio
async def test_connections_repository_validates_complete_host_mapping_shape() -> None:
    repo = ConnectionsRepository(cast(Any, object()))
    connection = _make_connection(kind="work_tracking", provider="stub", name="tracker")

    invalid: list[Any] = [
        ({"pattern": "*.example.test", "target": "10.0.0.1"},),
        [{"pattern": "*.example.test", "target": "10.0.0.1", "extra": True}],
        [{"pattern": "*.example.test", "target": "10.0.0.1", "enabled": 1}],
        [{"pattern": "", "target": "10.0.0.1"}],
        [{"pattern": "x" * 1_025, "target": "10.0.0.1"}],
    ]
    for mappings in invalid:
        with pytest.raises(ValueError):
            await repo.replace_host_mappings(connection, cast(Any, mappings))
    with pytest.raises(ValueError, match="at most 256"):
        await repo.replace_host_mappings(
            connection,
            [{"pattern": f"host-{index}", "target": "10.0.0.1"} for index in range(257)],
        )

    assert connection.host_mappings == []


@pytest.mark.asyncio
async def test_connections_repository_does_not_reenable_legacy_unsafe_row() -> None:
    repo = ConnectionsRepository(cast(Any, object()))
    connection = _make_connection(
        kind="work_tracking",
        provider="stub",
        name="legacy",
        base_url="https://operator:secret@example.test/api",
    )
    connection.enabled = False

    with pytest.raises(ValueError, match="credential"):
        await repo.set_enabled(connection, True)

    assert connection.enabled is False


@pytest.mark.parametrize(
    ("provider", "options"),
    [
        ("jira", {}),
        ("jira", {"project_key": ""}),
        ("ado", {}),
        ("ado", {"project": " "}),
    ],
)
async def test_connections_repository_requires_external_project_on_scoped_real_trackers(
    provider: str, options: dict[str, Any]
) -> None:
    repository = ConnectionsRepository(cast(Any, object()))

    with pytest.raises(ValueError, match="requires a bounded external project"):
        await repository.create(
            kind="work_tracking",
            provider=provider,
            name="direct-writer",
            project_id="project-1",
            options=options,
        )

    legacy = _make_connection(
        kind="work_tracking",
        provider=provider,
        name="legacy",
        project_id="project-1",
        options=options,
    )
    with pytest.raises(ValueError, match="requires a bounded external project"):
        await repository.update(legacy, {"name": "still-legacy"})
    assert legacy.name == "legacy"

    legacy.enabled = False
    with pytest.raises(ValueError, match="requires a bounded external project"):
        await repository.set_enabled(legacy, True)
    assert legacy.enabled is False


async def test_connections_repository_accepts_complete_scoped_jira_direct_write() -> None:
    class Session:
        def __init__(self) -> None:
            self.added: Connection | None = None

        def add(self, row: Connection) -> None:
            self.added = row

        async def commit(self) -> None:
            return None

        async def refresh(self, _row: Connection) -> None:
            return None

    session = Session()
    row = await ConnectionsRepository(cast(Any, session)).create(
        kind="work_tracking",
        provider="jira",
        name="scoped-jira",
        project_id="project-1",
        options={"project_key": "PHX"},
    )

    assert session.added is row
    assert row.options == {"project_key": "PHX"}


class FakeConnectionsRepository:
    """In-memory stand-in mirroring ConnectionsRepository semantics."""

    def __init__(self) -> None:
        self.connections: dict[str, Connection] = {}
        self.for_update_calls: list[str] = []
        self.list_calls = 0
        self.reference_reason: str | None = None

    async def list_connections(
        self,
        *,
        kind: str | None = None,
        project: str | None = None,
        manageable_project_ids: list[str] | tuple[str, ...] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Connection]:
        self.list_calls += 1
        rows = list(self.connections.values())
        if kind is not None:
            rows = [r for r in rows if r.kind == kind]
        if project is not None:
            rows = [r for r in rows if r.project_id == project]
        if manageable_project_ids is not None:
            rows = [
                row
                for row in rows
                if row.project_id in manageable_project_ids
                and row.secret_ref is None
                and row.kind != "secrets"
                and row.options.get("_apex_trusted_private_host") is not True
                and not (
                    row.kind == "cluster_inventory"
                    and row.provider.casefold() == "kubernetes"
                    and str(row.options.get("auth_mode", "bearer")).casefold()
                    in {"in_cluster", "in-cluster", "incluster"}
                )
            ]
        return sorted(rows, key=lambda r: (r.kind, r.name))[offset : offset + limit]

    async def get(self, connection_id: str) -> Connection | None:
        return self.connections.get(connection_id)

    async def get_for_update(self, connection_id: str) -> Connection | None:
        self.for_update_calls.append(connection_id)
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
        if {
            "provider",
            "project_id",
            "base_url",
            "options",
            "secret_ref",
            "enabled",
        }.intersection(changes):
            conn.runtime_version = _now()
        return conn

    async def set_enabled(self, conn: Connection, enabled: bool) -> Connection:
        conn.enabled = enabled
        conn.updated_at = _now()
        conn.runtime_version = _now()
        return conn

    async def delete(self, conn: Connection) -> None:
        self.connections.pop(conn.id, None)

    async def durable_reference_reason(self, conn: Connection) -> str | None:
        return self.reference_reason

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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "stu\x00b"),
        ("name", "jira\x00demo"),
        ("base_url", "https://jira\x00.example"),
    ],
)
def test_create_connection_rejects_nul_text_before_repository_mutation(
    admin: TestClient,
    repo: FakeConnectionsRepository,
    field: str,
    value: str,
) -> None:
    payload = {
        "kind": "work_tracking",
        "provider": "stub",
        "name": "jira-demo",
        field: value,
    }

    response = admin.post("/admin/connections", json=payload)

    assert response.status_code == 422
    assert repo.connections == {}


def test_create_rejects_scoped_jira_without_external_project(
    admin: TestClient,
    repo: FakeConnectionsRepository,
) -> None:
    response = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "jira",
            "name": "scoped-jira",
            "project_id": "demo",
            "options": {},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "scoped work-tracking connection requires an external project"
    )
    assert repo.connections == {}


def test_update_and_enable_reject_scoped_jira_without_external_project(
    admin: TestClient,
    repo: FakeConnectionsRepository,
) -> None:
    connection = _make_connection(
        kind="work_tracking",
        provider="jira",
        name="scoped-jira",
        project_id="demo",
        options={"project_key": "PHX"},
    )
    repo.connections[connection.id] = connection

    updated = admin.patch(f"/admin/connections/{connection.id}", json={"options": {}})

    assert updated.status_code == 422
    assert connection.options == {"project_key": "PHX"}

    connection.options = {}
    connection.enabled = False
    enabled = admin.post(f"/admin/connections/{connection.id}/enable")

    assert enabled.status_code == 422
    assert connection.enabled is False


@pytest.mark.parametrize("secret_ref", ["vault:path/to/key", "file:/run/secrets/key"])
def test_create_rejects_secret_schemes_without_a_registered_resolver(
    admin: TestClient, repo: FakeConnectionsRepository, secret_ref: str
) -> None:
    response = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "unsupported-secret-ref",
            "secret_ref": secret_ref,
        },
    )

    assert response.status_code == 422
    assert repo.connections == {}


def test_list_rejects_huge_offset_before_repository(
    repo: FakeConnectionsRepository, admin: TestClient
) -> None:
    response = admin.get("/admin/connections", params={"offset": 10_001})

    assert response.status_code == 422
    assert repo.list_calls == 0


def test_create_unknown_provider_is_422_with_registered_list(admin: TestClient) -> None:
    response = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "definitely-not-real", "name": "x"},
    )
    assert response.status_code == 422
    detail = response.text
    assert "unknown provider for connection kind" in detail
    assert "definitely-not-real" not in detail


def test_validation_error_does_not_reflect_secret_bearing_options(
    repo: FakeConnectionsRepository,
) -> None:
    password_literal = "password-value-that-must-never-be-reflected"
    token_literal = "token-value-that-must-never-be-reflected"
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_connections_repository] = lambda: repo
    app.dependency_overrides[get_current_identity] = lambda: identity(Role.ADMIN)

    with TestClient(app) as client:
        response = client.post(
            "/admin/connections",
            json={
                "kind": "work_tracking",
                "provider": "stub",
                "name": "secret-bearing-options",
                "options": {
                    "password": password_literal,
                    "nested": {"token": token_literal},
                },
            },
        )

    assert response.status_code == 422
    assert password_literal.encode() not in response.content
    assert token_literal.encode() not in response.content
    body = response.json()
    assert body["title"] == "Request validation failed"
    assert body["errors"] == [
        {
            "type": "value_error",
            "loc": ["body", "<field>"],
            "msg": "Invalid request value",
        }
    ]
    assert repo.connections == {}


def test_connection_api_rejects_credentials_in_scalar_labels_without_reflection(
    repo: FakeConnectionsRepository,
) -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_connections_repository] = lambda: repo
    app.dependency_overrides[get_current_identity] = lambda: identity(Role.ADMIN)

    with TestClient(app) as client:
        created = client.post(
            "/admin/connections",
            json={"kind": "work_tracking", "provider": "stub", "name": credential},
        )
        safe = client.post(
            "/admin/connections",
            json={"kind": "work_tracking", "provider": "stub", "name": "safe-name"},
        )
        patched = client.patch(
            f"/admin/connections/{safe.json()['id']}",
            json={"name": credential},
        )

    assert created.status_code == 422
    assert patched.status_code == 422
    assert credential.encode() not in created.content
    assert credential.encode() not in patched.content
    assert len(repo.connections) == 1
    assert next(iter(repo.connections.values())).name == "safe-name"


@pytest.mark.parametrize(
    "options",
    [
        {"basic_auth": "opaque-basic-auth-canary"},
        {"jira_pat": "opaque-provider-pat-canary"},
        {"auth_header": "opaque-auth-header-canary"},
        {"jwt": "opaque-jwt-canary"},
        {"shared_key": "opaque-shared-key-canary"},
        {"account_key": "opaque-account-key-canary"},
        {"storage_key": "opaque-storage-key-canary"},
        {"secret_access_key": "opaque-secret-access-key-canary"},
        {"subscription_key": "opaque-subscription-key-canary"},
        {"session_id": "opaque-session-id-canary"},
        {"client_certificate": "opaque-client-certificate-canary"},
        {"private_pem": "opaque-private-pem-canary"},
        {"connection_string": "opaque-connection-string-canary"},
        {"database_uri": "opaque-database-uri-canary"},
        {"postgres_url": "opaque-postgres-url-canary"},
        {"redis_url": "opaque-redis-url-canary"},
        {"broker_uri": "opaque-broker-uri-canary"},
        {"amqp_url": "opaque-amqp-url-canary"},
        {"mongodb_uri": "opaque-mongodb-uri-canary"},
        {"dsn": "opaque-dsn-canary"},
        {"sas": "opaque-sas-canary"},
        {"ssh_key": "opaque-ssh-key-canary"},
        {"signing_key": "opaque-signing-key-canary"},
        {"encryption_key": "opaque-encryption-key-canary"},
        {"authentication": "opaque-authentication-canary"},
        {"header": "Authorization: Bearer opaque-auth-scheme-canary"},
        {"ordinary": "postgresql://user:opaque-uri-canary@example.test/db"},
        {"items": ["Authorization: Bearer opaque-nested-auth-canary"]},
        {"value": "ghp_0123456789abcdefghijklmnopqrstuvwxyz"},
        {"value": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhcGV4LXVzZXIifQ.c2lnbmF0dXJlLWNhbmFyeQ"},
        {
            "value": "-----BEGIN PRIVATE KEY-----\n"
            "cHJpdmF0ZS1rZXktY2FuYXJ5\n"
            "-----END PRIVATE KEY-----"
        },
        {
            "callback": (
                "https://operator:opaque-url-canary@example.test/callback"
                "?signature=opaque-query-canary"
            )
        },
    ],
)
def test_create_rejects_nonstandard_legacy_credential_options_without_reflection(
    repo: FakeConnectionsRepository,
    options: dict[str, Any],
) -> None:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_connections_repository] = lambda: repo
    app.dependency_overrides[get_current_identity] = lambda: identity(Role.ADMIN)
    with TestClient(app) as client:
        response = client.post(
            "/admin/connections",
            json={
                "kind": "work_tracking",
                "provider": "stub",
                "name": "unsafe-options",
                "options": options,
            },
        )

    assert response.status_code == 422
    assert b"opaque-basic-auth-canary" not in response.content
    assert b"opaque-url-canary" not in response.content
    assert b"opaque-query-canary" not in response.content
    assert b"opaque-" not in response.content
    for value in options.values():
        if isinstance(value, str):
            assert value.encode() not in response.content
    assert repo.connections == {}


def test_create_rejects_oversized_scope_and_deep_options_before_write(
    repo: FakeConnectionsRepository,
    admin: TestClient,
) -> None:
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(17):
        child: dict[str, Any] = {}
        cursor["child"] = child
        cursor = child

    too_long = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "long-scope",
            "project_id": "p" * 256,
        },
    )
    too_deep = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "deep-options",
            "options": nested,
        },
    )

    assert too_long.status_code == 422
    assert too_deep.status_code == 422
    assert repo.connections == {}


def test_patch_validates_provider_against_existing_kind(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "execution_engine", "provider": "sim", "name": "engine-1"},
    ).json()["id"]
    assert (
        admin.patch(f"/admin/connections/{conn_id}", json={"provider": "nope"}).status_code == 422
    )
    patched = admin.patch(f"/admin/connections/{conn_id}", json={"name": "engine-renamed"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "engine-renamed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "stu\x00b"),
        ("name", "engine\x00renamed"),
        ("base_url", "https://engine\x00.example"),
    ],
)
def test_patch_connection_rejects_nul_text_before_repository_mutation(
    admin: TestClient,
    repo: FakeConnectionsRepository,
    field: str,
    value: str,
) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "execution_engine", "provider": "sim", "name": "engine-1"},
    ).json()["id"]
    before = {
        "provider": repo.connections[conn_id].provider,
        "name": repo.connections[conn_id].name,
        "base_url": repo.connections[conn_id].base_url,
    }

    response = admin.patch(f"/admin/connections/{conn_id}", json={field: value})

    assert response.status_code == 422
    assert repo.for_update_calls == []
    assert {
        "provider": repo.connections[conn_id].provider,
        "name": repo.connections[conn_id].name,
        "base_url": repo.connections[conn_id].base_url,
    } == before


@pytest.mark.parametrize(
    "patch",
    [
        {"provider": "s3"},
        {"project_id": "other"},
        {"base_url": "https://new-store.example"},
        {"options": {"bucket": "replacement"}},
        {"secret_ref": "env:OTHER_STORE_SECRET"},
    ],
)
def test_artifact_store_connection_location_is_immutable(
    admin: TestClient, patch: dict[str, Any]
) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={
            "kind": "artifact_store",
            "provider": "stub",
            "name": "artifacts-original",
            "project_id": "demo",
            "options": {"bucket": "original"},
            "secret_ref": "env:STORE_SECRET",
        },
    ).json()["id"]

    response = admin.patch(f"/admin/connections/{conn_id}", json=patch)

    assert response.status_code == 409
    assert "create a new connection id" in response.json()["detail"]


@pytest.mark.parametrize(
    "patch",
    [
        {"provider": "loadrunner"},
        {"project_id": "other"},
        {"base_url": "https://new-engine.example"},
        {"options": {"duration_s": 1.0}},
        {"secret_ref": "env:OTHER_ENGINE_SECRET"},
    ],
)
def test_execution_connection_runtime_identity_is_immutable(
    admin: TestClient, patch: dict[str, Any]
) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={
            "kind": "execution_engine",
            "provider": "sim",
            "name": "engine-original",
            "project_id": "demo",
        },
    ).json()["id"]

    response = admin.patch(f"/admin/connections/{conn_id}", json=patch)

    assert response.status_code == 409
    assert "create a new connection id" in response.json()["detail"]


@pytest.mark.parametrize(
    ("kind", "provider", "reference_reason"),
    [
        ("execution_engine", "sim", "active engine runs"),
        ("artifact_store", "stub", "stored pipeline artifacts"),
    ],
)
def test_runtime_connection_rename_preserves_in_flight_generation(
    admin: TestClient,
    repo: FakeConnectionsRepository,
    kind: str,
    provider: str,
    reference_reason: str,
) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": kind, "provider": provider, "name": f"{kind}-original"},
    ).json()["id"]
    row = repo.connections[conn_id]
    original_version = datetime(2020, 1, 1, tzinfo=UTC)
    original_modified = datetime(2020, 1, 2, tzinfo=UTC)
    row.runtime_version = original_version
    row.updated_at = original_modified
    repo.reference_reason = reference_reason

    response = admin.patch(
        f"/admin/connections/{conn_id}",
        json={"name": f"{kind}-renamed"},
    )

    assert response.status_code == 200
    assert conn_id in repo.for_update_calls
    assert row.updated_at > original_modified
    assert row.runtime_version == original_version


def test_artifact_store_connection_can_be_renamed_or_disabled(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "artifact_store", "provider": "stub", "name": "old-name"},
    ).json()["id"]

    renamed = admin.patch(f"/admin/connections/{conn_id}", json={"name": "new-name"})
    disabled = admin.post(f"/admin/connections/{conn_id}/disable")

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "new-name"
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False


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


def test_scoped_admin_cannot_manage_secret_bearing_connections(
    repo: FakeConnectionsRepository,
) -> None:
    client = make_client(repo, identity(Role.ADMIN, [ScopeRef(project_id="demo")]))
    repo.connections["secret"] = _make_connection(
        id="secret",
        kind="work_tracking",
        provider="stub",
        name="secret",
        project_id="demo",
        secret_ref="env:APEX_INTEGRATION_TRACKER_TOKEN",
    )
    repo.connections["secrets-port"] = _make_connection(
        id="secrets-port",
        kind="secrets",
        provider="env",
        name="secrets-port",
        project_id="demo",
    )

    assert client.get("/admin/connections").json() == []
    for connection_id in ("secret", "secrets-port"):
        assert client.get(f"/admin/connections/{connection_id}").status_code == 403
        assert (
            client.patch(
                f"/admin/connections/{connection_id}", json={"name": "changed"}
            ).status_code
            == 403
        )
        assert client.post(f"/admin/connections/{connection_id}/test").status_code == 403
        assert client.post(f"/admin/connections/{connection_id}/disable").status_code == 403
        assert client.delete(f"/admin/connections/{connection_id}").status_code == 403


def test_legacy_credentials_require_unscoped_repair_and_are_never_serialized(
    repo: FakeConnectionsRepository,
) -> None:
    password = "legacy-password-canary-9d8a"
    nested_token = "legacy-token-canary-4c7b"
    url_password = "legacy-url-canary-2f6e"
    raw_secret_ref = "legacy-secret-ref-canary-1a5d"
    repo.connections["legacy"] = _make_connection(
        id="legacy",
        kind="work_tracking",
        provider="stub",
        name="legacy",
        project_id="demo",
        base_url=(
            f"https://operator:{url_password}@tracker.example/api"
            "?access_token=query-canary#fragment-canary"
        ),
        options={
            "password": password,
            "nested": {"api_token": nested_token},
        },
        secret_ref=None,
    )
    canaries = (password, nested_token, url_password, "query-canary")

    scoped = make_client(repo, identity(Role.ADMIN, [ScopeRef(project_id="demo")]))
    scoped_list = scoped.get("/admin/connections")
    scoped_get = scoped.get("/admin/connections/legacy")

    assert scoped_list.status_code == 200
    assert scoped_list.json() == []
    assert scoped_get.status_code == 403
    for canary in canaries:
        assert canary.encode() not in scoped_list.content
        assert canary.encode() not in scoped_get.content

    unscoped = make_client(repo, identity(Role.ADMIN))
    unscoped_get = unscoped.get("/admin/connections/legacy")
    unscoped_list = unscoped.get("/admin/connections")

    assert unscoped_get.status_code == 200
    assert len(unscoped_list.json()) == 1
    for response in (unscoped_get, unscoped_list):
        for canary in canaries:
            assert canary.encode() not in response.content
    body = unscoped_get.json()
    assert body["base_url"] == "[REDACTED]"
    assert body["options"] == {"_apex_repair_required": True}
    assert body["secret_ref"] is None

    repaired = unscoped.patch(
        "/admin/connections/legacy",
        json={
            "base_url": "https://tracker.example/api",
            "options": {"project": "DEMO"},
            "secret_ref": None,
        },
    )

    assert repaired.status_code == 200
    assert repaired.json()["base_url"] == "https://tracker.example/api"
    assert repaired.json()["options"] == {"project": "DEMO"}
    assert repaired.json()["secret_ref"] is None
    assert scoped.get("/admin/connections/legacy").status_code == 200
    for canary in canaries:
        assert canary.encode() not in repaired.content

    repo.connections["legacy-ref"] = _make_connection(
        id="legacy-ref",
        kind="work_tracking",
        provider="stub",
        name="legacy-ref",
        project_id="demo",
        secret_ref=raw_secret_ref,
    )
    scoped_ref = scoped.get("/admin/connections/legacy-ref")
    unscoped_ref = unscoped.get("/admin/connections/legacy-ref")
    assert scoped_ref.status_code == 403
    assert unscoped_ref.status_code == 200
    assert unscoped_ref.json()["secret_ref"] == "[REDACTED]"
    assert raw_secret_ref.encode() not in scoped_ref.content
    assert raw_secret_ref.encode() not in unscoped_ref.content


def test_legacy_connection_credential_label_is_redacted_from_output(
    repo: FakeConnectionsRepository,
) -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    repo.connections["legacy-label"] = _make_connection(
        id="legacy-label",
        kind="work_tracking",
        provider="stub",
        name=credential,
    )

    response = make_client(repo, identity(Role.ADMIN)).get("/admin/connections/legacy-label")

    assert response.status_code == 200
    assert response.json()["name"] == "[REDACTED]"
    assert credential.encode() not in response.content


@pytest.mark.parametrize(
    "field",
    [
        "pat",
        "jira_pat",
        "auth_header",
        "bearer",
        "jwt",
        "psk",
        "shared_key",
        "account_key",
        "storage_key",
        "secret_access_key",
        "subscription_key",
        "session_id",
        "client_certificate",
        "private_pem",
        "connection_string",
        "database_uri",
        "postgres_url",
        "redis_url",
        "broker_uri",
        "amqp_url",
        "mongodb_uri",
        "dsn",
        "sas",
        "ssh_key",
        "signing_key",
        "encryption_key",
        "authentication",
    ],
)
def test_legacy_credential_alias_options_are_quarantined_atomically(field: str) -> None:
    value = {"safe_sibling": "visible-only-if-safe", field: "legacy-secret-canary"}

    assert connection_options_require_repair(value) is True
    projected = sanitize_connection_options_for_output(value)

    assert projected == {"_apex_repair_required": True}
    assert "legacy-secret-canary" not in repr(projected)


@pytest.mark.parametrize(
    "options",
    [
        {
            "safe_sibling": "visible-only-if-safe",
            "header": "Authorization: Bearer legacy-auth-scheme-canary",
        },
        {
            "safe_sibling": "visible-only-if-safe",
            "ordinary": "postgresql://user:legacy-uri-canary@example.test/db",
        },
        {
            "safe_sibling": "visible-only-if-safe",
            "items": ["Authorization: Bearer legacy-nested-auth-canary"],
        },
    ],
)
def test_legacy_credential_values_under_innocuous_keys_are_quarantined_atomically(
    options: dict[str, str],
) -> None:
    assert connection_options_require_repair(options) is True
    projected = sanitize_connection_options_for_output(options)

    assert projected == {"_apex_repair_required": True}
    assert "legacy-auth-scheme-canary" not in repr(projected)
    assert "legacy-uri-canary" not in repr(projected)
    assert "legacy-nested-auth-canary" not in repr(projected)


@pytest.mark.parametrize(
    "credential",
    [
        "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhcGV4LXVzZXIifQ.c2lnbmF0dXJlLWNhbmFyeQ",
        "-----BEGIN PRIVATE KEY-----\ncHJpdmF0ZS1rZXktY2FuYXJ5\n-----END PRIVATE KEY-----",
    ],
)
def test_standalone_credential_signatures_under_innocuous_keys_are_quarantined(
    credential: str,
) -> None:
    options = {"safe_sibling": "must-not-survive", "value": credential}

    assert connection_options_require_repair(options) is True
    projected = sanitize_connection_options_for_output(options)

    assert projected == {"_apex_repair_required": True}
    assert credential not in repr(projected)


@pytest.mark.parametrize(
    "options",
    [
        {"auth_mode": "bearer"},
        {"access_key": "non-secret-s3-access-key-id"},
        {"access_key_id": "non-secret-cloud-access-key-id"},
        {"aws_access_key_id": "non-secret-aws-access-key-id"},
        {"project_key": "PHX"},
        {"header": "application/json"},
        {"ordinary": "https://example.test/path"},
        {"tokenCount": "42"},
        {"tokenCountValue": "42"},
        {"signatureAlgorithm": "HMAC-SHA256"},
        {"signatureAlgorithmValue": "HMAC-SHA256"},
        {"authenticationMode": "bearer"},
        {"authenticationModeValue": "bearer"},
    ],
)
def test_known_nonsecret_connection_identifiers_remain_serializable(
    options: dict[str, str],
) -> None:
    assert connection_options_require_repair(options) is False
    assert sanitize_connection_options_for_output(options) == options


@pytest.mark.parametrize("field", ["accessToken", "sessionToken", "requestSignature"])
def test_terminal_token_and_signature_options_remain_credential_bearing(field: str) -> None:
    options = {field: "must-remain-secret"}

    assert connection_options_require_repair(options) is True
    with pytest.raises(ValueError, match="secret_ref"):
        reject_raw_secret_options(options)


def test_scoped_admin_cannot_create_adopt_or_enable_ambient_kubernetes_identity(
    repo: FakeConnectionsRepository,
) -> None:
    client = make_client(repo, identity(Role.ADMIN, [ScopeRef(project_id="demo")]))
    create = client.post(
        "/admin/connections",
        json={
            "kind": "cluster_inventory",
            "provider": "kubernetes",
            "name": "ambient-create",
            "project_id": "demo",
            "options": {"auth_mode": "in_cluster", "namespace": "kube-system"},
        },
    )
    repo.connections["ambient"] = _make_connection(
        id="ambient",
        kind="cluster_inventory",
        provider="kubernetes",
        name="ambient",
        project_id="demo",
        options={"auth_mode": "in-cluster", "namespace": "kube-system"},
    )
    repo.connections["bearer"] = _make_connection(
        id="bearer",
        kind="cluster_inventory",
        provider="kubernetes",
        name="bearer",
        project_id="demo",
        options={"auth_mode": "bearer", "namespace": "demo"},
    )

    assert create.status_code == 403
    assert [row["name"] for row in client.get("/admin/connections").json()] == ["bearer"]
    assert client.get("/admin/connections/ambient").status_code == 403
    assert client.post("/admin/connections/ambient/test").status_code == 403
    assert client.post("/admin/connections/ambient/enable").status_code == 403
    assert client.delete("/admin/connections/ambient").status_code == 403
    assert (
        client.patch(
            "/admin/connections/bearer",
            json={"options": {"auth_mode": "incluster", "namespace": "kube-system"}},
        ).status_code
        == 403
    )


def test_scoped_admin_cannot_attach_or_create_secret_connections(
    repo: FakeConnectionsRepository,
) -> None:
    client = make_client(repo, identity(Role.ADMIN, [ScopeRef(project_id="demo")]))
    secret_create = client.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "secret",
            "project_id": "demo",
            "secret_ref": "env:APEX_INTEGRATION_TRACKER_TOKEN",
        },
    )
    secrets_port_create = client.post(
        "/admin/connections",
        json={
            "kind": "secrets",
            "provider": "env",
            "name": "secrets-port",
            "project_id": "demo",
        },
    )
    plain_id = client.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "plain",
            "project_id": "demo",
        },
    ).json()["id"]

    assert secret_create.status_code == 403
    assert secrets_port_create.status_code == 403
    assert (
        client.patch(
            f"/admin/connections/{plain_id}",
            json={"secret_ref": "env:APEX_INTEGRATION_TRACKER_TOKEN"},
        ).status_code
        == 403
    )


def test_app_only_admin_cannot_manage_project_wide_connections(
    repo: FakeConnectionsRepository,
) -> None:
    repo.connections["demo"] = _make_connection(
        id="demo", kind="work_tracking", provider="stub", name="demo", project_id="demo"
    )
    client = make_client(
        repo,
        identity(Role.ADMIN, [ScopeRef(project_id="demo", app_id="app-a")]),
    )

    assert client.get("/admin/connections").json() == []
    assert client.get("/admin/connections/demo").status_code == 403
    assert (
        client.post(
            "/admin/connections",
            json={
                "kind": "work_tracking",
                "provider": "stub",
                "name": "app-wide",
                "project_id": "demo",
            },
        ).status_code
        == 403
    )
    assert client.patch("/admin/connections/demo", json={"name": "taken"}).status_code == 403
    assert client.post("/admin/connections/demo/disable").status_code == 403
    assert client.post("/admin/connections/demo/test").status_code == 403
    assert client.delete("/admin/connections/demo").status_code == 403


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
    assert response.json()["detail"] == "invalid connection target"


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
    assert response.json()["detail"] == "invalid connection target"


def test_create_rejects_private_s3_endpoint(admin: TestClient) -> None:
    response = admin.post(
        "/admin/connections",
        json={
            "kind": "artifact_store",
            "provider": "s3",
            "name": "private-s3",
            "options": {"endpoint": "http://169.254.169.254"},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid connection endpoint"


def test_platform_admin_can_explicitly_approve_private_s3_endpoint(admin: TestClient) -> None:
    response = admin.post(
        "/admin/connections",
        json={
            "kind": "artifact_store",
            "provider": "s3",
            "name": "approved-private-s3",
            "options": {
                "endpoint": "minio.apex.svc.cluster.local:9000",
                "_apex_trusted_private_host": True,
            },
        },
    )

    assert response.status_code == 201


def test_scoped_admin_cannot_set_reserved_private_host_approval(
    repo: FakeConnectionsRepository,
) -> None:
    client = make_client(repo, identity(Role.ADMIN, [ScopeRef(project_id="demo")]))
    response = client.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "approved-private",
            "project_id": "demo",
            "options": {"_apex_trusted_private_host": True},
        },
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "target",
    ["ftp://artifacts.example.com", "https://user:password@artifacts.example.com", "not-a-url"],
)
def test_create_rejects_malformed_or_credentialed_adapter_urls(
    admin: TestClient, target: str
) -> None:
    response = admin.post(
        "/admin/connections",
        json={
            "kind": "artifact_store",
            "provider": "s3",
            "name": f"invalid-{len(target)}",
            "options": {"endpoint": target},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid connection endpoint"
    assert target not in response.text


def test_create_defers_hostname_resolution_to_connect_time(admin: TestClient) -> None:
    response = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "dns-private-create",
            "base_url": "https://internal.example.test",
        },
    )

    assert response.status_code == 201


def test_patch_rejects_private_base_url(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "patch-private"},
    ).json()["id"]

    response = admin.patch(
        f"/admin/connections/{conn_id}", json={"base_url": "http://169.254.169.254"}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid connection target"


@pytest.mark.parametrize("field", ["name", "provider", "options"])
def test_patch_rejects_null_for_non_nullable_fields(admin: TestClient, field: str) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": f"nonnull-{field}"},
    ).json()["id"]

    response = admin.patch(f"/admin/connections/{conn_id}", json={field: None})

    assert response.status_code == 422


def test_patch_rejects_unknown_fields(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "no-typos"},
    ).json()["id"]

    response = admin.patch(f"/admin/connections/{conn_id}", json={"optoins": {}})

    assert response.status_code == 422


def test_enable_disable_and_delete(admin: TestClient) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "toggle-me"},
    ).json()["id"]
    assert admin.post(f"/admin/connections/{conn_id}/disable").json()["enabled"] is False
    assert admin.post(f"/admin/connections/{conn_id}/enable").json()["enabled"] is True
    assert admin.delete(f"/admin/connections/{conn_id}").status_code == 204
    assert admin.get(f"/admin/connections/{conn_id}").status_code == 404


def test_enable_rejects_legacy_unsafe_row_without_mutating_it(
    repo: FakeConnectionsRepository,
    admin: TestClient,
) -> None:
    connection = _make_connection(
        kind="work_tracking",
        provider="stub",
        name="legacy-disabled",
        base_url="https://operator:secret@example.test/api?token=signed",
    )
    connection.enabled = False
    repo.connections[connection.id] = connection

    response = admin.post(f"/admin/connections/{connection.id}/enable")

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid connection target"
    assert "secret" not in response.text
    assert "signed" not in response.text
    assert connection.enabled is False


def test_work_tracking_mutation_lease_blocks_disable_delete_and_affinity_edits(
    repo: FakeConnectionsRepository,
    admin: TestClient,
) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={
            "kind": "work_tracking",
            "provider": "stub",
            "name": "leased-tracker",
        },
    ).json()["id"]
    repo.reference_reason = "pending work-item mutations"

    disabled = admin.post(f"/admin/connections/{conn_id}/disable")
    deleted = admin.delete(f"/admin/connections/{conn_id}")
    affinity_edit = admin.patch(
        f"/admin/connections/{conn_id}",
        json={"base_url": "https://other-tracker.example"},
    )
    rename = admin.patch(
        f"/admin/connections/{conn_id}",
        json={"name": "leased-tracker-renamed"},
    )

    assert (
        disabled.status_code
        == deleted.status_code
        == affinity_edit.status_code
        == rename.status_code
        == 409
    )
    assert disabled.json()["detail"] == "connection is still referenced; migrate references first"
    assert "pending work-item mutations" not in disabled.text
    assert "pending work-item mutations" not in deleted.text
    assert "pending work-item mutations" not in affinity_edit.text
    assert "pending work-item mutations" not in rename.text


def test_work_tracking_reference_does_not_block_reenabling_connection(
    repo: FakeConnectionsRepository,
    admin: TestClient,
) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "resume-tracker"},
    ).json()["id"]
    repo.connections[conn_id].enabled = False
    configuration_version = repo.connections[conn_id].runtime_version
    repo.reference_reason = "pending work-item mutations"

    enabled = admin.post(f"/admin/connections/{conn_id}/enable")

    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert repo.connections[conn_id].runtime_version != configuration_version


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
    app = cast(FastAPI, admin.app)
    repo = app.dependency_overrides[get_connections_repository]()
    assert repo.for_update_calls.count(conn_id) >= 2


@pytest.mark.parametrize("field", ["pattern", "target"])
def test_host_mappings_reject_nul_text_before_repository_mutation(
    admin: TestClient,
    repo: FakeConnectionsRepository,
    field: str,
) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "mapped-nul"},
    ).json()["id"]
    mapping = {"pattern": "*.example", "target": "10.0.0.1"}
    mapping[field] += "\x00suffix"

    response = admin.put(
        f"/admin/connections/{conn_id}/host-mappings",
        json=[mapping],
    )

    assert response.status_code == 422
    assert repo.connections[conn_id].host_mappings == []
    assert repo.for_update_calls == []


def test_host_mappings_rejects_fanout_above_limit_before_replacement(
    admin: TestClient,
) -> None:
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "many-mappings"},
    ).json()["id"]

    response = admin.put(
        f"/admin/connections/{conn_id}/host-mappings",
        json=[{"pattern": f"host-{index}", "target": "target"} for index in range(257)],
    )

    assert response.status_code == 422
    assert admin.get(f"/admin/connections/{conn_id}/host-mappings").json() == []


# ── probe ────────────────────────────────────────────────────────────────────


async def test_artifact_probe_verifies_bytes_without_exposing_a_signed_url() -> None:
    class Store:
        def __init__(self) -> None:
            self.deleted: list[str] = []
            self.get_url_called = False

        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            assert data == b"probe"
            assert content_type == "text/plain"
            return StoredArtifact(key=key, uri=f"s3://private/{key}", size=len(data))

        async def iter_bytes(self, key: str, *, chunk_size: int):
            assert chunk_size == len(b"probe") + 1
            yield b"probe"

        async def get_url(self, key: str) -> str:
            self.get_url_called = True
            return "https://store.example/probe?X-Amz-Credential=secret"

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    store = Store()

    detail = await _probe_artifact_store(store)

    assert detail == "artifact round-trip succeeded"
    assert store.get_url_called is False
    assert len(store.deleted) == 1
    assert "secret" not in detail


async def test_artifact_probe_rejects_noncanonical_ack_and_still_cleans_up() -> None:
    class Store:
        def __init__(self) -> None:
            self.deleted: list[str] = []
            self.iter_called = False

        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            return StoredArtifact(key=key, uri="s3://private/wrong-key", size=len(data))

        async def iter_bytes(self, key: str, *, chunk_size: int):
            self.iter_called = True
            yield b"probe"

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    store = Store()

    with pytest.raises(RuntimeError, match="invalid object metadata"):
        await _probe_artifact_store(store)

    assert store.iter_called is False
    assert len(store.deleted) == 1


async def test_artifact_probe_never_invokes_unknown_model_dump_and_cleans_up() -> None:
    class HostileAcknowledgement:
        called = False

        def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
            self.called = True
            raise AssertionError("provider model_dump must not be called")

    acknowledgement = HostileAcknowledgement()

    class Store:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def put(self, key: str, data: bytes, *, content_type: str) -> Any:
            return acknowledgement

        async def iter_bytes(self, key: str, *, chunk_size: int):
            raise AssertionError("invalid acknowledgement must fail before read")
            yield b""  # pragma: no cover - makes this an async generator

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    store = Store()

    with pytest.raises(RuntimeError, match="invalid object metadata"):
        await _probe_artifact_store(store)

    assert acknowledgement.called is False
    assert len(store.deleted) == 1


async def test_artifact_probe_rejects_bytes_subclass_without_invoking_equality() -> None:
    class HostileBytes(bytes):
        called = False

        def __eq__(self, other: object) -> bool:
            self.called = True
            raise AssertionError("provider bytes equality must not be called")

    payload = HostileBytes(b"probe")

    class Store:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            return StoredArtifact(key=key, uri=f"s3://private/{key}", size=len(data))

        async def iter_bytes(self, key: str, *, chunk_size: int):
            yield payload

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    store = Store()

    with pytest.raises(RuntimeError, match="bounded payload"):
        await _probe_artifact_store(store)

    assert payload.called is False
    assert len(store.deleted) == 1


async def test_artifact_probe_requires_cleanup_support_before_upload() -> None:
    class StoreWithoutDelete:
        def __init__(self) -> None:
            self.put_called = False

        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            self.put_called = True
            return StoredArtifact(key=key, uri=f"s3://private/{key}", size=len(data))

    store = StoreWithoutDelete()

    with pytest.raises(ValueError, match="safe bounded probe"):
        await _probe_artifact_store(store)
    assert store.put_called is False


async def test_artifact_probe_waits_for_cleanup_when_cancelled() -> None:
    delete_started = asyncio.Event()
    release_delete = asyncio.Event()

    class Store:
        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            return StoredArtifact(key=key, uri=f"s3://private/{key}", size=len(data))

        async def iter_bytes(self, key: str, *, chunk_size: int):
            yield b"probe"

        async def delete(self, key: str) -> None:
            delete_started.set()
            await release_delete.wait()

    task = asyncio.create_task(_probe_artifact_store(Store()))
    await delete_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)

    assert task.done() is False
    release_delete.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_artifact_probe_bounds_streamed_provider_output_and_cleans_up() -> None:
    class Store:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            return StoredArtifact(key=key, uri=f"s3://private/{key}", size=len(data))

        async def iter_bytes(self, key: str, *, chunk_size: int):
            yield b"probe"
            yield b"provider-controlled-overflow"

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    store = Store()
    with pytest.raises(RuntimeError, match="bounded payload"):
        await _probe_artifact_store(store)
    assert len(store.deleted) == 1


async def test_artifact_probe_timeout_can_interrupt_endless_empty_chunks() -> None:
    class Store:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            return StoredArtifact(key=key, uri=f"s3://private/{key}", size=len(data))

        async def iter_bytes(self, key: str, *, chunk_size: int):
            while True:
                await asyncio.sleep(0)
                yield b""

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    store = Store()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await _probe_artifact_store(store)
    assert len(store.deleted) == 1


async def test_work_tracking_probe_never_invokes_unknown_model_dump() -> None:
    class HostileWorkItem(dict[str, Any]):
        called = False

        def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
            self.called = True
            raise AssertionError("provider model_dump must not be called")

        def __iter__(self) -> Iterator[str]:
            self.called = True
            raise AssertionError("provider mapping must not be iterated")

        def __len__(self) -> int:
            self.called = True
            raise AssertionError("provider mapping length must not be read")

    item = HostileWorkItem()

    class Adapter:
        async def get_item(self, key: str) -> Any:
            return item

    with pytest.raises(RuntimeError, match="invalid work item"):
        await _probe_work_tracking(Adapter())

    assert item.called is False


async def test_log_probe_rejects_hostile_entry_list_without_traversal() -> None:
    class HostileEntries(list[LogEntry]):
        called = False

        def __len__(self) -> int:
            self.called = True
            raise AssertionError("provider list length must not be called")

        def __iter__(self) -> Iterator[LogEntry]:
            self.called = True
            raise AssertionError("provider list iteration must not be called")

    entries = HostileEntries([LogEntry(at="2026-06-10T11:59:59Z", message="safe")])
    result = LogSearchResult.model_construct(entries=entries, total=1)

    class Adapter:
        async def search(
            self,
            query: LogQuery,
            *,
            window: TimeWindow,
            page: Page,
        ) -> LogSearchResult:
            return result

    with pytest.raises(RuntimeError, match="invalid log result"):
        await _probe_log_search(Adapter())

    assert entries.called is False


async def test_log_probe_rejects_arbitrary_entry_without_reading_spoofed_class() -> None:
    class HostileEntry:
        class_called = False

        def __getattribute__(self, name: str) -> Any:
            if name == "__class__":
                type(self).class_called = True
                raise AssertionError("provider __class__ descriptor must not be called")
            return object.__getattribute__(self, name)

    entry = HostileEntry()
    result = LogSearchResult.model_construct(entries=[entry], total=1)

    class Adapter:
        async def search(
            self,
            query: LogQuery,
            *,
            window: TimeWindow,
            page: Page,
        ) -> LogSearchResult:
            return result

    with pytest.raises(RuntimeError, match="invalid provider model"):
        await _probe_log_search(Adapter())

    assert entry.class_called is False


async def test_document_probe_rejects_hostile_result_list_without_traversal() -> None:
    class HostileHits(list[Any]):
        called = False

        def __len__(self) -> int:
            self.called = True
            raise AssertionError("provider list length must not be called")

        def __iter__(self) -> Iterator[Any]:
            self.called = True
            raise AssertionError("provider list iteration must not be called")

    hits = HostileHits()

    class Adapter:
        async def search(self, query: str, *, scope: Any, k: int) -> Any:
            return hits

    with pytest.raises(RuntimeError, match="invalid result list"):
        await _probe_documents(Adapter())

    assert hits.called is False


async def test_probe_rejects_forged_model_key_before_rehashing_it() -> None:
    class HostileKey(str):
        hash_calls = 0

        def __hash__(self) -> int:
            self.hash_calls += 1
            if self.hash_calls > 1:
                raise AssertionError("forged provider key must not be rehashed")
            return str.__hash__(self)

    file = FileContent(path="README.md", text="safe")
    state = cast(dict[Any, Any], file.__dict__)
    state.pop("media_type")
    key = HostileKey("media_type")
    state[key] = "text/plain"

    class Adapter:
        async def get_file(self, repo: Any, path: str) -> FileContent:
            return file

    with pytest.raises(RuntimeError, match="invalid provider model"):
        await _probe_source_control(Adapter())

    assert key.hash_calls == 1


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


def test_successful_probe_detail_redacts_provider_credential_material(
    admin: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bearer = "probe-bearer-secret-canary"
    signed = "probe-signed-query-secret-canary"

    async def credential_shaped_success(_adapter: Any) -> str:
        return f"Bearer {bearer}; https://provider.test/object?X-Amz-Signature={signed}"

    monkeypatch.setitem(PROBE_CALLS, PortKind.WORK_TRACKING, credential_shaped_success)
    conn_id = admin.post(
        "/admin/connections",
        json={"kind": "work_tracking", "provider": "stub", "name": "probe-redaction"},
    ).json()["id"]

    response = admin.post(f"/admin/connections/{conn_id}/test")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert bearer not in response.text
    assert signed not in response.text
    assert "[REDACTED]" in response.json()["detail"]


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


@pytest.mark.parametrize("probe_fails", [False, True])
def test_probe_closes_temporary_adapter(admin: TestClient, probe_fails: bool) -> None:
    provider = f"test-closable-{'failure' if probe_fails else 'success'}"
    instances: list[Any] = []

    class ClosableAdapter:
        def __init__(
            self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
        ) -> None:
            self.closed = False
            instances.append(self)

        async def get_item(self, key: str) -> Any:
            if probe_fails:
                raise RuntimeError("probe failed")
            return WorkItem(key=key, title="connection probe")

        async def aclose(self) -> None:
            self.closed = True

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(ClosableAdapter)
    try:
        conn_id = admin.post(
            "/admin/connections",
            json={"kind": "work_tracking", "provider": provider, "name": provider},
        ).json()["id"]
        response = admin.post(f"/admin/connections/{conn_id}/test")
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)

    assert response.status_code == 200
    assert response.json()["ok"] is (not probe_fails)
    assert len(instances) == 1
    assert instances[0].closed is True


def test_probe_timeout_is_inline_and_still_closes_adapter(
    admin: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = "test-probe-timeout"
    instances: list[Any] = []

    class HangingAdapter:
        def __init__(
            self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
        ) -> None:
            self.closed = False
            instances.append(self)

        async def get_item(self, key: str) -> Any:
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(HangingAdapter)
    monkeypatch.setattr("apex.routers.connections.CONNECTION_PROBE_TIMEOUT_S", 0.01)
    try:
        conn_id = admin.post(
            "/admin/connections",
            json={"kind": "work_tracking", "provider": provider, "name": provider},
        ).json()["id"]
        response = admin.post(f"/admin/connections/{conn_id}/test")
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["detail"] == "connection probe failed; check server logs for details"
    assert len(instances) == 1
    assert instances[0].closed is True


def test_probe_releases_nested_secret_checkout(
    admin: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = "test-probe-secret-release"

    class SecretCheckout:
        def __init__(self) -> None:
            self.released = False

        async def resolve(self, secret_ref: str) -> SecretValue:
            assert secret_ref == "env:APEX_INTEGRATION_TRACKER_TOKEN"
            return SecretValue(value="probe-secret")

        async def aclose(self) -> None:
            self.released = True

    secret_checkout = SecretCheckout()

    class Resolver:
        async def resolve(self, kind: PortKind) -> SecretCheckout:
            assert kind is PortKind.SECRETS
            return secret_checkout

    class Adapter:
        def __init__(
            self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
        ) -> None:
            assert secret is not None

        async def get_item(self, key: str) -> WorkItem:
            return WorkItem(key=key, title="connection probe")

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(Adapter)
    monkeypatch.setattr("apex.routers.connections.get_connection_resolver", Resolver)
    try:
        conn_id = admin.post(
            "/admin/connections",
            json={
                "kind": "work_tracking",
                "provider": provider,
                "name": provider,
                "secret_ref": "env:APEX_INTEGRATION_TRACKER_TOKEN",
            },
        ).json()["id"]
        response = admin.post(f"/admin/connections/{conn_id}/test")
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert secret_checkout.released is True


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
    assert body["detail"] == "connection probe configuration is invalid"


def test_probe_unknown_connection_is_404(admin: TestClient) -> None:
    assert admin.post("/admin/connections/nope/test").status_code == 404
