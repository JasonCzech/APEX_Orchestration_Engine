"""Persistence invariants for prompt aggregate writes."""

from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import IntegrityError

from apex.persistence.models import Prompt, PromptVersion
from apex.persistence.repositories.prompts import DuplicatePromptKeyError, PromptRepository


async def test_bootstrap_mode_flushes_without_committing_caller_transaction() -> None:
    session = Mock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    prompt = Prompt(id="p1", namespace="phase", key="story/system")
    version = PromptVersion(id="v1", prompt_id="p1", version=1, content="hello")

    await PromptRepository(session, commit_on_write=False).add_prompt(prompt, version)

    session.commit.assert_not_awaited()
    assert session.flush.await_count == 3
    assert prompt.active_version_id == "v1"


async def test_version_allocation_locks_prompt_before_reading_max() -> None:
    session = Mock()
    session.scalar = AsyncMock(side_effect=["p1", 4])

    maximum = await PromptRepository(session).max_version("p1")

    assert maximum == 4
    lock_statement = session.scalar.await_args_list[0].args[0]
    assert "FOR UPDATE" in str(lock_statement)


async def test_duplicate_prompt_insert_rolls_back_and_raises_conflict() -> None:
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock(
        side_effect=IntegrityError(
            "INSERT",
            {},
            Exception('duplicate key violates unique constraint "uq_prompts_namespace"'),
        )
    )
    session.rollback = AsyncMock()
    prompt = Prompt(id="p1", namespace="phase", key="story/system")
    version = PromptVersion(id="v1", prompt_id="p1", version=1, content="hello")

    with pytest.raises(DuplicatePromptKeyError):
        await PromptRepository(session).add_prompt(prompt, version)

    session.rollback.assert_awaited_once()


async def test_unrelated_prompt_integrity_error_is_not_mislabeled() -> None:
    error = IntegrityError(
        "INSERT",
        {},
        Exception('duplicate key violates unique constraint "pk_prompts"'),
    )
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock(side_effect=error)
    session.rollback = AsyncMock()
    prompt = Prompt(id="p1", namespace="phase", key="story/system")
    version = PromptVersion(id="v1", prompt_id="p1", version=1, content="hello")

    with pytest.raises(IntegrityError) as raised:
        await PromptRepository(session).add_prompt(prompt, version)

    assert raised.value is error
    session.rollback.assert_awaited_once()


async def test_catalog_search_excludes_application_namespace_by_default() -> None:
    result = Mock()
    result.all.return_value = []
    session = Mock()
    session.scalars = AsyncMock(return_value=result)
    repository = PromptRepository(session)

    assert await repository.search() == []
    restricted_statement = session.scalars.await_args.args[0]
    assert "prompts.namespace !=" in str(restricted_statement)

    assert await repository.search(allow_application=True) == []
    unrestricted_statement = session.scalars.await_args.args[0]
    assert "prompts.namespace !=" not in str(unrestricted_statement)


async def test_application_active_lookup_requires_trusted_internal_flag() -> None:
    session = Mock()
    session.scalar = AsyncMock(return_value=None)
    repository = PromptRepository(session)

    assert await repository.get_active_version("application", "a1") is None
    session.scalar.assert_not_awaited()

    assert await repository.get_active_version("application", "a1", allow_application=True) is None
    session.scalar.assert_awaited_once()
