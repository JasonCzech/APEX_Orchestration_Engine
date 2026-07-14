"""PostgreSQL-gated prompt version allocation/lineage concurrency test."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apex.persistence.models import Base, Prompt, PromptVersion
from apex.persistence.repositories.prompts import PromptRepository
from apex.services.prompts import PromptCatalogService

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


class _BarrierRepository(PromptRepository):
    def __init__(self, session: AsyncSession, barrier: asyncio.Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier

    async def max_version(self, prompt_id: str) -> int:
        # Both services have already loaded the Prompt before either takes the lock.
        await self._barrier.wait()
        return await super().max_version(prompt_id)


async def test_concurrent_versions_are_unique_and_form_current_parent_chain() -> None:
    uri = os.environ["APEX_TEST_DATABASE_URI"]
    engine = create_async_engine(uri)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    marker = uuid4().hex

    async with engine.begin() as connection:
        await connection.execute(text("CREATE SCHEMA IF NOT EXISTS apex"))
        await connection.run_sync(Base.metadata.create_all)

    async with maker() as session:
        prompt, first = await PromptCatalogService(PromptRepository(session)).create_prompt(
            namespace=f"concurrency-{marker}",
            key="system",
            content="v1",
        )

    barrier = asyncio.Barrier(2)

    async def save(content: str) -> PromptVersion:
        async with maker() as session:
            _, version = await PromptCatalogService(
                _BarrierRepository(session, barrier)
            ).save_version(prompt.id, content=content)
            return version

    try:
        await asyncio.gather(save("v2-or-v3-a"), save("v2-or-v3-b"))
        async with maker() as session:
            versions = list(
                await session.scalars(
                    select(PromptVersion)
                    .where(PromptVersion.prompt_id == prompt.id)
                    .order_by(PromptVersion.version)
                )
            )

        assert [version.version for version in versions] == [1, 2, 3]
        assert versions[0].id == first.id
        assert versions[1].parent_version_id == versions[0].id
        assert versions[2].parent_version_id == versions[1].id
    finally:
        async with maker() as session:
            await session.execute(delete(Prompt).where(Prompt.id == prompt.id))
            await session.commit()
        await engine.dispose()
