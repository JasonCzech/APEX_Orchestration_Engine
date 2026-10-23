"""Scoped name projections and bounded, durable list progress use the real database."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.cycles import (
    KINDS,
    CycleAction,
    CycleProgress,
    VersionCreate,
    add_version,
    cycle_progress,
    transition,
)
from apex.domain.deliverables import DELIVERABLE_FIELDS
from apex.persistence.db import get_session
from apex.persistence.models import (
    ApiConsumer,
    Application,
    Base,
    CycleExecutionDispatch,
    CycleExecutionPreparation,
    CycleScriptValidation,
    Environment,
    ProjectApplication,
    ProjectLabel,
)
from apex.persistence.models import TestCycle as CycleRow
from apex.routers import catalog, cycles


class AsyncFacade:
    def __init__(self, session: Session):
        self.session = session

    def add(self, value: Any):
        self.session.add(value)

    async def get(self, model: Any, key: Any):
        return self.session.get(model, key)

    async def scalar(self, statement: Any):
        return self.session.scalar(statement)

    async def scalars(self, statement: Any):
        return self.session.scalars(statement)

    async def execute(self, statement: Any):
        return self.session.execute(statement)

    async def delete(self, value: Any):
        self.session.delete(value)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def refresh(self, value: Any):
        self.session.refresh(value)


def identity(app: FastAPI, *, role=Role.ADMIN, scopes: list[ScopeRef] | None = None):
    app.dependency_overrides[get_current_identity] = lambda: ConsumerIdentity(
        consumer_id="viewer",
        name="Current person",
        consumer_type=ConsumerType.DASHBOARD,
        role=role,
        scopes=scopes or [],
    )


@pytest.fixture
def directory() -> Iterator[tuple[TestClient, Engine, list[str]]]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        for table in (
            "project_labels",
            "api_consumers",
            "applications",
            "project_applications",
            "environments",
            "test_cycles",
            "cycle_execution_preparations",
            "cycle_execution_dispatches",
            "cycle_script_validations",
        ):
            Base.metadata.tables[f"apex.{table}"].create(connection)
    with Session(engine) as session:
        session.add_all(
            [
                ProjectLabel(project_id="p1", name="Payments"),
                ProjectLabel(project_id="p2", name="Payments"),
                ApiConsumer(
                    id="author",
                    name="Original author",
                    key_hash="a" * 64,
                    consumer_type="dashboard",
                    role="operator",
                ),
                ApiConsumer(
                    id="outsider",
                    name="Private outsider",
                    key_hash="b" * 64,
                    consumer_type="dashboard",
                    role="admin",
                ),
                Application(id="a1", project_ids=["p1"], name="Checkout"),
                Environment(id="e1", application_id="a1", name="Test"),
            ]
        )
        for id, project, app in [("c1", "p1", "a1"), ("c2", "p2", "a2"), ("c3", "p1", "sibling")]:
            session.add(
                CycleRow(
                    id=id,
                    name=id,
                    project_id=project,
                    app_id=app,
                    environment_id="e1",
                    mode="simulation",
                    owner="author",
                    revision=1,
                    independent_review=False,
                    versions=[],
                    progress=cycle_progress([]).model_dump(),
                )
            )
        session.commit()
    app = FastAPI()
    app.include_router(catalog.router)
    app.include_router(cycles.router)
    app.dependency_overrides[cycles.enabled] = lambda: None
    identity(app)

    async def database():
        with Session(engine, expire_on_commit=False) as session:
            yield cast(AsyncSession, AsyncFacade(session))

    app.dependency_overrides[get_session] = database
    sql: list[str] = []
    event.listen(
        engine, "before_cursor_execute", lambda _c, _u, statement, *_args: sql.append(statement)
    )
    try:
        with TestClient(app) as client:
            yield client, engine, sql
    finally:
        engine.dispose()


def test_project_names_allow_duplicates_rename_remove_without_changing_resource_scope(directory):
    client, engine, _ = directory
    assert [row["project_id"] for row in client.get("/catalog/projects").json()] == ["p1", "p2"]
    assert (
        client.put(
            "/catalog/projects", json={"project_id": "p1", "name": "Payments renamed"}
        ).status_code
        == 200
    )
    assert (
        client.get("/catalog/projects", params={"project": "p1"}).json()[0]["name"]
        == "Payments renamed"
    )
    assert client.delete("/catalog/projects", params={"project": "p1"}).status_code == 204
    assert client.get("/catalog/projects", params={"project": "p1"}).json() == []
    with Session(engine) as session:
        assert session.scalars(
            select(ProjectApplication.project_id).where(ProjectApplication.application_id == "a1")
        ).all() == ["p1"]
        assert session.scalar(select(CycleRow.project_id).where(CycleRow.id == "c1")) == "p1"


def test_project_reads_filter_scope_and_app_admin_cannot_change_projectwide_name(directory):
    client, _, _ = directory
    identity(client.app, role=Role.ADMIN, scopes=[ScopeRef(project_id="p1", app_id="a1")])
    assert [row["project_id"] for row in client.get("/catalog/projects").json()] == ["p1"]
    assert client.get("/catalog/projects", params={"project": "p2"}).status_code == 403
    assert (
        client.put("/catalog/projects", json={"project_id": "p1", "name": "No"}).status_code == 403
    )
    assert client.delete("/catalog/projects", params={"project": "p1"}).status_code == 403
    identity(client.app, role=Role.ADMIN, scopes=[ScopeRef(project_id="p1")])
    assert (
        client.put("/catalog/projects", json={"project_id": "p1", "name": "Allowed"}).status_code
        == 200
    )
    identity(client.app, role=Role.OPERATOR, scopes=[ScopeRef(project_id="p1")])
    assert (
        client.put("/catalog/projects", json={"project_id": "p1", "name": "No"}).status_code == 403
    )
    identity(client.app, role=Role.VIEWER)
    assert client.get("/catalog/projects").json() == []


def test_cycle_actor_lookup_is_reference_bound_and_handles_renamed_deleted_consumers(directory):
    client, engine, _ = directory
    identity(client.app, role=Role.VIEWER, scopes=[ScopeRef(project_id="p1", app_id="a1")])
    assert client.get("/cycles/c1/actors").json() == {
        "names": {"author": "Original author"},
        "truncated": False,
    }
    assert client.get("/cycles/c2/actors").status_code == 404
    assert client.get("/cycles/c3/actors").status_code == 404
    with Session(engine) as session:
        session.execute(
            update(ApiConsumer).where(ApiConsumer.id == "author").values(name="Renamed author")
        )
        session.commit()
    assert client.get("/cycles/c1/actors").json()["names"] == {"author": "Renamed author"}
    assert client.get("/cycles").json()[0]["owner_name"] == "Renamed author"
    with Session(engine) as session:
        session.execute(
            update(ApiConsumer)
            .where(ApiConsumer.id == "author")
            .values(deleted_at=datetime.now(UTC))
        )
        session.commit()
    assert client.get("/cycles/c1/actors").json()["names"] == {}
    assert client.get("/cycles").json()[0]["owner_name"] is None


def test_list_reads_only_bounded_projection_and_mutation_updates_it_atomically(directory):
    client, engine, sql = directory
    request = VersionCreate(
        expected_revision=1, kind="scope", title="Draft", content={}, change_summary="Start"
    )
    response = client.post("/cycles/c1/versions", json=request.model_dump())
    assert response.status_code == 201
    assert response.json()["progress"]["status"] == "draft"
    sql.clear()
    page = client.get("/cycles", params={"limit": 1, "offset": 0}).json()
    assert len(page) == 1
    assert all("test_cycles.versions" not in statement for statement in sql)
    with Session(engine) as session:
        projection = session.scalar(select(CycleRow.progress).where(CycleRow.id == "c1"))
    assert projection == response.json()["progress"]
    stale = client.post("/cycles/c1/versions", json=request.model_dump())
    assert stale.status_code == 409
    assert client.get("/cycles/c1").json()["progress"] == projection


def test_progress_latest_archive_and_stale_precedence():
    version = add_version(
        [],
        VersionCreate(
            expected_revision=1, kind="scope", title="First", content={}, change_summary="Start"
        ),
        "author",
    )
    assert cycle_progress([]).status == "not_started"
    assert cycle_progress([version]).status == "draft"
    version["status"] = "in_review"
    assert cycle_progress([version]).status == "in_review"
    version["status"] = "approved"  # Invalid content cannot become current approval.
    assert cycle_progress([version]).status == "stale"
    newer = {
        **version,
        "id": "newer",
        "archived": True,
        "status": "draft",
        "replaces": version["id"],
    }
    progress = cycle_progress([version, newer])
    assert progress.status == "archived"
    assert progress.approved == 0
    assert progress.deliverables[0].version_id == "newer"


def test_progress_counts_only_current_complete_approvals_and_retains_stale_dependencies():
    versions = []
    for kind in KINDS:
        content = {
            field.key: (field.minimum or 1)
            if field.control == "number"
            else field.options[0]
            if field.options
            else "Recorded"
            for field in DELIVERABLE_FIELDS[kind]
            if field.required
        }
        if kind == "script":
            content["source"] = '{"actions": []}'
        version = add_version(
            versions,
            VersionCreate(
                expected_revision=1,
                kind=kind,
                title=kind,
                content=content,
                dependencies=[versions[0]["id"]] if versions else [],
                change_summary="Initial",
            ),
            "author",
        )
        versions.append(version)
        for action in ("submit", "approve"):
            versions = transition(
                versions,
                CycleAction(expected_revision=1, action=action, version_id=version["id"]),
                "reviewer",
                independent_review=False,
            )
    assert cycle_progress(versions).status == "approved"
    assert cycle_progress(versions).approved == 6
    replacement = add_version(
        versions,
        VersionCreate(
            expected_revision=1,
            kind="scope",
            title="Changed",
            content=versions[0]["content"],
            replaces=versions[0]["id"],
            change_summary="Revise",
        ),
        "author",
    )
    progress = cycle_progress([*versions, replacement])
    assert progress.status == "stale"
    assert progress.approved == 0
    assert [item.status for item in progress.deliverables] == [
        "draft",
        "stale",
        "stale",
        "stale",
        "stale",
        "stale",
    ]


def test_legacy_backfill_is_bounded_and_subsequent_lists_do_not_read_history(directory):
    client, engine, sql = directory
    with Session(engine) as session:
        session.execute(update(CycleRow).values(progress=None))
        session.commit()
    sql.clear()
    first = client.get("/cycles").json()
    assert all(row["progress"]["status"] == "not_started" for row in first)
    assert sum("test_cycles.versions" in statement for statement in sql) == 1
    sql.clear()
    assert client.get("/cycles").json() == first
    assert all("test_cycles.versions" not in statement for statement in sql)


@pytest.mark.asyncio
async def test_legacy_projection_never_attaches_new_revision_to_old_list_row(directory):
    client, engine, _ = directory
    page = client.get("/cycles").json()
    old = next(row for row in page if row["id"] == "c1")
    old["progress"] = None
    with Session(engine) as session:
        session.execute(
            update(CycleRow).where(CycleRow.id == "c1").values(revision=2, progress=None)
        )
        session.commit()
        await cycles.fill_missing_progress(
            cast(AsyncSession, AsyncFacade(session)),
            ConsumerIdentity(
                consumer_id="viewer",
                name="Viewer",
                consumer_type=ConsumerType.DASHBOARD,
                role=Role.ADMIN,
            ),
            [old],
        )
        assert old["revision"] == 1 and old["progress"] is None
        assert session.scalar(select(CycleRow.progress).where(CycleRow.id == "c1")) is None


@pytest.mark.parametrize(
    "history", [[{"kind": "scope"}], [{"kind": "future", "id": "x", "status": "draft"}]]
)
def test_malformed_legacy_history_has_explicit_unavailable_projection(directory, history):
    client, engine, _ = directory
    with Session(engine) as session:
        session.execute(
            update(CycleRow)
            .where(CycleRow.id == "c1")
            .values(progress={"future": True}, versions=history)
        )
        session.commit()
    row = next(row for row in client.get("/cycles").json() if row["id"] == "c1")
    assert row["progress"] is None


@pytest.mark.parametrize("field,value", [("approved", 6), ("total", 7), ("status", "approved")])
def test_stored_progress_rejects_contradictory_totals_or_status(field, value):
    projection = cycle_progress([]).model_dump()
    projection[field] = value
    with pytest.raises(ValueError):
        CycleProgress.model_validate(projection)


def test_stored_progress_rejects_duplicate_kinds_and_unbounded_identity():
    projection = cycle_progress([]).model_dump()
    projection["deliverables"][1] = projection["deliverables"][0]
    with pytest.raises(ValueError):
        CycleProgress.model_validate(projection)
    with pytest.raises(ValueError):
        cycle_progress([{"kind": "scope", "id": "x" * 33, "status": "draft"}])


def test_actor_names_include_preparation_archive_validation_and_dispatch_references_only(directory):
    client, engine, _ = directory
    with Session(engine) as session:
        for actor in ("preparer", "archiver", "validator", "dispatcher", "canceler"):
            session.add(
                ApiConsumer(
                    id=actor,
                    name=actor.title(),
                    key_hash=actor,
                    consumer_type="dashboard",
                    role="operator",
                )
            )
        session.add(
            CycleExecutionPreparation(
                id="prep",
                cycle_id="c1",
                idempotency_key="prep",
                request_fingerprint="fp",
                snapshot={},
                created_by="preparer",
                archived_by="archiver",
            )
        )
        session.add(
            CycleScriptValidation(
                cycle_id="c1", preparation_id="prep", request_key="validate", created_by="validator"
            )
        )
        session.add(
            CycleExecutionDispatch(
                cycle_id="c1",
                preparation_id="prep",
                request_key="dispatch",
                pipeline_config={},
                created_by="dispatcher",
                canceled_by="canceler",
            )
        )
        session.commit()
    identity(client.app, role=Role.VIEWER, scopes=[ScopeRef(project_id="p1", app_id="a1")])
    names = client.get("/cycles/c1/actors").json()["names"]
    assert names == {
        "author": "Original author",
        "preparer": "Preparer",
        "archiver": "Archiver",
        "validator": "Validator",
        "dispatcher": "Dispatcher",
        "canceler": "Canceler",
    }
    assert "outsider" not in names
