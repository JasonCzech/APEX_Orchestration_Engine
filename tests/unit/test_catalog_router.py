"""Catalog router: CRUD, role gating, and project scoping via a fake repository."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import (
    Application,
    Environment,
    EnvironmentHost,
    EnvironmentSnapshot,
)
from apex.persistence.repositories.catalog import CatalogRepository, DuplicateNameError
from apex.routers.catalog import (
    ApplicationOut,
    EnvironmentCreate,
    EnvironmentOut,
    get_catalog_repository,
    router,
)


def _now() -> datetime:
    return datetime.now(UTC)


def test_environment_create_rejects_nul_application_id() -> None:
    with pytest.raises(ValueError, match="string_pattern_mismatch"):
        EnvironmentCreate.model_validate({"application_id": "app\x00id", "name": "staging"})


def test_catalog_api_rejects_credentials_in_scalar_labels_without_reflection(
    repo: "FakeCatalogRepository",
) -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_catalog_repository] = lambda: repo
    app.dependency_overrides[get_current_identity] = lambda: ADMIN
    client = TestClient(app)

    unsafe_application = client.post(
        "/catalog/applications",
        json={"project_id": "demo", "name": credential},
    )
    safe_application = client.post(
        "/catalog/applications",
        json={"project_id": "demo", "name": "Checkout"},
    )
    app_id = safe_application.json()["id"]
    unsafe_description = client.patch(
        f"/catalog/applications/{app_id}", json={"description": credential}
    )
    unsafe_environment = client.post(
        "/catalog/environments",
        json={"application_id": app_id, "name": credential},
    )
    unsafe_host_role = client.post(
        "/catalog/environments",
        json={
            "application_id": app_id,
            "name": "staging",
            "hosts": [{"hostname": "api.example.test", "role": credential}],
        },
    )

    for response in (
        unsafe_application,
        unsafe_description,
        unsafe_environment,
        unsafe_host_role,
    ):
        assert response.status_code == 422
        assert credential.encode() not in response.content
    assert len(repo.applications) == 1
    assert repo.environments == {}


def _make_application(project_id: str, name: str, description: str | None = None) -> Application:
    app = Application(id=uuid4().hex, project_id=project_id, name=name, description=description)
    app.archived_at = None
    app.created_at = _now()
    app.updated_at = _now()
    return app


def _make_hosts(hosts: Sequence[dict[str, Any]]) -> list[EnvironmentHost]:
    return [
        EnvironmentHost(id=uuid4().hex, hostname=h["hostname"], role=h.get("role")) for h in hosts
    ]


def _make_environment(app: Application, name: str, **kwargs: Any) -> Environment:
    env = Environment(
        id=uuid4().hex,
        application_id=app.id,
        name=name,
        kind=kwargs.get("kind"),
        base_url=kwargs.get("base_url"),
        options=dict(kwargs.get("options") or {}),
        target_approved=kwargs.get("target_approved", False),
        target_version=kwargs.get("target_version", 0),
    )
    env.application = app
    env.hosts = _make_hosts(kwargs.get("hosts") or [])
    env.created_at = _now()
    env.updated_at = _now()
    return env


def test_catalog_legacy_credential_labels_are_redacted_from_output() -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    app = _make_application("demo", credential, description=credential)
    environment = _make_environment(
        app,
        credential,
        kind=credential,
        hosts=[{"hostname": credential, "role": credential}],
    )

    app_output = ApplicationOut.model_validate(app)
    environment_output = EnvironmentOut.model_validate(environment)

    assert app_output.name == "[REDACTED]"
    assert app_output.description == "[REDACTED]"
    assert environment_output.name == "[REDACTED]"
    assert environment_output.kind == "[REDACTED]"
    assert environment_output.hosts[0].hostname == "[REDACTED]"
    assert environment_output.hosts[0].role == "[REDACTED]"


class FakeCatalogRepository:
    """In-memory stand-in mirroring CatalogRepository semantics (incl. dupes)."""

    def __init__(self) -> None:
        self.applications: dict[str, Application] = {}
        self.environments: dict[str, Environment] = {}
        self.snapshots: dict[str, list[EnvironmentSnapshot]] = {}
        self.environment_for_update_calls: list[str] = []
        self.application_list_calls = 0
        self.environment_list_calls = 0

    # applications

    async def list_applications(
        self,
        *,
        project: str | None = None,
        visible_projects: Sequence[str] | None = None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Application]:
        self.application_list_calls += 1
        apps = list(self.applications.values())
        if project is not None:
            apps = [a for a in apps if a.project_id == project]
        if visible_projects is not None:
            apps = [a for a in apps if a.project_id in visible_projects]
        if allowed_scopes is not None:
            project_wide = {scope.project_id for scope in allowed_scopes if scope.app_id is None}
            exact = {(scope.project_id, scope.app_id) for scope in allowed_scopes}
            apps = [
                app
                for app in apps
                if app.project_id in project_wide or (app.project_id, app.id) in exact
            ]
        if not include_archived:
            apps = [a for a in apps if a.archived_at is None]
        return sorted(apps, key=lambda a: (a.project_id, a.name))[offset : offset + limit]

    async def get_application(self, application_id: str) -> Application | None:
        return self.applications.get(application_id)

    async def create_application(
        self, *, project_id: str, name: str, description: str | None = None
    ) -> Application:
        if any(a.project_id == project_id and a.name == name for a in self.applications.values()):
            raise DuplicateNameError(name)
        app = _make_application(project_id, name, description)
        self.applications[app.id] = app
        return app

    async def update_application(self, app: Application, changes: dict[str, Any]) -> Application:
        new_name = changes.get("name")
        if new_name is not None and any(
            a.id != app.id and a.project_id == app.project_id and a.name == new_name
            for a in self.applications.values()
        ):
            raise DuplicateNameError(new_name)
        for field, value in changes.items():
            setattr(app, field, value)
        app.updated_at = _now()
        return app

    async def set_application_archived(self, app: Application, archived: bool) -> Application:
        app.archived_at = _now() if archived else None
        return app

    async def delete_application(self, app: Application) -> None:
        self.applications.pop(app.id, None)
        for env_id in [e.id for e in self.environments.values() if e.application_id == app.id]:
            self.environments.pop(env_id, None)

    # environments

    async def list_environments(
        self,
        *,
        application_id: str | None = None,
        visible_projects: Sequence[str] | None = None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Environment]:
        self.environment_list_calls += 1
        envs = list(self.environments.values())
        if application_id is not None:
            envs = [e for e in envs if e.application_id == application_id]
        if visible_projects is not None:
            envs = [e for e in envs if e.application.project_id in visible_projects]
        if allowed_scopes is not None:
            project_wide = {scope.project_id for scope in allowed_scopes if scope.app_id is None}
            exact = {(scope.project_id, scope.app_id) for scope in allowed_scopes}
            envs = [
                env
                for env in envs
                if env.application.project_id in project_wide
                or (env.application.project_id, env.application_id) in exact
            ]
        return sorted(envs, key=lambda e: (e.application.project_id, e.name))[
            offset : offset + limit
        ]

    async def get_environment(self, environment_id: str) -> Environment | None:
        return self.environments.get(environment_id)

    async def get_environment_for_update(self, environment_id: str) -> Environment | None:
        self.environment_for_update_calls.append(environment_id)
        return self.environments.get(environment_id)

    async def create_environment(
        self,
        *,
        application_id: str,
        name: str,
        kind: str | None = None,
        base_url: str | None = None,
        target_approved: bool = False,
        target_version: int = 0,
        options: dict[str, Any] | None = None,
        hosts: Sequence[dict[str, Any]] = (),
    ) -> Environment:
        if any(
            e.application_id == application_id and e.name == name
            for e in self.environments.values()
        ):
            raise DuplicateNameError(name)
        app = self.applications[application_id]
        env = _make_environment(
            app,
            name,
            kind=kind,
            base_url=base_url,
            target_approved=target_approved,
            target_version=target_version,
            options=options,
            hosts=list(hosts),
        )
        self.environments[env.id] = env
        return env

    async def update_environment(
        self,
        env: Environment,
        changes: dict[str, Any],
        hosts: Sequence[dict[str, Any]] | None = None,
    ) -> Environment:
        for field, value in changes.items():
            setattr(env, field, value)
        if hosts is not None:
            env.hosts = _make_hosts(hosts)
        env.updated_at = _now()
        return env

    async def delete_environment(self, env: Environment) -> None:
        self.environments.pop(env.id, None)

    async def latest_snapshot(self, environment_id: str) -> EnvironmentSnapshot | None:
        snaps = self.snapshots.get(environment_id, [])
        return max(snaps, key=lambda s: s.scanned_at) if snaps else None


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


def make_client(repo: FakeCatalogRepository, who: ConsumerIdentity) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_catalog_repository] = lambda: repo
    app.dependency_overrides[get_current_identity] = lambda: who
    return TestClient(app)


@pytest.fixture
def repo() -> FakeCatalogRepository:
    return FakeCatalogRepository()


# ── applications ─────────────────────────────────────────────────────────────


def test_create_get_list_application(repo: FakeCatalogRepository) -> None:
    client = make_client(repo, ADMIN)
    created = client.post(
        "/catalog/applications",
        json={"project_id": "demo", "name": "Checkout", "description": "demo app"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["project_id"] == "demo"
    assert body["name"] == "Checkout"
    assert body["archived_at"] is None

    fetched = client.get(f"/catalog/applications/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]

    listed = client.get("/catalog/applications")
    assert [a["name"] for a in listed.json()] == ["Checkout"]


def test_catalog_lists_reject_huge_offset_before_repository(
    repo: FakeCatalogRepository,
) -> None:
    client = make_client(repo, ADMIN)

    applications = client.get("/catalog/applications", params={"offset": 10_001})
    environments = client.get("/catalog/environments", params={"offset": 10_001})

    assert applications.status_code == 422
    assert environments.status_code == 422
    assert repo.application_list_calls == 0
    assert repo.environment_list_calls == 0


def test_duplicate_application_name_is_409(repo: FakeCatalogRepository) -> None:
    client = make_client(repo, ADMIN)
    payload = {"project_id": "demo", "name": "Checkout"}
    assert client.post("/catalog/applications", json=payload).status_code == 201
    assert client.post("/catalog/applications", json=payload).status_code == 409


def test_application_rejects_scope_longer_than_database_column_before_write(
    repo: FakeCatalogRepository,
) -> None:
    response = make_client(repo, ADMIN).post(
        "/catalog/applications",
        json={"project_id": "p" * 256, "name": "Checkout"},
    )

    assert response.status_code == 422
    assert repo.applications == {}


def test_update_application(repo: FakeCatalogRepository) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]
    patched = client.patch(f"/catalog/applications/{app_id}", json={"description": "updated"})
    assert patched.status_code == 200
    assert patched.json()["description"] == "updated"
    assert patched.json()["name"] == "Checkout"  # untouched fields preserved


def test_patch_rejects_null_nonnullable_names_before_repository_mutation(
    repo: FakeCatalogRepository,
) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]
    environment = client.post(
        "/catalog/environments",
        json={"application_id": app_id, "name": "staging"},
    ).json()

    app_response = client.patch(f"/catalog/applications/{app_id}", json={"name": None})
    environment_response = client.patch(
        f"/catalog/environments/{environment['id']}", json={"name": None}
    )

    assert app_response.status_code == 422
    assert environment_response.status_code == 422
    assert repo.applications[app_id].name == "Checkout"
    assert repo.environments[environment["id"]].name == "staging"


def test_archive_and_unarchive(repo: FakeCatalogRepository) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]

    archived = client.post(f"/catalog/applications/{app_id}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    assert client.get("/catalog/applications").json() == []
    included = client.get("/catalog/applications", params={"include_archived": True}).json()
    assert [a["id"] for a in included] == [app_id]

    restored = client.post(f"/catalog/applications/{app_id}/unarchive")
    assert restored.json()["archived_at"] is None
    assert [a["id"] for a in client.get("/catalog/applications").json()] == [app_id]


def test_role_gating_on_application_mutations(repo: FakeCatalogRepository) -> None:
    viewer = make_client(repo, VIEWER_DEMO)
    operator = make_client(repo, OPERATOR_DEMO)
    admin = make_client(repo, ADMIN)
    payload = {"project_id": "demo", "name": "Checkout"}

    assert viewer.post("/catalog/applications", json=payload).status_code == 403

    created = operator.post("/catalog/applications", json=payload)
    assert created.status_code == 201
    app_id = created.json()["id"]

    assert operator.delete(f"/catalog/applications/{app_id}").status_code == 403  # admin-only
    assert admin.delete(f"/catalog/applications/{app_id}").status_code == 204
    assert admin.get(f"/catalog/applications/{app_id}").status_code == 404


def test_project_scoping_on_applications(repo: FakeCatalogRepository) -> None:
    demo_app = _make_application("demo", "Checkout")
    other_app = _make_application("other", "Billing")
    repo.applications = {a.id: a for a in (demo_app, other_app)}

    scoped = make_client(repo, OPERATOR_DEMO)
    assert [a["name"] for a in scoped.get("/catalog/applications").json()] == ["Checkout"]
    assert scoped.get(f"/catalog/applications/{other_app.id}").status_code == 404
    assert (
        scoped.patch(f"/catalog/applications/{other_app.id}", json={"name": "X"}).status_code == 404
    )
    assert (
        scoped.post(
            "/catalog/applications", json={"project_id": "other", "name": "Nope"}
        ).status_code
        == 403
    )

    unscoped = make_client(repo, ADMIN)
    assert len(unscoped.get("/catalog/applications").json()) == 2


def test_app_only_operator_cannot_create_project_wide_application(
    repo: FakeCatalogRepository,
) -> None:
    app_only = ConsumerIdentity(
        consumer_id="app-op",
        name="app-op",
        consumer_type=ConsumerType.INTERNAL,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="demo", app_id="existing-app")],
    )
    client = make_client(repo, app_only)

    response = client.post(
        "/catalog/applications",
        json={"project_id": "demo", "name": "Sibling"},
    )

    assert response.status_code == 403
    assert repo.applications == {}


# ── environments ─────────────────────────────────────────────────────────────


def test_environment_crud_with_hosts(repo: FakeCatalogRepository) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]

    created = client.post(
        "/catalog/environments",
        json={
            "application_id": app_id,
            "name": "staging-2",
            "kind": "vm",
            "hosts": [
                {"hostname": "app-01.local", "role": "app"},
                {"hostname": "db-01.local", "role": "db"},
            ],
        },
    )
    assert created.status_code == 201
    env = created.json()
    assert [h["hostname"] for h in env["hosts"]] == ["app-01.local", "db-01.local"]
    assert env["last_snapshot"] is None
    assert env["target_approved"] is False
    assert env["target_version"] == 0

    # hosts replace-on-update: PATCH with hosts swaps the entire list
    patched = client.patch(
        f"/catalog/environments/{env['id']}",
        json={"hosts": [{"hostname": "app-02.local", "role": "app"}]},
    )
    assert patched.status_code == 200
    assert [h["hostname"] for h in patched.json()["hosts"]] == ["app-02.local"]

    # PATCH without hosts leaves them alone
    renamed = client.patch(f"/catalog/environments/{env['id']}", json={"name": "staging-3"})
    assert renamed.json()["name"] == "staging-3"
    assert [h["hostname"] for h in renamed.json()["hosts"]] == ["app-02.local"]

    listed = client.get("/catalog/environments", params={"application": app_id})
    assert [e["id"] for e in listed.json()] == [env["id"]]

    assert client.delete(f"/catalog/environments/{env['id']}").status_code == 204
    assert client.get(f"/catalog/environments/{env['id']}").status_code == 404


def test_environment_rejects_deep_options_and_host_fanout_before_write(
    repo: FakeCatalogRepository,
) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(17):
        child: dict[str, Any] = {}
        cursor["child"] = child
        cursor = child

    deep = client.post(
        "/catalog/environments",
        json={"application_id": app_id, "name": "deep", "options": nested},
    )
    fanout = client.post(
        "/catalog/environments",
        json={
            "application_id": app_id,
            "name": "many-hosts",
            "hosts": [{"hostname": f"host-{index}"} for index in range(257)],
        },
    )

    assert deep.status_code == 422
    assert fanout.status_code == 422
    assert repo.environments == {}


def test_environment_writes_reject_raw_credentials_in_options(
    repo: FakeCatalogRepository,
) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]

    created = client.post(
        "/catalog/environments",
        json={
            "application_id": app_id,
            "name": "unsafe",
            "options": {"nested": {"api_token": "raw-secret"}},
        },
    )

    assert created.status_code == 422
    assert "raw-secret" not in created.text
    assert repo.environments == {}

    environment = _make_environment(app=repo.applications[app_id], name="existing")
    repo.environments[environment.id] = environment
    updated = client.patch(
        f"/catalog/environments/{environment.id}",
        json={"options": {"password": "raw-secret"}},
    )

    assert updated.status_code == 422
    assert "raw-secret" not in updated.text
    assert environment.options == {}


@pytest.mark.parametrize(
    "credential",
    [
        "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhcGV4LXVzZXIifQ.c2lnbmF0dXJlLWNhbmFyeQ",
        "-----BEGIN PRIVATE KEY-----\ncHJpdmF0ZS1rZXktY2FuYXJ5\n-----END PRIVATE KEY-----",
    ],
)
def test_environment_api_rejects_standalone_credential_signatures(
    repo: FakeCatalogRepository,
    credential: str,
) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]

    response = client.post(
        "/catalog/environments",
        json={
            "application_id": app_id,
            "name": "unsafe-signature",
            "options": {"value": credential},
        },
    )

    assert response.status_code == 422
    assert credential not in response.text
    assert repo.environments == {}


def test_environment_writes_reject_server_owned_repair_marker(
    repo: FakeCatalogRepository,
) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]

    created = client.post(
        "/catalog/environments",
        json={
            "application_id": app_id,
            "name": "forged-quarantine",
            "options": {"_apex_repair_required": True},
        },
    )

    assert created.status_code == 422
    assert repo.environments == {}


def test_environment_output_redacts_credential_bearing_legacy_rows() -> None:
    app = _make_application("demo", "Checkout")
    environment = _make_environment(
        app,
        "legacy",
        base_url="https://operator:super-secret@example.com/run?token=signed-secret",
        options={
            "password": "plain-secret",
            "safe_sibling": "must-not-create-a-partial-sanitization-bypass",
        },
        target_approved=True,
        target_version=1,
    )

    projected = EnvironmentOut.model_validate(environment).model_dump(mode="json")

    assert projected["base_url"] == "[REDACTED]"
    assert projected["options"] == {"_apex_repair_required": True}
    assert projected["target_approved"] is False
    assert "super-secret" not in str(projected)
    assert "signed-secret" not in str(projected)
    assert "plain-secret" not in str(projected)


async def test_catalog_repository_rejects_unsafe_target_metadata_before_session_io() -> None:
    repository = CatalogRepository(cast(Any, object()))

    with pytest.raises(ValueError, match="credential-bearing"):
        await repository.create_environment(
            application_id="app-1",
            name="unsafe",
            base_url="https://user:secret@example.test/run",
            target_approved=True,
            target_version=1,
        )
    with pytest.raises(ValueError, match="managed connection secret_ref"):
        await repository.create_environment(
            application_id="app-1",
            name="unsafe",
            options={"nested": {"api_token": "raw-secret"}},
        )
    with pytest.raises(ValueError, match="credential-bearing"):
        await repository.create_environment(
            application_id="app-1",
            name="forged-quarantine",
            options={"_apex_repair_required": True},
        )


async def test_catalog_repository_validates_effective_target_before_mutating_row() -> None:
    repository = CatalogRepository(cast(Any, object()))
    application = _make_application("demo", "Checkout")
    environment = _make_environment(
        application,
        "safe",
        base_url="https://8.8.8.8/load",
        options={},
        target_approved=True,
        target_version=1,
    )

    with pytest.raises(ValueError, match="managed connection secret_ref"):
        await repository.update_environment(
            environment,
            {"options": {"password": "raw-secret"}},
        )

    assert environment.options == {}
    assert environment.base_url == "https://8.8.8.8/load"


def test_scoped_operator_cannot_create_or_change_execution_target(
    repo: FakeCatalogRepository,
) -> None:
    admin = make_client(repo, ADMIN)
    app_id = admin.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]
    operator = make_client(repo, OPERATOR_DEMO)

    denied_create = operator.post(
        "/catalog/environments",
        json={
            "application_id": app_id,
            "name": "unsafe",
            "base_url": "http://169.254.169.254/latest/meta-data",
        },
    )
    plain = operator.post(
        "/catalog/environments",
        json={"application_id": app_id, "name": "plain"},
    )

    assert denied_create.status_code == 403
    assert plain.status_code == 201
    assert (
        operator.patch(
            f"/catalog/environments/{plain.json()['id']}",
            json={"base_url": "https://8.8.8.8/load"},
        ).status_code
        == 403
    )


def test_platform_admin_approves_and_versions_execution_target(
    repo: FakeCatalogRepository,
) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]
    created = client.post(
        "/catalog/environments",
        json={
            "application_id": app_id,
            "name": "approved",
            "base_url": "https://8.8.8.8/load",
        },
    )

    assert created.status_code == 201
    assert created.json()["target_approved"] is True
    assert created.json()["target_version"] == 1

    cleared = client.patch(f"/catalog/environments/{created.json()['id']}", json={"base_url": None})
    assert cleared.status_code == 200
    assert cleared.json()["target_approved"] is False
    assert cleared.json()["target_version"] == 2


def test_private_execution_target_requires_platform_approval_marker(
    repo: FakeCatalogRepository,
) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]
    payload: dict[str, Any] = {
        "application_id": app_id,
        "name": "private",
        "base_url": "http://10.0.0.8/load",
    }

    assert client.post("/catalog/environments", json=payload).status_code == 422
    payload["options"] = {"_apex_trusted_private_host": True}
    approved = client.post("/catalog/environments", json=payload)
    assert approved.status_code == 201
    assert approved.json()["target_approved"] is True


def test_environment_snapshot_summary(repo: FakeCatalogRepository) -> None:
    app = _make_application("demo", "Checkout")
    env = _make_environment(app, "staging-2")
    repo.applications[app.id] = app
    repo.environments[env.id] = env
    repo.snapshots[env.id] = [
        EnvironmentSnapshot(
            id=uuid4().hex,
            environment_id=env.id,
            scanned_at=datetime(2026, 6, 1, tzinfo=UTC),
            data={"services": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
        )
    ]

    fetched = make_client(repo, ADMIN).get(f"/catalog/environments/{env.id}")
    assert fetched.status_code == 200
    snapshot = fetched.json()["last_snapshot"]
    assert snapshot["service_count"] == 3
    assert snapshot["scanned_at"].startswith("2026-06-01")


def test_environment_duplicate_name_is_409(repo: FakeCatalogRepository) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]
    payload = {"application_id": app_id, "name": "staging-2"}
    assert client.post("/catalog/environments", json=payload).status_code == 201
    assert client.post("/catalog/environments", json=payload).status_code == 409


def test_environment_scoping_inherits_application_project(
    repo: FakeCatalogRepository,
) -> None:
    other_app = _make_application("other", "Billing")
    other_env = _make_environment(other_app, "prod")
    repo.applications[other_app.id] = other_app
    repo.environments[other_env.id] = other_env

    scoped = make_client(repo, OPERATOR_DEMO)
    assert scoped.get("/catalog/environments").json() == []
    assert scoped.get(f"/catalog/environments/{other_env.id}").status_code == 404
    assert (
        scoped.post(
            "/catalog/environments",
            json={"application_id": other_app.id, "name": "staging"},
        ).status_code
        == 404
    )
    assert scoped.delete(f"/catalog/environments/{other_env.id}").status_code == 404

    viewer = make_client(repo, VIEWER_DEMO)
    assert (
        viewer.post(
            "/catalog/environments", json={"application_id": other_app.id, "name": "x"}
        ).status_code
        == 403
    )  # role gate fires before scoping
