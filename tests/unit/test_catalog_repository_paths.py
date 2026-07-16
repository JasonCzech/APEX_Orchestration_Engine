"""Database-backed catalog CRUD, visibility, and validation paths."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from apex.auth.identity import ScopeRef
from apex.persistence.models import (
    Application,
    Base,
    Environment,
    EnvironmentSnapshot,
)
from apex.persistence.repositories import catalog as catalog_module
from apex.persistence.repositories.catalog import CatalogRepository, DuplicateNameError


class _AsyncFacade:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instance: Any) -> None:
        self._session.add(instance)

    async def get(self, model: Any, key: Any) -> Any:
        return self._session.get(model, key)

    async def scalar(self, statement: Any) -> Any:
        return self._session.scalar(statement)

    async def scalars(self, statement: Any) -> Any:
        return self._session.scalars(statement)

    async def delete(self, instance: Any) -> None:
        self._session.delete(instance)

    async def commit(self) -> None:
        self._session.commit()

    async def rollback(self) -> None:
        self._session.rollback()

    async def refresh(self, instance: Any) -> None:
        self._session.refresh(instance)


async def test_catalog_crud_visibility_hosts_and_snapshots() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        for table in (
            "apex.applications",
            "apex.environments",
            "apex.environment_hosts",
            "apex.environment_snapshots",
        ):
            Base.metadata.tables[table].create(connection)
    try:
        with Session(engine, expire_on_commit=False) as session:
            repository = CatalogRepository(cast(AsyncSession, _AsyncFacade(session)))
            first = await repository.create_application(
                project_id="project-1", name="Checkout", description="old"
            )
            second = await repository.create_application(project_id="project-2", name="Search")
            assert await repository.get_application(first.id) is first
            assert [app.id for app in await repository.list_applications(project="project-1")] == [
                first.id
            ]
            assert [
                app.id for app in await repository.list_applications(visible_projects=["project-2"])
            ] == [second.id]
            assert await repository.list_applications(allowed_scopes=[]) == []
            assert [
                app.id
                for app in await repository.list_applications(
                    allowed_scopes=[ScopeRef(project_id="project-1", app_id=first.id)]
                )
            ] == [first.id]

            await repository.update_application(first, {"description": "new"})
            assert first.description == "new"
            await repository.set_application_archived(first, True)
            assert await repository.list_applications(project="project-1") == []
            assert [
                app.id
                for app in await repository.list_applications(
                    project="project-1", include_archived=True
                )
            ] == [first.id]
            await repository.set_application_archived(first, False)

            environment = await repository.create_environment(
                application_id=first.id,
                name="staging",
                kind="k8s",
                base_url="https://8.8.8.8/load",
                target_approved=True,
                target_version=1,
                options={},
                hosts=[
                    {"hostname": "app.internal", "role": "app"},
                    {"hostname": "db.internal", "role": None},
                ],
            )
            assert len(environment.hosts) == 2
            assert (await repository.get_environment(environment.id)).id == environment.id  # type: ignore[union-attr]
            assert (
                await repository.get_environment_for_update(environment.id)
            ).id == environment.id  # type: ignore[union-attr]
            assert [
                row.id for row in await repository.list_environments(application_id=first.id)
            ] == [environment.id]
            assert [
                row.id
                for row in await repository.list_environments(
                    visible_projects=["project-1"],
                    allowed_scopes=[ScopeRef(project_id="project-1")],
                )
            ] == [environment.id]
            assert await repository.list_environments(allowed_scopes=[]) == []

            updated = await repository.update_environment(
                environment,
                {"name": "production", "kind": "vm", "options": {}},
                hosts=[{"hostname": "prod.internal", "role": "web"}],
            )
            assert updated.name == "production"
            assert [(host.hostname, host.role) for host in updated.hosts] == [
                ("prod.internal", "web")
            ]

            old = datetime.now(UTC) - timedelta(minutes=1)
            session.add_all(
                [
                    EnvironmentSnapshot(environment_id=environment.id, scanned_at=old, data={}),
                    EnvironmentSnapshot(
                        environment_id=environment.id,
                        scanned_at=datetime.now(UTC),
                        data={"latest": True},
                    ),
                ]
            )
            session.commit()
            snapshot = await repository.latest_snapshot(environment.id)
            assert snapshot is not None and snapshot.data == {"latest": True}

            await repository.delete_environment(updated)
            assert await repository.get_environment(environment.id) is None
            await repository.delete_application(second)
            assert await repository.get_application(second.id) is None
    finally:
        engine.dispose()


async def test_catalog_translates_sqlite_duplicate_names() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.applications"].create(connection)
    try:
        with Session(engine, expire_on_commit=False) as session:
            repository = CatalogRepository(cast(AsyncSession, _AsyncFacade(session)))
            await repository.create_application(project_id="project-1", name="duplicate")
            with pytest.raises(DuplicateNameError, match="already exists"):
                await repository.create_application(project_id="project-1", name="duplicate")
    finally:
        engine.dispose()


@pytest.mark.parametrize("value", [None, 1, "", "x" * 6, "bad\x00value"])
def test_catalog_bounded_text_rejects_invalid_values(value: Any) -> None:
    with pytest.raises(ValueError, match="character string"):
        catalog_module._bounded_text(value, label="field", max_chars=5)


@pytest.mark.asyncio
async def test_catalog_repository_rejects_credentials_in_scalar_labels() -> None:
    repository = CatalogRepository(cast(AsyncSession, object()))
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    application = Application(project_id="project-1", name="safe")
    environment = Environment(
        application_id="a" * 32,
        name="safe",
        options={},
        target_approved=False,
        target_version=0,
    )

    with pytest.raises(ValueError, match="credential material"):
        await repository.create_application(project_id="project-1", name=credential)
    with pytest.raises(ValueError, match="credential material"):
        await repository.update_application(application, {"description": credential})
    with pytest.raises(ValueError, match="credential material"):
        await repository.create_environment(
            application_id="a" * 32,
            name="safe",
            hosts=[{"hostname": "api.example.test", "role": credential}],
        )
    with pytest.raises(ValueError, match="credential material"):
        await repository.update_environment(environment, {"name": credential})

    assert application.description is None
    assert environment.name == "safe"


@pytest.mark.asyncio
async def test_catalog_repository_never_executes_hostile_mapping_hooks() -> None:
    class HostileDict(dict[Any, Any]):
        called = False

        def __iter__(self) -> Any:
            self.called = True
            raise AssertionError("custom dictionary iteration must not execute")

        def keys(self) -> Any:
            self.called = True
            raise AssertionError("custom dictionary keys must not execute")

    repository = CatalogRepository(cast(AsyncSession, object()))
    application = Application(project_id="project-1", name="safe")
    environment = Environment(
        application_id="a" * 32,
        name="safe",
        options={},
        target_approved=False,
        target_version=0,
    )
    options = HostileDict(region="east")
    changes = HostileDict(name="forged")
    host = HostileDict(hostname="api.example.test")

    with pytest.raises(ValueError, match="options must be an object"):
        await repository.create_environment(
            application_id="a" * 32,
            name="safe",
            options=options,
        )
    with pytest.raises(ValueError, match="unsupported application fields"):
        await repository.update_application(application, changes)
    with pytest.raises(ValueError, match="unsupported environment fields"):
        await repository.update_environment(environment, changes)
    with pytest.raises(ValueError, match="host must contain only"):
        await repository.create_environment(
            application_id="a" * 32,
            name="safe",
            hosts=[host],
        )

    assert options.called is False
    assert changes.called is False
    assert host.called is False
    assert application.name == "safe"
    assert environment.name == "safe"


@pytest.mark.asyncio
async def test_catalog_repository_never_executes_hostile_scalar_hooks() -> None:
    class HostileInt(int):
        called = False

        def __ge__(self, other: object) -> bool:
            self.called = True
            raise AssertionError("custom integer comparison must not execute")

    class HostileTruth:
        called = False

        def __bool__(self) -> bool:
            self.called = True
            raise AssertionError("custom truthiness must not execute")

    hostile_version = HostileInt(1)
    with pytest.raises(ValueError, match="target_version must be an integer"):
        catalog_module._validate_environment_target_metadata(
            base_url="https://8.8.8.8/run",
            options={},
            target_approved=True,
            target_version=hostile_version,
        )

    hostile_archived = HostileTruth()
    application = Application(project_id="project-1", name="safe")
    repository = CatalogRepository(cast(AsyncSession, object()))
    with pytest.raises(ValueError, match="archived state must be a boolean"):
        await repository.set_application_archived(application, cast(Any, hostile_archived))

    assert hostile_version.called is False
    assert hostile_archived.called is False
    assert application.archived_at is None


def test_catalog_target_and_host_validation_covers_all_metadata_guards() -> None:
    assert (
        catalog_module._bounded_text(
            None, label="optional", max_chars=5, min_chars=0, allow_none=True
        )
        is None
    )
    assert catalog_module._bounded_text("", label="empty", max_chars=5, min_chars=0) == ""
    assert (
        catalog_module._validate_environment_target_metadata(
            base_url="https://8.8.8.8/run",
            options={},
            target_approved=True,
            target_version=1,
        )
        == {}
    )

    invalid_metadata = [
        ({"options": []}, "must be an object"),
        ({"options": {"password": "raw"}}, "managed connection secret_ref"),
        ({"options": {"_apex_repair_required": True}}, "credential-bearing"),
        ({"target_approved": "yes"}, "must be a boolean"),
        ({"target_version": True}, "must be an integer"),
        ({"target_version": -1}, "must be an integer"),
        ({"target_approved": True}, "requires a URL"),
        ({"base_url": "https://8.8.8.8", "target_approved": True}, "positive version"),
    ]
    defaults = {
        "base_url": None,
        "options": {},
        "target_approved": False,
        "target_version": 0,
    }
    for overrides, message in invalid_metadata:
        with pytest.raises(ValueError, match=message):
            catalog_module._validate_environment_target_metadata(**(defaults | overrides))

    with pytest.raises(ValueError, match="at most"):
        catalog_module._build_hosts([{"hostname": "h"}] * 257)
    with pytest.raises(ValueError, match="must contain only"):
        catalog_module._build_hosts(cast(Any, ["host"]))
    with pytest.raises(ValueError, match="must be a list"):
        catalog_module._build_hosts(cast(Any, ({"hostname": "host"},)))
    with pytest.raises(ValueError, match="must contain only"):
        catalog_module._build_hosts([{"hostname": "host", "unknown": True}])
    with pytest.raises(ValueError, match="hostname"):
        catalog_module._build_hosts([{}])
    with pytest.raises(ValueError, match="host role"):
        catalog_module._build_hosts([{"hostname": "host", "role": 1}])
    built = catalog_module._build_hosts([{"hostname": "host", "role": ""}])
    assert built[0].hostname == "host" and built[0].role == ""


async def test_catalog_update_rejects_unknown_fields_before_database_io() -> None:
    environment = Environment(
        id="environment-1",
        application_id="application-1",
        name="name",
        options={},
        target_approved=False,
        target_version=0,
    )
    with pytest.raises(ValueError, match="unsupported environment fields"):
        await CatalogRepository(cast(Any, object())).update_environment(
            environment, {"application_id": "different"}
        )


async def test_catalog_reload_detects_a_disappearing_environment() -> None:
    session = Mock()
    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="vanished after commit"):
        await CatalogRepository(session)._reload_environment("missing")


def test_catalog_scope_and_driver_constraint_helpers() -> None:
    assert catalog_module._application_scope_filter([]) is None
    assert catalog_module._environment_scope_filter([]) is None
    scopes = [
        ScopeRef(project_id="project-1"),
        ScopeRef(project_id="project-1", app_id="covered-by-project"),
        ScopeRef(project_id="project-2", app_id="app-2"),
    ]
    assert catalog_module._application_scope_filter(scopes) is not None
    assert catalog_module._environment_scope_filter(scopes) is not None

    class DriverConstraintError(Exception):
        def __init__(self, constraint_name: str) -> None:
            super().__init__("duplicate catalog row")
            self.diag = SimpleNamespace(constraint_name=constraint_name)

    original = DriverConstraintError("uq_applications_project_id")
    error = catalog_module.IntegrityError("insert", {}, original)
    assert catalog_module._is_duplicate_catalog_name(error)
