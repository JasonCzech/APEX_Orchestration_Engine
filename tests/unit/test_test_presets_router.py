"""/test-presets: visibility, scope checks, versions and usage against SQLite."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import Application, Base, RunPresetLink
from apex.persistence.repositories.catalog import CatalogRepository
from apex.persistence.repositories.test_presets import TestPresetsRepository
from apex.routers.test_presets import get_catalog_repository, get_presets_repository, router


def _identity(
    consumer_id: str, role: Role, scopes: list[ScopeRef] | None = None
) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id=consumer_id,
        name=consumer_id,
        consumer_type=ConsumerType.DASHBOARD,
        role=role,
        scopes=scopes or [],
    )


ADMIN = _identity("admin", Role.ADMIN)
ALICE = _identity("alice", Role.OPERATOR, [ScopeRef(project_id="proj-a")])
BOB = _identity("bob", Role.OPERATOR, [ScopeRef(project_id="proj-a")])
APP_ONLY = _identity("carol", Role.OPERATOR, [ScopeRef(project_id="proj-a", app_id="app-a")])
VIEWER = _identity("vic", Role.VIEWER, [ScopeRef(project_id="proj-a")])
OUTSIDER = _identity("olga", Role.OPERATOR, [ScopeRef(project_id="proj-z")])


def _payload(*participants: tuple[str, str]) -> dict[str, Any]:
    sections: dict[str, Any] = {"execution": {"engine": "sim"}}
    if participants:
        rows = [
            {
                "id": f"p{i}",
                "project_id": project,
                "application_id": app,
                "environment_id": "e",
                "layer": "backend" if i == 0 else "middle",
            }
            for i, (project, app) in enumerate(participants)
        ]
        sections["systems"] = {
            "scope": {
                "test_scope": {
                    "schema_version": 1,
                    "participants": rows,
                    "primary_participant_id": "p0",
                    "traffic_entry_participant_id": None,
                }
            }
        }
    return {"schema_version": 1, "sections": sections}


@pytest.fixture
def client() -> Iterator[Any]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", execution_options={"schema_translate_map": {"apex": None}}
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    tables = (
        "applications",
        "project_applications",
        "test_presets",
        "test_preset_versions",
        "run_preset_links",
        "run_participants",
        "run_scope_bindings",
    )

    async def setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[Base.metadata.tables[f"apex.{name}"] for name in tables]
                )
            )
        async with maker() as session:
            session.add_all(
                [
                    Application(id="app-a", name="ACC", project_ids=["proj-a"]),
                    Application(id="app-b", name="Mule", project_ids=["proj-a", "proj-b"]),
                ]
            )
            await session.commit()

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    state: dict[str, Any] = {"identity": ALICE}

    async def presets_repo() -> AsyncIterator[TestPresetsRepository]:
        async with maker() as session:
            yield TestPresetsRepository(session)

    async def catalog_repo() -> AsyncIterator[CatalogRepository]:
        async with maker() as session:
            yield CatalogRepository(session)

    app.dependency_overrides[get_presets_repository] = presets_repo
    app.dependency_overrides[get_catalog_repository] = catalog_repo
    app.dependency_overrides[get_current_identity] = lambda: state["identity"]
    with TestClient(app) as test_client:
        test_client.portal.call(setup)  # type: ignore[union-attr]

        class Client:
            def as_(self, identity: ConsumerIdentity) -> TestClient:
                state["identity"] = identity
                return test_client

            def seed_usage(self, rows: list[RunPresetLink]) -> None:
                async def insert() -> None:
                    async with maker() as session:
                        session.add_all(rows)
                        await session.commit()

                test_client.portal.call(insert)  # type: ignore[union-attr]

        yield Client()
        test_client.portal.call(engine.dispose)  # type: ignore[union-attr]


def _create(client: TestClient, **overrides: Any) -> Any:
    body = {
        "name": "ACC release certification",
        "project_id": "proj-a",
        "application_id": "app-a",
        "visibility": "project",
        "payload": _payload(("proj-a", "app-a")),
        **overrides,
    }
    return client.post("/v1/test-presets", json=body)


def test_project_preset_is_shared_versioned_and_guards_lost_updates(client: Any) -> None:
    created = _create(client.as_(ALICE))
    assert created.status_code == 201
    preset = created.json()
    assert (preset["version"], preset["mine"], preset["editable"]) == (1, True, True)

    bob = client.as_(BOB)
    assert [row["name"] for row in bob.get("/v1/test-presets").json()] == [
        "ACC release certification"
    ]
    assert bob.get("/v1/test-presets", params={"application": "app-b"}).json() == []
    update = {
        "name": "ACC release certification",
        "application_id": "app-a",
        "visibility": "project",
        "payload": _payload(("proj-a", "app-a")),
        "expected_version": 1,
        "note": "Longer soak",
    }
    updated = bob.put(f"/v1/test-presets/{preset['id']}", json=update)
    assert updated.status_code == 200
    assert (updated.json()["version"], updated.json()["updated_by"]) == (2, "bob")
    # A stale editor is told to reload instead of overwriting.
    assert bob.put(f"/v1/test-presets/{preset['id']}", json=update).status_code == 409
    versions = bob.get(f"/v1/test-presets/{preset['id']}/versions").json()
    assert [(row["version"], row["note"]) for row in versions] == [
        (2, "Longer soak"),
        (1, "Created"),
    ]
    # Only the creator decides who sees it.
    assert (
        bob.put(
            f"/v1/test-presets/{preset['id']}",
            json={**update, "expected_version": 2, "visibility": "personal"},
        ).status_code
        == 403
    )


def test_personal_presets_are_private_to_their_creator(client: Any) -> None:
    preset = _create(client.as_(ALICE), visibility="personal").json()
    assert client.as_(BOB).get("/v1/test-presets").json() == []
    assert client.as_(BOB).get(f"/v1/test-presets/{preset['id']}").status_code == 404
    assert len(client.as_(ALICE).get("/v1/test-presets").json()) == 1


def test_sharing_with_a_project_needs_project_wide_scope(client: Any) -> None:
    app_only = client.as_(APP_ONLY)
    assert _create(app_only).status_code == 403
    personal = _create(app_only, visibility="personal")
    assert personal.status_code == 201
    # A preset for another application is invisible to an app-only consumer.
    _create(
        client.as_(ALICE),
        name="Mule soak",
        application_id="app-b",
        payload=_payload(("proj-a", "app-b")),
    )
    assert [row["name"] for row in client.as_(APP_ONLY).get("/v1/test-presets").json()] == [
        "ACC release certification"
    ]


def test_presets_cannot_name_systems_outside_the_callers_scope(client: Any) -> None:
    alice = client.as_(ALICE)
    response = _create(alice, payload=_payload(("proj-a", "app-a"), ("proj-z", "app-z")))
    assert response.status_code == 403
    # The application must belong to the preset's project.
    assert _create(alice, application_id="app-missing").status_code == 422
    assert _create(alice, project_id="proj-a", application_id="app-b").status_code == 201
    # Payloads are wizard sections only, and never hold credentials.
    assert _create(alice, payload={"schema_version": 1, "sections": {}}).status_code == 422
    assert (
        _create(alice, payload={"schema_version": 1, "sections": {"bogus": {}}}).status_code == 422
    )
    leaked = {
        "schema_version": 1,
        "sections": {"text": {"request": "ghp_0123456789abcdefghijklmnopqrstuvwxyz"}},
    }
    assert _create(alice, payload=leaked).status_code == 422


def test_viewers_read_and_outsiders_see_nothing(client: Any) -> None:
    preset = _create(client.as_(ALICE)).json()
    viewer = client.as_(VIEWER)
    assert viewer.get("/v1/test-presets").json()[0]["editable"] is False
    assert _create(viewer).status_code == 403
    outsider = client.as_(OUTSIDER)
    assert outsider.get("/v1/test-presets").json() == []
    assert outsider.get(f"/v1/test-presets/{preset['id']}").status_code == 404
    assert outsider.delete(f"/v1/test-presets/{preset['id']}").status_code == 404


def test_archive_hides_and_delete_removes(client: Any) -> None:
    alice = client.as_(ALICE)
    preset = _create(alice).json()
    assert alice.post(f"/v1/test-presets/{preset['id']}/archive").json()["archived_at"]
    assert alice.get("/v1/test-presets").json() == []
    assert len(alice.get("/v1/test-presets", params={"include_archived": True}).json()) == 1
    assert alice.post(f"/v1/test-presets/{preset['id']}/unarchive").json()["archived_at"] is None
    assert alice.delete(f"/v1/test-presets/{preset['id']}").status_code == 204
    assert alice.get(f"/v1/test-presets/{preset['id']}").status_code == 404


def test_usage_counts_only_tests_the_caller_may_see(client: Any) -> None:
    client.seed_usage(
        [
            RunPresetLink(
                thread_id="t1",
                preset_kind="team",
                preset_id="p1",
                project_id="proj-a",
                app_id="app-a",
            ),
            RunPresetLink(
                thread_id="t2",
                preset_kind="team",
                preset_id="p1",
                project_id="proj-z",
                app_id="app-z",
            ),
            RunPresetLink(
                thread_id="t3",
                preset_kind="organization",
                preset_id="asst",
                project_id="proj-a",
                app_id="app-a",
            ),
        ]
    )
    params = {"kind": "team", "ids": ["p1", "p2"]}
    scoped = client.as_(ALICE).get("/v1/test-presets/usage", params=params).json()
    assert [(row["preset_id"], row["tests"]) for row in scoped] == [("p1", 1)]
    unscoped = client.as_(ADMIN).get("/v1/test-presets/usage", params=params).json()
    assert [(row["preset_id"], row["tests"]) for row in unscoped] == [("p1", 2)]


def test_list_pages_over_visible_presets_only(client: Any) -> None:
    # Others' personal presets sort first and outnumber a page; they must not
    # crowd out the presets the caller can actually use.
    bob = client.as_(BOB)
    for index in range(4):
        assert _create(bob, name=f"A{index} private", visibility="personal").status_code == 201
    alice = client.as_(ALICE)
    for index in range(3):
        assert _create(alice, name=f"Z{index} shared").status_code == 201
    page = alice.get("/v1/test-presets", params={"limit": 2})
    assert [row["name"] for row in page.json()] == ["Z0 shared", "Z1 shared"]
    rest = alice.get("/v1/test-presets", params={"limit": 2, "offset": 2})
    assert [row["name"] for row in rest.json()] == ["Z2 shared"]


def test_update_keeps_visibility_and_application_unless_sent(client: Any) -> None:
    alice = client.as_(ALICE)
    created = _create(alice, visibility="personal").json()
    renamed = alice.put(
        f"/v1/test-presets/{created['id']}",
        json={"name": "Renamed", "payload": created["payload"]},
    )
    assert renamed.status_code == 200
    assert (renamed.json()["visibility"], renamed.json()["application_id"]) == ("personal", "app-a")
    assert client.as_(BOB).get(f"/v1/test-presets/{created['id']}").status_code == 404
    general = client.as_(ALICE).put(
        f"/v1/test-presets/{created['id']}",
        json={"name": "Renamed", "payload": created["payload"], "application_id": None},
    )
    assert general.status_code == 200, general.json()
    assert general.json()["application_id"] is None


def test_history_hides_versions_naming_systems_outside_the_callers_scope(client: Any) -> None:
    admin = client.as_(ADMIN)
    created = _create(admin, payload=_payload(("proj-a", "app-a"), ("proj-b", "app-b"))).json()
    updated = admin.put(
        f"/v1/test-presets/{created['id']}",
        json={"name": created["name"], "payload": _payload(("proj-a", "app-a"))},
    )
    assert updated.status_code == 200

    def versions(identity: ConsumerIdentity) -> list[int]:
        rows = client.as_(identity).get(f"/v1/test-presets/{created['id']}/versions").json()
        return [row["version"] for row in rows]

    assert versions(ALICE) == [2]
    assert versions(ADMIN) == [2, 1]


def test_payload_systems_are_checked_wherever_they_appear(client: Any) -> None:
    alice = client.as_(ALICE)
    legacy = {
        "schema_version": 1,
        "sections": {"systems": {"scope": {"project_id": "proj-z", "app_id": "app-z"}}},
    }
    assert _create(alice, payload=legacy).status_code == 403
    # Scope and prompt keys in the execution bundle are dropped, not stored.
    bundle = {
        "schema_version": 1,
        "sections": {
            "execution": {
                "engine": "sim",
                "golden_configurable": {
                    "test_scope": {"participants": []},
                    "project_id": "proj-z",
                    "prompt_overrides": {"phase/execution": {"content": "x"}},
                    "load_test": {"vusers": 5},
                },
            }
        },
    }
    stored = _create(alice, payload=bundle).json()
    assert stored["payload"]["sections"]["execution"]["golden_configurable"] == {
        "load_test": {"vusers": 5}
    }
