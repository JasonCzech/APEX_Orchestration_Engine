"""Thin Postgres round-trip for DraftsRepository (skipped without a database)."""

import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apex.persistence.repositories.drafts import DraftsRepository

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


async def test_draft_crud_roundtrip() -> None:
    engine = create_async_engine(os.environ["APEX_TEST_DATABASE_URI"])
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            repo = DraftsRepository(session)
            project_id = f"proj-it-{uuid4().hex[:8]}"
            draft = await repo.create(
                title="wizard draft",
                project_id=project_id,
                payload={"step": 1},
                created_by="it-bot",
            )
            try:
                listed = await repo.list_all(project_id=project_id)
                assert [d.id for d in listed] == [draft.id]

                first_updated_at = draft.updated_at
                replaced = await repo.replace(
                    draft.id, title="wizard draft v2", payload={"step": 2}
                )
                assert replaced is not None
                assert replaced.title == "wizard draft v2"
                assert replaced.payload == {"step": 2}
                assert replaced.updated_at > first_updated_at
            finally:
                assert await repo.delete(draft.id) is True
            assert await repo.get(draft.id) is None
    finally:
        await engine.dispose()
