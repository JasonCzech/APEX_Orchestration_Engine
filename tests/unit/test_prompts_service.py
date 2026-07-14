"""PromptCatalogService invariants over an in-memory fake store (no Postgres).

FakePromptRepository is the canonical fake for the prompt aggregate; the router
tests reuse it via import.
"""

import pytest

from apex.persistence.models import Prompt, PromptVersion
from apex.persistence.repositories.prompts import DuplicatePromptKeyError
from apex.services.prompts import (
    DuplicatePromptError,
    PromptCatalogService,
    PromptNotFoundError,
    PromptVersionMismatchError,
    PromptVersionNotFoundError,
)


class FakePromptRepository:
    """In-memory PromptStore: dumb storage, mirroring PromptRepository's contract
    (add_prompt/add_version persist rows and move the active pointer)."""

    def __init__(self) -> None:
        self.prompts: dict[str, Prompt] = {}
        self.versions: dict[str, PromptVersion] = {}

    async def get(self, prompt_id: str) -> Prompt | None:
        return self.prompts.get(prompt_id)

    async def get_by_key(self, namespace: str, key: str) -> Prompt | None:
        for prompt in self.prompts.values():
            if prompt.namespace == namespace and prompt.key == key:
                return prompt
        return None

    async def search(
        self,
        *,
        namespace: str | None = None,
        include_archived: bool = False,
        q: str | None = None,
        allow_application: bool = False,
    ) -> list[Prompt]:
        rows = list(self.prompts.values())
        if namespace is not None:
            rows = [p for p in rows if p.namespace == namespace]
        if not allow_application:
            rows = [p for p in rows if p.namespace != "application"]
        if not include_archived:
            rows = [p for p in rows if p.archived_at is None]
        if q:
            needle = q.lower()
            rows = [
                p
                for p in rows
                if needle in p.key.lower()
                or needle in p.namespace.lower()
                or needle in (p.description or "").lower()
            ]
        return sorted(rows, key=lambda p: (p.namespace, p.key))

    async def get_version(self, version_id: str) -> PromptVersion | None:
        return self.versions.get(version_id)

    async def get_versions_by_ids(self, version_ids: list[str]) -> list[PromptVersion]:
        return [self.versions[vid] for vid in version_ids if vid in self.versions]

    async def list_versions(self, prompt_id: str) -> list[PromptVersion]:
        rows = [v for v in self.versions.values() if v.prompt_id == prompt_id]
        return sorted(rows, key=lambda v: v.version, reverse=True)

    async def max_version(self, prompt_id: str) -> int:
        return max(
            (v.version for v in self.versions.values() if v.prompt_id == prompt_id), default=0
        )

    async def get_active_version(
        self,
        namespace: str,
        key: str,
        *,
        allow_application: bool = False,
    ) -> PromptVersion | None:
        if namespace == "application" and not allow_application:
            return None
        prompt = await self.get_by_key(namespace, key)
        if prompt is None or prompt.archived_at is not None or not prompt.active_version_id:
            return None
        return self.versions.get(prompt.active_version_id)

    async def add_prompt(self, prompt: Prompt, first_version: PromptVersion) -> None:
        self.prompts[prompt.id] = prompt
        self.versions[first_version.id] = first_version
        prompt.active_version_id = first_version.id

    async def add_version(self, prompt: Prompt, version: PromptVersion) -> None:
        self.versions[version.id] = version
        prompt.active_version_id = version.id

    async def save(self, prompt: Prompt) -> None:
        self.prompts[prompt.id] = prompt


class RacingPromptRepository(FakePromptRepository):
    async def add_prompt(self, prompt: Prompt, first_version: PromptVersion) -> None:
        raise DuplicatePromptKeyError("concurrent duplicate")


@pytest.fixture
def repo() -> FakePromptRepository:
    return FakePromptRepository()


@pytest.fixture
def catalog(repo: FakePromptRepository) -> PromptCatalogService:
    return PromptCatalogService(repo)


async def test_create_prompt_makes_v1_and_points_active(catalog: PromptCatalogService) -> None:
    prompt, version = await catalog.create_prompt(
        namespace="phase", key="story_analysis/system", content="v1 content", created_by="alice"
    )
    assert version.version == 1
    assert version.parent_version_id is None
    assert version.created_by == "alice"
    assert prompt.active_version_id == version.id
    got, active = await catalog.get_prompt(prompt.id)
    assert got.id == prompt.id
    assert active is not None and active.content == "v1 content"


async def test_create_duplicate_key_raises(catalog: PromptCatalogService) -> None:
    await catalog.create_prompt(namespace="phase", key="k", content="a")
    with pytest.raises(DuplicatePromptError):
        await catalog.create_prompt(namespace="phase", key="k", content="b")


async def test_create_concurrent_duplicate_is_translated_to_domain_error() -> None:
    catalog = PromptCatalogService(RacingPromptRepository())

    with pytest.raises(DuplicatePromptError, match="prompt phase/k already exists"):
        await catalog.create_prompt(namespace="phase", key="k", content="content")


async def test_save_version_is_monotonic_and_moves_pointer(
    catalog: PromptCatalogService,
) -> None:
    prompt, v1 = await catalog.create_prompt(namespace="ns", key="k", content="one")
    _, v2 = await catalog.save_version(prompt.id, content="two", note="second")
    _, v3 = await catalog.save_version(prompt.id, content="three")
    assert [v1.version, v2.version, v3.version] == [1, 2, 3]
    assert v2.parent_version_id == v1.id
    assert v3.parent_version_id == v2.id
    assert prompt.active_version_id == v3.id
    versions = await catalog.list_versions(prompt.id)
    assert [v.version for v in versions] == [3, 2, 1]  # newest first, all immutable rows kept


async def test_rollback_moves_pointer_and_preserves_versions(
    catalog: PromptCatalogService,
) -> None:
    prompt, v1 = await catalog.create_prompt(namespace="ns", key="k", content="one")
    _, v2 = await catalog.save_version(prompt.id, content="two")
    rolled, active = await catalog.rollback(prompt.id, v1.id)
    assert rolled.active_version_id == v1.id
    assert active.id == v1.id
    # audit preserved: both versions still exist, numbering untouched
    assert [v.version for v in await catalog.list_versions(prompt.id)] == [2, 1]
    # saving after a rollback branches from the rolled-back version
    _, v3 = await catalog.save_version(prompt.id, content="three")
    assert v3.version == 3
    assert v3.parent_version_id == v1.id
    assert v2.content == "two"  # never mutated


async def test_rollback_to_other_prompts_version_is_mismatch(
    catalog: PromptCatalogService,
) -> None:
    prompt_a, _ = await catalog.create_prompt(namespace="ns", key="a", content="a")
    _, version_b = await catalog.create_prompt(namespace="ns", key="b", content="b")
    with pytest.raises(PromptVersionMismatchError):
        await catalog.rollback(prompt_a.id, version_b.id)
    with pytest.raises(PromptVersionNotFoundError):
        await catalog.rollback(prompt_a.id, "missing-version")


async def test_get_version_scoped_to_prompt(catalog: PromptCatalogService) -> None:
    prompt_a, version_a = await catalog.create_prompt(namespace="ns", key="a", content="a")
    _, version_b = await catalog.create_prompt(namespace="ns", key="b", content="b")
    got = await catalog.get_version(prompt_a.id, version_a.id)
    assert got.content == "a"
    with pytest.raises(PromptVersionNotFoundError):
        await catalog.get_version(prompt_a.id, version_b.id)


async def test_archive_filters_list_and_unarchive_restores(
    catalog: PromptCatalogService,
) -> None:
    prompt, _ = await catalog.create_prompt(namespace="ns", key="k", content="x")
    await catalog.set_archived(prompt.id, True)
    assert prompt.archived_at is not None
    assert await catalog.list_prompts() == []
    archived = await catalog.list_prompts(include_archived=True)
    assert [p.id for p, _ in archived] == [prompt.id]
    await catalog.set_archived(prompt.id, False)
    assert prompt.archived_at is None
    assert [p.id for p, _ in await catalog.list_prompts()] == [prompt.id]


async def test_list_prompts_filters_namespace_and_q(catalog: PromptCatalogService) -> None:
    await catalog.create_prompt(namespace="phase", key="story_analysis/system", content="x")
    await catalog.create_prompt(
        namespace="observability", key="elk/system", content="y", description="log search"
    )
    rows = await catalog.list_prompts(namespace="phase")
    assert [p.key for p, _ in rows] == ["story_analysis/system"]
    rows = await catalog.list_prompts(q="log sea")
    assert [p.key for p, _ in rows] == ["elk/system"]
    rows = await catalog.list_prompts()
    assert {p.namespace for p, _ in rows} == {"phase", "observability"}
    for _, active in rows:
        assert active is not None and active.version == 1


async def test_application_catalog_reads_fail_closed_without_explicit_access(
    catalog: PromptCatalogService,
    repo: FakePromptRepository,
) -> None:
    phase, _ = await catalog.create_prompt(namespace="phase", key="story/system", content="phase")
    application, application_v1 = await catalog.create_prompt(
        namespace="application", key="app-1", content="tenant requirements"
    )

    assert [prompt.id for prompt, _ in await catalog.list_prompts()] == [phase.id]
    assert await catalog.list_prompts(namespace="application") == []
    with pytest.raises(PromptNotFoundError):
        await catalog.get_prompt(application.id)
    with pytest.raises(PromptNotFoundError):
        await catalog.list_versions(application.id)
    with pytest.raises(PromptVersionNotFoundError):
        await catalog.get_version(application.id, application_v1.id)
    assert await repo.get_active_version("application", "app-1") is None

    visible = await catalog.list_prompts(allow_application=True)
    assert {prompt.id for prompt, _ in visible} == {phase.id, application.id}
    prompt, active = await catalog.get_prompt(application.id, allow_application=True)
    assert prompt.id == application.id
    assert active is not None and active.content == "tenant requirements"
    assert [
        version.id
        for version in await catalog.list_versions(application.id, allow_application=True)
    ] == [application_v1.id]
    assert (
        await catalog.get_version(
            application.id,
            application_v1.id,
            allow_application=True,
        )
    ).content == "tenant requirements"
    active_by_key = await repo.get_active_version("application", "app-1", allow_application=True)
    assert active_by_key is not None and active_by_key.id == application_v1.id


async def test_unknown_prompt_raises_not_found(catalog: PromptCatalogService) -> None:
    with pytest.raises(PromptNotFoundError):
        await catalog.get_prompt("nope")
    with pytest.raises(PromptNotFoundError):
        await catalog.save_version("nope", content="x")
    with pytest.raises(PromptNotFoundError):
        await catalog.set_archived("nope", True)
    with pytest.raises(PromptNotFoundError):
        await catalog.list_versions("nope")
