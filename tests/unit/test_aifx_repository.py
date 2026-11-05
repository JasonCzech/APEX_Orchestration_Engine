"""AifxRepository over real SQL (aiosqlite) plus migration 0049 shape checks."""

import importlib
import inspect
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apex.persistence.models import (
    AifxApplicationSource,
    AifxOrganization,
    Application,
    Base,
    Connection,
)
from apex.persistence.repositories.aifx import (
    AifxConflictError,
    AifxInUseError,
    AifxRepository,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
migration_0049 = importlib.import_module("apex.persistence.migrations.versions.0049_aifx_context")

_TABLES = (
    "connections",
    "host_mappings",
    "applications",
    "project_applications",
    "aifx_organizations",
    "aifx_application_sources",
)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", execution_options={"schema_translate_map": {"apex": None}}
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[Base.metadata.tables[f"apex.{name}"] for name in _TABLES]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Connection(id="conn-1", kind="source_control", provider="ghe", name="GHE"),
                Connection(id="conn-2", kind="source_control", provider="ghe", name="GHE 2"),
                Application(id="app-a", name="Alpha", project_ids=["p1"]),
                Application(id="app-b", name="Bravo", project_ids=["p2"]),
                Application(id="app-c", name="Charlie", project_ids=[]),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


def _org_fields(name: str = "Acme", **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "name": name,
        "connection_id": "conn-1",
        "ghe_owner": "acme",
        "global_rules_repo": "global-rules",
        "team_documents_repo_pattern": "{apm}-team-documents",
        "acb_repo_pattern": "{apm}-acb",
    }
    fields.update(overrides)
    return fields


async def _org(repo: AifxRepository, name: str = "Acme") -> AifxOrganization:
    return await repo.create_organization(_org_fields(name))


# --- organizations ---------------------------------------------------------------


async def test_organization_crud_roundtrip_and_server_defaults(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    created = await repo.create_organization(_org_fields("zeta", id_ignored="x"))
    assert len(created.id) == 32
    assert created.default_ref == "main"
    assert created.enabled is True
    assert created.created_at is not None and created.updated_at is not None
    await repo.create_organization(_org_fields("Alpha"))
    await repo.create_organization(_org_fields("beta"))

    # Ordered case-insensitively by name.
    assert [o.name for o in await repo.list_organizations()] == ["Alpha", "beta", "zeta"]
    assert await repo.get_organization(created.id) is created
    assert await repo.get_organization("missing") is None

    updated = await repo.update_organization(
        created,
        {"default_ref": "release", "enabled": False, "connection_id": "conn-2", "id": "hijack"},
    )
    assert updated.id == created.id  # non-whitelisted field ignored
    assert (updated.default_ref, updated.enabled, updated.connection_id) == (
        "release",
        False,
        "conn-2",
    )

    await repo.delete_organization(updated)
    assert await repo.get_organization(created.id) is None
    assert [o.name for o in await repo.list_organizations()] == ["Alpha", "beta"]


async def test_create_organization_drops_unknown_fields(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    row = await repo.create_organization(_org_fields(id="a" * 32, created_at="nope"))
    assert row.id != "a" * 32


async def test_duplicate_organization_name_is_a_safe_conflict(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    await _org(repo, "Acme")
    with pytest.raises(AifxConflictError) as caught:
        await _org(repo, "Acme")
    message = str(caught.value)
    assert message == "an AIFx organization with this name already exists"
    assert "UNIQUE" not in message and "constraint" not in message
    # The session was rolled back and remains usable.
    assert [o.name for o in await repo.list_organizations()] == ["Acme"]
    await _org(repo, "Other")


async def test_rename_into_taken_name_is_a_conflict(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    await _org(repo, "Acme")
    other = await _org(repo, "Other")
    with pytest.raises(AifxConflictError, match="name already exists"):
        await repo.update_organization(other, {"name": "Acme"})


async def test_organization_connection_fk_is_enforced(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    with pytest.raises(AifxConflictError) as caught:
        await repo.create_organization(_org_fields(connection_id="no-such-connection"))
    assert str(caught.value) == "AIFx onboarding changed while saving; reload and retry"
    assert await repo.list_organizations() == []


async def test_connection_referenced_by_organization_cannot_be_deleted(
    db: AsyncSession,
) -> None:
    repo = AifxRepository(db)
    await _org(repo)
    connection = await db.get(Connection, "conn-1")
    assert connection is not None
    await db.delete(connection)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    assert await db.get(Connection, "conn-1") is not None


async def test_delete_organization_in_use_raises_and_keeps_rows(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    org = await _org(repo)
    await repo.put_source("app-a", {"organization_id": org.id, "apm_id": "APM1"})
    with pytest.raises(AifxInUseError, match="remove its application mappings first"):
        await repo.delete_organization(org)
    assert await repo.get_organization(org.id) is not None

    source = await repo.get_source("app-a")
    assert source is not None
    await repo.delete_source(source)
    assert await repo.get_source("app-a") is None
    await repo.delete_organization(org)
    assert await repo.get_organization(org.id) is None


# --- application mappings --------------------------------------------------------


async def test_put_source_inserts_then_updates_in_place(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    org = await _org(repo)
    other = await _org(repo, "Other")

    inserted = await repo.put_source(
        "app-a",
        {
            "organization_id": org.id,
            "apm_id": "APM1",
            "team_documents_repo": "APM1-team-documents",
            "acb_repo": "APM1-acb",
            "application_id": "app-b",  # not writable
        },
    )
    assert inserted.application_id == "app-a"
    assert inserted.ref is None and inserted.enabled is True
    assert inserted.created_at is not None

    updated = await repo.put_source(
        "app-a", {"organization_id": other.id, "ref": "v2", "enabled": False, "acb_repo": None}
    )
    assert updated is inserted
    assert (updated.organization_id, updated.apm_id, updated.ref, updated.enabled) == (
        other.id,
        "APM1",
        "v2",
        False,
    )
    assert updated.acb_repo is None
    assert updated.team_documents_repo == "APM1-team-documents"
    rows = (await db.scalars(select(AifxApplicationSource))).all()
    assert [r.application_id for r in rows] == ["app-a"]


async def test_put_source_duplicate_apm_is_a_safe_conflict(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    org = await _org(repo)
    await repo.put_source("app-a", {"organization_id": org.id, "apm_id": "APM1"})
    with pytest.raises(AifxConflictError) as caught:
        await repo.put_source("app-b", {"organization_id": org.id, "apm_id": "APM1"})
    assert str(caught.value) == "this APM ID is already mapped to another application"
    assert await repo.get_source("app-b") is None


async def test_put_source_unknown_organization_or_application_is_a_conflict(
    db: AsyncSession,
) -> None:
    repo = AifxRepository(db)
    # A rollback expires loaded rows; capture the id while it is still loaded.
    org_id = (await _org(repo)).id
    with pytest.raises(AifxConflictError, match="reload and retry"):
        await repo.put_source("app-a", {"organization_id": "gone", "apm_id": "APM1"})
    with pytest.raises(AifxConflictError, match="reload and retry"):
        await repo.put_source("no-such-app", {"organization_id": org_id, "apm_id": "APM2"})
    assert await repo.list_sources() == []


async def test_apm_taken_is_case_insensitive_and_excludes_same_application(
    db: AsyncSession,
) -> None:
    repo = AifxRepository(db)
    org = await _org(repo)
    await repo.put_source("app-a", {"organization_id": org.id, "apm_id": "Apm-One"})

    assert await repo.apm_taken("apm-one", except_application_id="app-b") is True
    assert await repo.apm_taken("APM-ONE", except_application_id="app-b") is True
    assert await repo.apm_taken("Apm-One", except_application_id="app-a") is False
    assert await repo.apm_taken("apm-one", except_application_id="app-a") is False
    assert await repo.apm_taken("apm-two", except_application_id="app-b") is False


async def test_list_sources_orders_pages_and_filters(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    org = await _org(repo)
    await repo.put_source("app-a", {"organization_id": org.id, "apm_id": "C300"})
    await repo.put_source("app-b", {"organization_id": org.id, "apm_id": "A100"})
    await repo.put_source("app-c", {"organization_id": org.id, "apm_id": "B200"})

    def apms(rows: Any) -> list[str]:
        return [r.apm_id for r in rows]

    assert apms(await repo.list_sources()) == ["A100", "B200", "C300"]
    assert apms(await repo.list_sources(limit=2)) == ["A100", "B200"]
    assert apms(await repo.list_sources(limit=2, offset=2)) == ["C300"]
    assert apms(await repo.list_sources(limit=2, offset=5)) == []
    assert apms(await repo.list_sources(["app-a", "app-c"])) == ["B200", "C300"]
    assert apms(await repo.list_sources(["app-a", "app-c"], limit=1, offset=1)) == ["C300"]
    assert apms(await repo.list_sources(["unknown"])) == []
    assert await repo.list_sources([]) == []
    assert await repo.list_sources([], limit=10) == []


async def test_applications_by_id(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    assert await repo.applications_by_id([]) == {}
    found = await repo.applications_by_id(["app-a", "app-c", "missing"])
    assert set(found) == {"app-a", "app-c"}
    assert found["app-a"].name == "Alpha"
    assert found["app-a"].project_ids == ["p1"]


async def test_deleting_application_cascades_its_mapping(db: AsyncSession) -> None:
    repo = AifxRepository(db)
    org = await _org(repo)
    await repo.put_source("app-a", {"organization_id": org.id, "apm_id": "APM1"})
    db.expunge_all()
    await db.execute(delete(Application).where(Application.id == "app-a"))
    await db.commit()
    assert await repo.get_source("app-a") is None
    org_row = await repo.get_organization(org.id)
    assert org_row is not None
    await repo.delete_organization(org_row)


# --- migration 0049 --------------------------------------------------------------


def test_0049_follows_0048_and_creates_both_tables() -> None:
    upgrade_source = inspect.getsource(migration_0049.upgrade)
    downgrade_source = inspect.getsource(migration_0049.downgrade)

    assert migration_0049.revision == "0049"
    assert migration_0049.down_revision == "0048"
    for name in (
        '"aifx_organizations"',
        '"aifx_application_sources"',
        '"uq_aifx_organizations_name"',
        '"uq_aifx_application_sources_apm_id"',
        '"ix_aifx_application_sources_organization_id"',
        '"apex.connections.id"',
        '"apex.applications.id"',
        '"apex.aifx_organizations.id"',
    ):
        assert name in upgrade_source
    # Children dropped before their parent.
    assert downgrade_source.index('"aifx_application_sources"') < downgrade_source.index(
        '"aifx_organizations"'
    )


def test_0049_constraint_names_match_the_orm_naming_convention() -> None:
    upgrade_source = inspect.getsource(migration_0049.upgrade)
    for table_name in ("apex.aifx_organizations", "apex.aifx_application_sources"):
        table = Base.metadata.tables[table_name]
        for constraint in table.constraints:
            assert f'"{constraint.name}"' in upgrade_source, constraint.name
        for index in table.indexes:
            assert f'"{index.name}"' in upgrade_source, index.name


def test_0049_offline_upgrade_and_downgrade_sql() -> None:
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0048:0049", "--sql"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "CREATE TABLE apex.aifx_organizations" in upgrade
    assert "CREATE TABLE apex.aifx_application_sources" in upgrade
    assert "CONSTRAINT uq_aifx_organizations_name UNIQUE (name)" in upgrade
    assert "CONSTRAINT uq_aifx_application_sources_apm_id UNIQUE (apm_id)" in upgrade
    assert "REFERENCES apex.connections (id) ON DELETE RESTRICT" in upgrade
    assert "REFERENCES apex.applications (id) ON DELETE CASCADE" in upgrade
    assert "REFERENCES apex.aifx_organizations (id) ON DELETE RESTRICT" in upgrade
    assert "CREATE INDEX ix_aifx_application_sources_organization_id" in upgrade
    assert upgrade.index("CREATE TABLE apex.aifx_organizations") < upgrade.index(
        "CREATE TABLE apex.aifx_application_sources"
    )

    downgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0049:0048", "--sql"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "DROP TABLE apex.aifx_application_sources" in downgrade
    assert "DROP TABLE apex.aifx_organizations" in downgrade
    assert downgrade.index("DROP TABLE apex.aifx_application_sources") < downgrade.index(
        "DROP TABLE apex.aifx_organizations"
    )
