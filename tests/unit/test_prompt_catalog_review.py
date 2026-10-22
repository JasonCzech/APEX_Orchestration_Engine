"""Regression coverage for prompt catalog integration with seeding and deletion."""

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from apex.bootstrap.runner import BootstrapReport, _seed_default_prompts
from apex.persistence.models import Application, Environment, Prompt
from apex.persistence.repositories.catalog import CatalogInUseError, CatalogRepository
from apex.persistence.repositories.prompts import PromptRepository
from apex.services.prompts import PromptCatalogService
from tests.unit.test_prompt_selections import create
from tests.unit.test_prompt_selections import db as db


@pytest.mark.parametrize("seeder", ["bootstrap", "script"])
async def test_seed_preserves_defaults_beyond_first_catalog_page(db, monkeypatch, seeder):
    catalog = PromptCatalogService(PromptRepository(db))
    for namespace, metadata in (("system", {}), ("phase", {"phase": "reporting"})):
        for index in range(101):
            await create(db, namespace, f"alternative-{index:03}", **metadata)
        designated, _ = await create(db, namespace, "zzz-designated", **metadata)
        await catalog.set_default(designated.id)
    before = list((await db.scalars(select(Prompt.id).where(Prompt.is_default))).all())
    if seeder == "bootstrap":
        await _seed_default_prompts(db, BootstrapReport(), lambda _: None)
        await db.commit()
    else:
        from scripts import seed_prompts

        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        monkeypatch.setattr(seed_prompts, "get_sessionmaker", lambda: factory)
        assert await seed_prompts.main() == 0
    after = set((await db.scalars(select(Prompt.id).where(Prompt.is_default))).all())
    assert set(before) <= after


@pytest.mark.parametrize("kind", ["application", "environment"])
async def test_catalog_deletion_retains_associated_prompts(db, kind):
    await create(
        db,
        kind,
        "retained",
        **(
            {"application_id": "app-one"}
            if kind == "application"
            else {"environment_id": "env-one"}
        ),
    )
    await db.execute(text("PRAGMA foreign_keys=ON"))
    repo = CatalogRepository(db)
    resource: Any
    if kind == "application":
        resource = await repo.get_application("app-one")
        operation = repo.delete_application
    else:
        resource = await repo.get_environment_for_update("env-one")
        operation = repo.delete_environment
    with pytest.raises(CatalogInUseError) as error:
        await operation(resource)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert await db.get(Application, "app-one") is not None
    assert await db.get(Environment, "env-one") is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"content": " \n\t"},
        {"selection_enabled": False},
        {"selection_enabled": False, "phase": "reporting"},
    ],
)
def test_corrupt_manifest_entry_cannot_bypass_content_or_applicability(changes):
    from pydantic import ValidationError

    from apex.domain.prompt_selection import PromptManifestEntry

    with pytest.raises(ValidationError):
        PromptManifestEntry.model_validate(
            {
                "namespace": "system",
                "prompt_id": "system-prompt",
                "version_id": "system-version",
                "key": "system",
                "version": 1,
                "content": "Selected system instructions",
                **changes,
            }
        )
