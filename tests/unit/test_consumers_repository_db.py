"""Thin Postgres round-trip for ConsumersRepository (skipped without a database)."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apex.auth.identity import ScopeRef
from apex.auth.service import hash_api_key
from apex.persistence.models import ApiConsumer, ConsumerDeletionRecord, ConsumerKey
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
                assert len(fetched.keys) == 1
                assert fetched.keys[0].key_hash == consumer.key_hash
                assert await repo.get_by_name(name) is not None

                updated = await repo.update(consumer.id, role="operator", enabled=False, scopes=[])
                assert updated is not None
                assert (updated.role, updated.enabled, updated.scopes) == ("operator", False, [])

                new_hash = hash_api_key(uuid4().hex)
                rotated = await repo.replace_key_hash(consumer.id, new_hash)
                assert rotated is not None and rotated.key_hash == new_hash
                assert len(rotated.keys) == 2
                assert rotated.keys[-1].rotated_from_id == fetched.keys[0].id
            finally:
                assert await repo.delete(consumer.id, deleted_by="admin-it") is True
            assert await repo.get(consumer.id) is None
            deleted = await session.get(ApiConsumer, consumer.id)
            assert deleted is not None
            assert deleted.deleted_at is not None
            tombstones = list(
                await session.scalars(
                    select(ConsumerDeletionRecord).where(
                        ConsumerDeletionRecord.consumer_id == consumer.id
                    )
                )
            )
            assert len(tombstones) == 1
            assert tombstones[0].deleted_by == "admin-it"
            keys = list(
                await session.scalars(
                    select(ConsumerKey).where(ConsumerKey.consumer_id == consumer.id)
                )
            )
            assert keys and all(key.revoked_at is not None for key in keys)
    finally:
        await engine.dispose()
