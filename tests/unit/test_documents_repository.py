"""Thin Postgres integration test for DocumentsRepository (skipped without a DB).

Run with: APEX_TEST_DATABASE_URI=postgresql+asyncpg://... uv run pytest -q
Requires migration 0003 applied (documents table in the `apex` schema).
"""

import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


async def test_documents_repository_crud_roundtrip() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from apex.auth.identity import ScopeRef
    from apex.persistence.models import Document
    from apex.persistence.repositories.documents import DocumentsRepository

    engine = create_async_engine(os.environ["APEX_TEST_DATABASE_URI"])
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    doc_id = uuid4().hex
    try:
        async with sessionmaker() as session:
            repo = DocumentsRepository(session)
            created = await repo.add(
                Document(
                    id=doc_id,
                    name="it.txt",
                    media_type="text/plain",
                    size_bytes=2,
                    artifact_key=f"documents/{doc_id}/it.txt",
                    project_id="it-project",
                    summary="integration probe",
                )
            )
            assert created.created_at is not None

            fetched = await repo.get(doc_id)
            assert fetched is not None and fetched.name == "it.txt"

            by_key = await repo.get_by_artifact_key(f"documents/{doc_id}/it.txt")
            assert by_key is not None and by_key.id == doc_id

            listed = await repo.list(project="it-project", q="probe")
            assert doc_id in [d.id for d in listed]

            scoped = await repo.list(allowed_scopes=[ScopeRef(project_id="other-project")])
            assert doc_id not in [d.id for d in scoped]

            await repo.delete(fetched)
            assert await repo.get(doc_id) is None
    finally:
        async with sessionmaker() as session:
            leftover = await DocumentsRepository(session).get(doc_id)
            if leftover is not None:
                await DocumentsRepository(session).delete(leftover)
        await engine.dispose()
