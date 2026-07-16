"""SavedQueriesRepository: field-validation unit test + Postgres round-trip
(the round-trip skips without APEX_TEST_DATABASE_URI, like the other repo tests)."""

import os
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apex.persistence.models import Base, SavedQuery
from apex.persistence.repositories.saved_queries import (
    SavedQueriesRepository,
    SavedQueryNameConflictError,
)

needs_postgres = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


async def test_update_rejects_unknown_fields_without_touching_db() -> None:
    repo = SavedQueriesRepository(session=None)  # type: ignore[arg-type] — fails before any IO
    with pytest.raises(ValueError, match="unsupported saved query fields"):
        await repo.update(SavedQuery(name="n", provider="jira", query="q"), {"id": "nope"})
    with pytest.raises(ValueError, match="unsupported saved query fields"):
        await repo.update(
            SavedQuery(name="n", provider="jira", query="q"),
            {"project_id": "other-project"},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "x" * 33),
        ("id", "sk-0123456789abcdefghijkl"),
        ("name", None),
        ("provider", 1),
        ("query", ""),
        ("project_id", "x" * 256),
        ("description", "bad\x00description"),
        ("created_by", "ghp_0123456789abcdefghijklmnopqrstuvwxyz"),
    ],
)
async def test_add_rejects_invalid_or_credential_bearing_complete_rows_before_io(
    field: str, value: Any
) -> None:
    session = Mock()
    row = SavedQuery(
        name="triage",
        provider="jira",
        query="project = PHX",
        project_id="project-1",
        description="open issues",
        created_by="operator",
    )
    setattr(row, field, value)

    with pytest.raises(ValueError, match="saved query"):
        await SavedQueriesRepository(session).add(row)

    session.add.assert_not_called()


async def test_add_assigns_and_validates_a_safe_id_before_io() -> None:
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    row = SavedQuery(name="triage", provider="jira", query="project = PHX")

    assert row.id is None
    await SavedQueriesRepository(session).add(row)

    assert type(row.id) is str
    assert len(row.id) == 32
    session.add.assert_called_once_with(row)


async def test_update_validates_the_complete_effective_row_before_mutation() -> None:
    repository = SavedQueriesRepository(cast(Any, object()))
    legacy = SavedQuery(
        id="saved-query-1",
        name="safe",
        provider="jira",
        query="ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        description="legacy",
    )
    with pytest.raises(ValueError, match="credential material"):
        await repository.update(legacy, {"description": "replacement"})
    assert legacy.description == "legacy"

    current = SavedQuery(
        id="saved-query-2",
        name="safe",
        provider="jira",
        query="project = PHX",
    )
    with pytest.raises(ValueError, match="name must be"):
        await repository.update(current, {"name": None})
    assert current.name == "safe"


async def test_update_rejects_hostile_or_non_string_keys_without_executing_hooks() -> None:
    class HostileDict(dict[Any, Any]):
        called = False

        def __iter__(self) -> Any:
            self.called = True
            raise AssertionError("custom dictionary iteration must not execute")

        def keys(self) -> Any:
            self.called = True
            raise AssertionError("custom dictionary keys must not execute")

    repository = SavedQueriesRepository(cast(Any, object()))
    row = SavedQuery(name="safe", provider="jira", query="project = PHX")
    hostile = HostileDict(name="forged")

    with pytest.raises(ValueError, match="unsupported saved query fields"):
        await repository.update(row, hostile)
    with pytest.raises(ValueError, match="unsupported saved query fields"):
        await repository.update(row, {1: "forged"})  # type: ignore[dict-item]

    assert hostile.called is False
    assert row.name == "safe"


def _integrity_error(constraint: str, *, detail: str = "CANARY-DB-DETAIL") -> IntegrityError:
    return IntegrityError(
        "INSERT",
        {},
        Exception(f'{detail}: duplicate key violates unique constraint "{constraint}"'),
    )


@pytest.mark.parametrize(
    "constraint",
    ["uq_saved_queries_project_id", "uq_saved_queries_global_name"],
)
async def test_duplicate_saved_query_conflict_is_fixed_and_drops_driver_context(
    constraint: str,
) -> None:
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock(side_effect=_integrity_error(constraint))
    session.rollback = AsyncMock()
    session.refresh = AsyncMock()

    with pytest.raises(SavedQueryNameConflictError) as raised:
        await SavedQueriesRepository(session).add(
            SavedQuery(name="CANARY-CALLER-NAME", provider="jira", query="q")
        )

    assert str(raised.value) == "saved query name already exists"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "CANARY" not in str(raised.value)
    session.rollback.assert_awaited_once()
    session.refresh.assert_not_awaited()


async def test_unrelated_saved_query_integrity_error_is_not_mislabeled() -> None:
    error = _integrity_error("ck_saved_queries_provider")
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock(side_effect=error)
    session.rollback = AsyncMock()

    with pytest.raises(IntegrityError) as raised:
        await SavedQueriesRepository(session).add(
            SavedQuery(name="unique", provider="jira", query="q")
        )

    assert raised.value is error
    session.rollback.assert_awaited_once()


def test_global_saved_query_names_are_unique_when_project_is_null() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.saved_queries"].create(connection)

    with Session(engine) as session:
        session.add(SavedQuery(name="global", project_id=None, provider="jira", query="q1"))
        session.commit()
        session.add(SavedQuery(name="global", project_id=None, provider="ado", query="q2"))
        with pytest.raises(IntegrityError):
            session.commit()


@needs_postgres
async def test_saved_query_crud_roundtrip() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(os.environ["APEX_TEST_DATABASE_URI"])
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            repo = SavedQueriesRepository(session)
            project_id = f"proj-it-{uuid4().hex[:8]}"
            scoped = await repo.add(
                SavedQuery(
                    name="open bugs",
                    project_id=project_id,
                    provider="jira",
                    query='project = PHX AND statusCategory = "To Do"',
                    description="triage view",
                    created_by="it-bot",
                )
            )
            global_row = await repo.add(
                SavedQuery(
                    name=f"global wiql {uuid4().hex[:6]}",
                    project_id=None,
                    provider="ado",
                    query="SELECT [System.Id] FROM WorkItems",
                )
            )
            try:
                assert scoped.id and scoped.created_at is not None

                # duplicate names use a dedicated conflict, not generic validation failure
                with pytest.raises(SavedQueryNameConflictError, match="already exists"):
                    await repo.add(
                        SavedQuery(
                            name="open bugs", project_id=project_id, provider="ado", query="q"
                        )
                    )

                # scoped listing sees the project row + the global row
                listed = await repo.list(allowed_project_ids=[project_id])
                listed_ids = {row.id for row in listed}
                assert {scoped.id, global_row.id} <= listed_ids
                assert all(row.project_id in (None, project_id) for row in listed)

                # provider + project filters narrow correctly
                only_scoped = await repo.list(project=project_id, provider="jira")
                assert [row.id for row in only_scoped] == [scoped.id]

                first_updated_at = scoped.updated_at
                updated = await repo.update(
                    scoped, {"query": "project = PHX", "description": "narrowed"}
                )
                assert updated.query == "project = PHX"
                assert updated.description == "narrowed"
                assert updated.updated_at >= first_updated_at

                assert (await repo.get(scoped.id)) is not None
            finally:
                await repo.delete(scoped)
                await repo.delete(global_row)
            assert await repo.get(scoped.id) is None
    finally:
        await engine.dispose()
