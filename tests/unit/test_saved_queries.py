"""SavedQueriesRepository: field-validation unit test + Postgres round-trip
(the round-trip skips without APEX_TEST_DATABASE_URI, like the other repo tests)."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apex.persistence.models import Base, SavedQuery
from apex.persistence.repositories.saved_queries import SavedQueriesRepository

needs_postgres = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


async def test_update_rejects_unknown_fields_without_touching_db() -> None:
    repo = SavedQueriesRepository(session=None)  # type: ignore[arg-type] — fails before any IO
    with pytest.raises(ValueError, match="not updatable"):
        await repo.update(SavedQuery(name="n", provider="jira", query="q"), {"id": "nope"})


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

                # duplicate (project_id, name) -> ValueError from the unique constraint
                with pytest.raises(ValueError, match="already exists"):
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
