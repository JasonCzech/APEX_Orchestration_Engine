"""Async SQLAlchemy repository for the prompt catalog aggregate.

Thin persistence CRUD satisfying apex.services.prompts.PromptStore; catalog
invariants (immutability, monotonic version numbers, pointer semantics) live in
PromptCatalogService so they are enforced identically over fakes in tests.
"""

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import Prompt, PromptVersion


class DuplicatePromptKeyError(Exception):
    """The database rejected a duplicate ``(namespace, key)`` prompt."""


class PromptRepository:
    def __init__(self, session: AsyncSession, *, commit_on_write: bool = True) -> None:
        self._session = session
        self._commit_on_write = commit_on_write

    # ── Reads ────────────────────────────────────────────────────────────────

    async def get(self, prompt_id: str) -> Prompt | None:
        return await self._session.get(Prompt, prompt_id)

    async def get_by_key(self, namespace: str, key: str) -> Prompt | None:
        stmt = select(Prompt).where(Prompt.namespace == namespace, Prompt.key == key)
        return await self._session.scalar(stmt)

    async def search(
        self,
        *,
        namespace: str | None = None,
        include_archived: bool = False,
        q: str | None = None,
        allow_application: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Prompt]:
        stmt = select(Prompt).order_by(Prompt.namespace, Prompt.key)
        if namespace is not None:
            stmt = stmt.where(Prompt.namespace == namespace)
        if not allow_application:
            # Application prompts do not carry project ownership yet. Keep them
            # out of ordinary catalog reads until that ownership can be enforced.
            stmt = stmt.where(Prompt.namespace != "application")
        if not include_archived:
            stmt = stmt.where(Prompt.archived_at.is_(None))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(
                    Prompt.key.ilike(like),
                    Prompt.namespace.ilike(like),
                    Prompt.description.ilike(like),
                )
            )
        return list((await self._session.scalars(stmt.limit(limit).offset(offset))).all())

    async def get_version(self, version_id: str) -> PromptVersion | None:
        return await self._session.get(PromptVersion, version_id)

    async def get_versions_by_ids(self, version_ids: list[str]) -> list[PromptVersion]:
        if not version_ids:
            return []
        stmt = select(PromptVersion).where(PromptVersion.id.in_(version_ids))
        return list((await self._session.scalars(stmt)).all())

    async def list_versions(
        self, prompt_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[PromptVersion]:
        stmt = (
            select(PromptVersion)
            .where(PromptVersion.prompt_id == prompt_id)
            .order_by(PromptVersion.version.desc())
        )
        return list((await self._session.scalars(stmt.limit(limit).offset(offset))).all())

    async def max_version(self, prompt_id: str) -> int:
        # Serialize version allocation on the aggregate root. Under PostgreSQL's
        # READ COMMITTED isolation, the max query that follows sees the version
        # committed by the previous lock holder, so concurrent saves allocate
        # distinct monotonic numbers instead of racing on max + 1.
        await self._session.scalar(
            select(Prompt)
            .where(Prompt.id == prompt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        stmt = select(func.max(PromptVersion.version)).where(PromptVersion.prompt_id == prompt_id)
        return (await self._session.scalar(stmt)) or 0

    async def get_active_version(
        self,
        namespace: str,
        key: str,
        *,
        allow_application: bool = False,
    ) -> PromptVersion | None:
        """The active version for an unarchived (namespace, key), or None."""
        if namespace == "application" and not allow_application:
            return None
        stmt = (
            select(PromptVersion)
            .join(Prompt, Prompt.active_version_id == PromptVersion.id)
            .where(
                Prompt.namespace == namespace,
                Prompt.key == key,
                Prompt.archived_at.is_(None),
            )
        )
        return await self._session.scalar(stmt)

    # ── Writes ───────────────────────────────────────────────────────────────

    async def add_prompt(self, prompt: Prompt, first_version: PromptVersion) -> None:
        """Insert prompt + v1 and point the active pointer at v1 (one commit).

        The circular prompts<->prompt_versions FK forces the two-step flush:
        the pointer can only be set once the version row exists.
        """
        try:
            prompt.active_version_id = None
            self._session.add(prompt)
            await self._session.flush()
            self._session.add(first_version)
            await self._session.flush()
            prompt.active_version_id = first_version.id
            await self._finish_write()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_duplicate_prompt_key(exc):
                raise DuplicatePromptKeyError(str(exc.orig)) from exc
            raise

    async def add_version(self, prompt: Prompt, version: PromptVersion) -> None:
        """Insert an immutable version and move the active pointer (one commit)."""
        self._session.add(version)
        await self._session.flush()
        prompt.active_version_id = version.id
        await self._finish_write()

    async def save(self, prompt: Prompt) -> None:
        """Persist pointer/archive mutations made by the service."""
        self._session.add(prompt)
        await self._finish_write()

    async def _finish_write(self) -> None:
        if self._commit_on_write:
            await self._session.commit()
        else:
            await self._session.flush()


def _is_duplicate_prompt_key(exc: IntegrityError) -> bool:
    constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    message = str(exc.orig).lower()
    return constraint_name == "uq_prompts_namespace" or (
        "uq_prompts_namespace" in message
        or (
            "unique constraint failed" in message
            and "prompts.namespace" in message
            and "prompts.key" in message
        )
    )
