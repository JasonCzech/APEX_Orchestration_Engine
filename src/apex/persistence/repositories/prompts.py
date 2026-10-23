"""Async SQLAlchemy repository for the prompt catalog aggregate.

Thin persistence CRUD satisfying apex.services.prompts.PromptStore; catalog
invariants (immutability, monotonic version numbers, pointer semantics) live in
PromptCatalogService so they are enforced identically over fakes in tests.
"""

import re

from sqlalchemy import and_, false, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.auth.identity import ConsumerIdentity
from apex.persistence.models import Application, Environment, Prompt, PromptVersion
from apex.persistence.repositories._conflicts import (
    bounded_driver_message,
    driver_constraint_name,
)
from apex.persistence.repositories.catalog import application_scope_filter


class DuplicatePromptKeyError(Exception):
    """The database rejected a duplicate ``(namespace, key)`` prompt."""


def search_pattern(q: str) -> str:
    """A case-insensitive LIKE pattern for catalog search.

    ``%`` and ``\\`` match literally. Spaces, underscores and hyphens all match
    one another, so "env triage" finds the ``env_triage`` key.
    """
    escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%")
    return "%" + re.sub(r"[\s_-]+", "_", escaped) + "%"


class PromptRepository:
    def __init__(
        self,
        session: AsyncSession,
        *,
        commit_on_write: bool = True,
        identity: ConsumerIdentity | None = None,
    ) -> None:
        self.identity = identity
        self._session = session
        self._commit_on_write = commit_on_write

    def readable(self):
        identity = self.identity
        if identity is None or identity.is_unscoped:
            return true()
        app_scope = application_scope_filter(identity.scopes)
        apps = select(Application.id).where(
            app_scope if app_scope is not None else false(), Application.archived_at.is_(None)
        )
        envs = select(Environment.id).where(Environment.application_id.in_(apps))
        return or_(
            Prompt.namespace.not_in(["application", "environment"]),
            and_(
                Prompt.namespace == "application",
                or_(Prompt.is_shared.is_(True), Prompt.application_id.in_(apps)),
            ),
            and_(Prompt.namespace == "environment", Prompt.environment_id.in_(envs)),
        )

    async def validate_associations(
        self, application_id: str | None, environment_id: str | None
    ) -> None:
        if application_id and await self._session.get(Application, application_id) is None:
            raise ValueError("Application not found")
        if environment_id and await self._session.get(Environment, environment_id) is None:
            raise ValueError("Environment not found")

    async def set_default(self, prompt_id: str) -> Prompt:
        # Lock the complete default family in stable order, including the current
        # default; the partial unique indexes protect against concurrent inserts.
        target = await self.get(prompt_id)
        if target is None:
            raise ValueError("Prompt not found")
        family = and_(Prompt.namespace == target.namespace, Prompt.phase == target.phase)
        locked = (
            await self._session.scalars(
                select(Prompt)
                .where(family)
                .order_by(Prompt.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        target = next((row for row in locked if row.id == prompt_id), None)
        if target is None:
            raise ValueError("Prompt not found")
        if (
            target.namespace not in {"system", "phase"}
            or not target.selection_enabled
            or target.archived_at
            or not target.active_version_id
        ):
            raise ValueError("Only active selectable system or phase prompts can be defaults")
        await self._session.execute(update(Prompt).where(family).values(is_default=False))
        await self._session.flush()
        target.is_default = True
        await self._finish_write()
        return target

    # ── Reads ────────────────────────────────────────────────────────────────

    async def get(self, prompt_id: str) -> Prompt | None:
        return await self._session.scalar(
            select(Prompt).where(Prompt.id == prompt_id, self.readable())
        )

    async def get_default(self, namespace: str, *, phase: str | None = None) -> Prompt | None:
        """Read the designated default without paginating catalog alternatives."""
        return await self._session.scalar(
            select(Prompt).where(
                Prompt.namespace == namespace,
                Prompt.phase == phase,
                Prompt.is_default.is_(True),
                self.readable(),
            )
        )

    async def get_by_key(self, namespace: str, key: str) -> Prompt | None:
        stmt = select(Prompt).where(
            Prompt.namespace == namespace, Prompt.key == key, self.readable()
        )
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
        phase: str | None = None,
        application_id: str | None = None,
        include_shared: bool = False,
        environment_id: str | None = None,
        selection_enabled: bool | None = None,
    ) -> list[Prompt]:
        stmt = select(Prompt).where(self.readable()).order_by(Prompt.namespace, Prompt.key)
        if phase is not None:
            stmt = stmt.where(Prompt.phase == phase)
        if application_id is not None:
            stmt = stmt.where(
                or_(
                    Prompt.application_id == application_id,
                    Prompt.is_shared.is_(True) if include_shared else false(),
                )
            )
        elif include_shared:
            stmt = stmt.where(Prompt.is_shared.is_(True))
        if environment_id is not None:
            stmt = stmt.where(Prompt.environment_id == environment_id)
        if selection_enabled is not None:
            stmt = stmt.where(Prompt.selection_enabled == selection_enabled)
        if namespace is not None:
            stmt = stmt.where(Prompt.namespace == namespace)
        if not allow_application and self.identity is None:
            # Application prompts do not carry project ownership yet. Keep them
            # out of ordinary catalog reads until that ownership can be enforced.
            stmt = stmt.where(Prompt.namespace != "application")
        if not include_archived:
            stmt = stmt.where(Prompt.archived_at.is_(None))
        if q:
            like = search_pattern(q)
            active_text = (
                select(PromptVersion.id)
                .where(
                    PromptVersion.id == Prompt.active_version_id,
                    PromptVersion.content.ilike(like, escape="\\"),
                )
                .exists()
            )
            stmt = stmt.where(
                or_(
                    Prompt.key.ilike(like, escape="\\"),
                    Prompt.namespace.ilike(like, escape="\\"),
                    Prompt.description.ilike(like, escape="\\"),
                    active_text,
                )
            )
        return list((await self._session.scalars(stmt.limit(limit).offset(offset))).all())

    async def get_version(self, version_id: str) -> PromptVersion | None:
        return await self._session.scalar(
            select(PromptVersion)
            .join(Prompt, Prompt.id == PromptVersion.prompt_id)
            .where(PromptVersion.id == version_id, self.readable())
        )

    async def get_versions_by_ids(self, version_ids: list[str]) -> list[PromptVersion]:
        if not version_ids:
            return []
        stmt = (
            select(PromptVersion)
            .join(Prompt, Prompt.id == PromptVersion.prompt_id)
            .where(PromptVersion.id.in_(version_ids), self.readable())
        )
        return list((await self._session.scalars(stmt)).all())

    async def list_versions(
        self, prompt_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[PromptVersion]:
        stmt = (
            select(PromptVersion)
            .join(Prompt, Prompt.id == PromptVersion.prompt_id)
            .where(PromptVersion.prompt_id == prompt_id, self.readable())
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
        if self.identity is None and namespace == "application" and not allow_application:
            return None
        stmt = (
            select(PromptVersion)
            .join(Prompt, Prompt.active_version_id == PromptVersion.id)
            .where(
                Prompt.namespace == namespace,
                Prompt.key == key,
                Prompt.archived_at.is_(None),
                self.readable(),
            )
        )
        return await self._session.scalar(stmt)

    # ── Writes ───────────────────────────────────────────────────────────────

    async def add_prompt(self, prompt: Prompt, first_version: PromptVersion) -> None:
        """Insert prompt + v1 and point the active pointer at v1 (one commit).

        The circular prompts<->prompt_versions FK forces the two-step flush:
        the pointer can only be set once the version row exists.
        """
        duplicate_key = False
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
                duplicate_key = True
            else:
                raise
        if duplicate_key:
            # Do not retain the raw driver exception in the public conflict:
            # constraint diagnostics can contain caller-controlled prompt keys.
            raise DuplicatePromptKeyError("prompt key already exists")

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
    constraint_name = driver_constraint_name(exc.orig)
    message = bounded_driver_message(exc.orig)
    return constraint_name == "uq_prompts_namespace" or (
        "uq_prompts_namespace" in message
        or (
            "unique constraint failed" in message
            and "prompts.namespace" in message
            and "prompts.key" in message
        )
    )
