"""Preparation archival retains audit data and serializes against other cycle edits."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.cycle_executions import ExecutionSnapshot
from apex.persistence.db import get_session
from apex.persistence.models import (
    Base,
    CycleExecutionPreparation,
    CycleScriptValidation,
)
from apex.persistence.models import (
    TestCycle as CycleRow,
)
from apex.routers import cycle_executions, cycle_script_validation
from apex.routers.cycles import enabled


class AsyncFacade:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def scalar(self, statement: Any) -> Any:
        return self.session.scalar(statement)

    async def scalars(self, statement: Any) -> Any:
        return self.session.scalars(statement)

    async def execute(self, statement: Any) -> Any:
        return self.session.execute(statement)

    async def commit(self) -> None:
        self.session.commit()

    async def rollback(self) -> None:
        self.session.rollback()


def snapshot() -> dict[str, Any]:
    return ExecutionSnapshot(
        plan_version_id="plan",
        plan_title="Plan",
        plan_checksum="plan-checksum",
        script_version_id="script",
        script_title="Script",
        script_checksum="script-checksum",
        script_source_checksum="source-checksum",
        script_format="apex_native_json",
        readiness_version_id=None,
        readiness_checksum=None,
        engine="apex-load",
        mode="live",
        environment_id="environment",
        environment_version=1,
        connection_id="engine",
        connection_version="2026-09-07T12:00:00+00:00",
        launch_authority="pipeline",
        workload={},
    ).model_dump(mode="json")


@pytest.fixture
def archive_app() -> Iterator[tuple[FastAPI, Engine]]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        for name in (
            "applications",
            "environments",
            "environment_snapshots",
            "engine_runs",
            "test_cycles",
            "cycle_execution_preparations",
            "cycle_execution_dispatches",
            "cycle_run_links",
            "cycle_script_validations",
            "cycle_script_artifacts",
            "connections",
        ):
            Base.metadata.tables[f"apex.{name}"].create(connection)
    with Session(engine) as session:
        session.add(
            CycleRow(
                id="cycle",
                name="Cycle",
                project_id="project",
                app_id="app",
                environment_id="env",
                mode="live",
                owner="author",
                revision=1,
                independent_review=True,
                versions=[],
            )
        )
        for identifier in ("prep", "other-prep"):
            session.add(
                CycleExecutionPreparation(
                    id=identifier,
                    cycle_id="cycle",
                    idempotency_key=identifier,
                    request_fingerprint="fingerprint",
                    snapshot=snapshot(),
                    created_by="author",
                )
            )
        session.add(
            CycleScriptValidation(
                id="validation",
                cycle_id="cycle",
                preparation_id="prep",
                request_key="prior",
                status="accepted",
                issues=[],
                warnings=[],
                created_by="validator",
                finished_at=datetime.now(UTC),
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(cycle_executions.router)
    app.include_router(cycle_script_validation.router)
    app.dependency_overrides[enabled] = lambda: None

    async def database():
        with Session(engine) as session:
            yield cast(AsyncSession, AsyncFacade(session))

    app.dependency_overrides[get_session] = database
    try:
        yield app, engine
    finally:
        engine.dispose()


def set_identity(
    app: FastAPI,
    role: Role = Role.OPERATOR,
    project: str = "project",
    app_id: str | None = "app",
    actor: str = "operator",
) -> None:
    app.dependency_overrides[get_current_identity] = lambda: ConsumerIdentity(
        consumer_id=actor,
        name=actor,
        consumer_type=ConsumerType.DASHBOARD,
        role=role,
        scopes=[ScopeRef(project_id=project, app_id=app_id)],
    )


def test_archive_is_scoped_idempotent_and_retains_binding_and_validation_history(archive_app):
    app, engine = archive_app
    set_identity(app)
    with TestClient(app) as client:
        response = client.post(
            "/cycles/cycle/executions/prep/archive", json={"expected_revision": 1}
        )
        assert response.status_code == 200
        assert response.json() == {"id": "prep", "revision": 2}
        set_identity(app, actor="second-operator")
        replay = client.post("/cycles/cycle/executions/prep/archive", json={"expected_revision": 1})
        assert replay.status_code == 200
        assert replay.json() == response.json()
        workspace = client.get("/cycles/cycle/executions")
        assert workspace.status_code == 200
        retained = next(row for row in workspace.json()["preparations"] if row["id"] == "prep")
        assert retained["archived_at"] and retained["archived_by"] == "operator"
        assert (
            next(check for check in retained["checks"] if check["key"] == "archived")["status"]
            == "blocked"
        )
        assert retained["launch_available"] is False
    with Session(engine) as session:
        preparation = session.get(CycleExecutionPreparation, "prep")
        assert preparation is not None and preparation.archived_at is not None
        assert preparation.archived_by == "operator"
        assert preparation.snapshot == snapshot()
        assert preparation.created_by == "author"
        assert preparation.idempotency_key == "prep"
        validation = session.get(CycleScriptValidation, "validation")
        assert validation is not None and validation.status == "accepted"
        assert validation.preparation_id == preparation.id
        cycle = session.get(CycleRow, "cycle")
        assert cycle is not None and cycle.revision == 2


@pytest.mark.parametrize(
    "role,project,app_id,status",
    [
        (Role.VIEWER, "project", "app", 403),
        (Role.OPERATOR, "elsewhere", "app", 404),
        (Role.OPERATOR, "project", "other-app", 404),
        (Role.OPERATOR, "project", None, 200),
    ],
)
def test_archive_enforces_role_and_exact_scope(archive_app, role, project, app_id, status):
    app, engine = archive_app
    set_identity(app, role, project, app_id)
    with TestClient(app) as client:
        response = client.post(
            "/cycles/cycle/executions/prep/archive", json={"expected_revision": 1}
        )
        assert response.status_code == status
    if status != 200:
        with Session(engine) as session:
            preparation = session.get(CycleExecutionPreparation, "prep")
            assert preparation is not None and preparation.archived_at is None


def test_archive_stale_revision_cannot_mutate_another_preparation(archive_app):
    app, engine = archive_app
    set_identity(app)
    with TestClient(app) as client:
        assert (
            client.post(
                "/cycles/cycle/executions/prep/archive", json={"expected_revision": 1}
            ).status_code
            == 200
        )
        conflict = client.post(
            "/cycles/cycle/executions/other-prep/archive", json={"expected_revision": 1}
        )
        assert conflict.status_code == 409
        assert (
            client.post(
                "/cycles/cycle/executions/missing/archive", json={"expected_revision": 2}
            ).status_code
            == 404
        )
    with Session(engine) as session:
        preparation = session.get(CycleExecutionPreparation, "other-prep")
        assert preparation is not None and preparation.archived_at is None


@pytest.mark.parametrize("revision", [0, -1, True, 1.5, "1"])
def test_archive_rejects_invalid_revision(archive_app, revision):
    app, _ = archive_app
    set_identity(app)
    with TestClient(app) as client:
        assert (
            client.post(
                "/cycles/cycle/executions/prep/archive", json={"expected_revision": revision}
            ).status_code
            == 422
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_status", [None, "pending"])
async def test_archived_preparation_cannot_start_or_retry_provider_validation(
    archive_app, existing_status
):
    app, engine = archive_app
    set_identity(app)
    with TestClient(app) as client:
        assert (
            client.post(
                "/cycles/cycle/executions/prep/archive", json={"expected_revision": 1}
            ).status_code
            == 200
        )
    identity = ConsumerIdentity(
        consumer_id="operator",
        name="Operator",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="project", app_id="app")],
    )
    with Session(engine) as session:
        if existing_status:
            session.add(
                CycleScriptValidation(
                    id="pending",
                    cycle_id="cycle",
                    preparation_id="prep",
                    request_key="new-request",
                    status=existing_status,
                    issues=[],
                    warnings=[],
                    created_by="operator",
                )
            )
            session.commit()
        facade = cast(AsyncSession, AsyncFacade(session))
        _, _, _, blockers = await cycle_script_validation.validation_context(
            "cycle", "prep", facade, identity
        )
        assert any("archived" in blocker for blocker in blockers)
        resolver = AsyncMock()
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as error:
            await cycle_script_validation.validate_script(
                "cycle",
                "prep",
                cycle_script_validation.ValidateCycleScript(
                    expected_revision=2, idempotency_key="new-request"
                ),
                facade,
                identity,
                resolver,
            )
        assert error.value.status_code == 422
        resolver.resolve_metadata.assert_not_awaited()
        assert len(session.scalars(select(CycleScriptValidation)).all()) == (
            2 if existing_status else 1
        )
