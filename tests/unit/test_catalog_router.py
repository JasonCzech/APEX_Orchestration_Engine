"""Catalog router: CRUD, role gating, and project scoping via a fake repository."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import (
    Application,
    Environment,
    EnvironmentHost,
    EnvironmentSnapshot,
)
from apex.persistence.repositories.catalog import DuplicateNameError
from apex.routers.catalog import get_catalog_repository, router


def _now() -> datetime:
    return datetime.now(UTC)


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
    )
    env.application = app
    env.hosts = _make_hosts(kwargs.get("hosts") or [])
    env.created_at = _now()
    env.updated_at = _now()
    return env


class FakeCatalogRepository:
    """In-memory stand-in mirroring CatalogRepository semantics (incl. dupes)."""

    def __init__(self) -> None:
        self.applications: dict[str, Application] = {}
        self.environments: dict[str, Environment] = {}
        self.snapshots: dict[str, list[EnvironmentSnapshot]] = {}

    # applications

    async def list_applications(
        self,
        *,
        project: str | None = None,
        visible_projects: Sequence[str] | None = None,
        include_archived: bool = False,
    ) -> list[Application]:
        apps = list(self.applications.values())
        if project is not None:
            apps = [a for a in apps if a.project_id == project]
        if visible_projects is not None:
            apps = [a for a in apps if a.project_id in visible_projects]
        if not include_archived:
            apps = [a for a in apps if a.archived_at is None]
        return sorted(apps, key=lambda a: (a.project_id, a.name))

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
    ) -> list[Environment]:
        envs = list(self.environments.values())
        if application_id is not None:
            envs = [e for e in envs if e.application_id == application_id]
        if visible_projects is not None:
            envs = [e for e in envs if e.application.project_id in visible_projects]
        return sorted(envs, key=lambda e: (e.application.project_id, e.name))

    async def get_environment(self, environment_id: str) -> Environment | None:
        return self.environments.get(environment_id)

    async def create_environment(
        self,
        *,
        application_id: str,
        name: str,
        kind: str | None = None,
        base_url: str | None = None,
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
            app, name, kind=kind, base_url=base_url, options=options, hosts=list(hosts)
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


def test_duplicate_application_name_is_409(repo: FakeCatalogRepository) -> None:
    client = make_client(repo, ADMIN)
    payload = {"project_id": "demo", "name": "Checkout"}
    assert client.post("/catalog/applications", json=payload).status_code == 201
    assert client.post("/catalog/applications", json=payload).status_code == 409


def test_update_application(repo: FakeCatalogRepository) -> None:
    client = make_client(repo, ADMIN)
    app_id = client.post(
        "/catalog/applications", json={"project_id": "demo", "name": "Checkout"}
    ).json()["id"]
    patched = client.patch(f"/catalog/applications/{app_id}", json={"description": "updated"})
    assert patched.status_code == 200
    assert patched.json()["description"] == "updated"
    assert patched.json()["name"] == "Checkout"  # untouched fields preserved


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
