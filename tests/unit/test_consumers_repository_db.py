"""Thin Postgres round-trip for ConsumersRepository (skipped without a database)."""

import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apex.auth.identity import ScopeRef
from apex.auth.service import hash_api_key
from apex.persistence.repositories.consumers import ConsumersRepository

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


async def test_consumer_crud_roundtrip() -> None:
    engine = create_async_engine(os.environ["APEX_TEST_DATABASE_URI"])
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            repo = ConsumersRepository(session)
            name = f"it-consumer-{uuid4().hex[:8]}"
            consumer = await repo.create(
                name=name,
                consumer_type="headless",
                role="viewer",
                key_hash=hash_api_key(uuid4().hex),
                scopes=[ScopeRef(project_id="proj-it", app_id="app-1")],
            )
            try:
                fetched = await repo.get(consumer.id)
                assert fetched is not None
                assert fetched.name == name
                assert [(s.project_id, s.app_id) for s in fetched.scopes] == [("proj-it", "app-1")]
                assert await repo.get_by_name(name) is not None

                updated = await repo.update(consumer.id, role="operator", enabled=False, scopes=[])
                assert updated is not None
                assert (updated.role, updated.enabled, updated.scopes) == ("operator", False, [])

                new_hash = hash_api_key(uuid4().hex)
                rotated = await repo.replace_key_hash(consumer.id, new_hash)
                assert rotated is not None and rotated.key_hash == new_hash
            finally:
                assert await repo.delete(consumer.id) is True
            assert await repo.get(consumer.id) is None
    finally:
        await engine.dispose()
