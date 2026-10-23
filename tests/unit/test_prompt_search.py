"""Catalog search matches what people type: labels with spaces, and prompt text."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apex.persistence.models import Base
from apex.persistence.repositories.prompts import PromptRepository, search_pattern
from apex.services.prompts import PromptCatalogService


def test_search_pattern_matches_separators_and_escapes_wildcards() -> None:
    assert search_pattern(" Env  Triage ") == "%Env_Triage%"
    assert search_pattern("story-analysis") == "%story_analysis%"
    assert search_pattern("100%") == "%100\\%%"
    assert search_pattern("a\\b") == "%a\\\\b%"


@pytest.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://", execution_options={"schema_translate_map": {"apex": None}}
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    Base.metadata.tables[f"apex.{name}"] for name in ("prompts", "prompt_versions")
                ],
            )
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


async def test_search_covers_spaced_labels_and_active_text(session: AsyncSession) -> None:
    catalog = PromptCatalogService(PromptRepository(session))
    triage, _ = await catalog.create_prompt(
        namespace="phase", key="env_triage/system", content="Assess readiness."
    )
    await catalog.create_prompt(
        namespace="phase", key="reporting/system", content="Summarize KPIs."
    )
    await catalog.create_prompt(
        namespace="phase", key="budget/system", content="Spend 100% wisely."
    )

    async def keys(q: str) -> list[str]:
        return [prompt.key for prompt in await PromptRepository(session).search(q=q)]

    assert await keys("Env Triage") == ["env_triage/system"]
    assert await keys("readiness") == ["env_triage/system"]
    assert await keys("100%") == ["budget/system"]
    assert await keys("%") == ["budget/system"]
    # Only the active version's text is searched.
    await catalog.save_version(triage.id, content="Check inventory.")
    assert await keys("readiness") == []
