"""Async repository for the application/environment catalog (legacy app_env).

Applications carry project_id; environments inherit it via their application.
Visibility filtering is parameterized (`visible_projects`) so routers translate
identity scopes into a plain allow-list — the repository stays auth-agnostic.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apex.persistence.models import (
    Application,
    Environment,
    EnvironmentHost,
    EnvironmentSnapshot,
)


class DuplicateNameError(Exception):
    """A unique (parent, name) constraint was violated."""


def _environment_query() -> Select[tuple[Environment]]:
    return select(Environment).options(
        selectinload(Environment.application), selectinload(Environment.hosts)
    )


def _build_hosts(hosts: Sequence[dict[str, Any]]) -> list[EnvironmentHost]:
    return [EnvironmentHost(hostname=h["hostname"], role=h.get("role")) for h in hosts]


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── applications ────────────────────────────────────────────────────────

    async def list_applications(
        self,
        *,
        project: str | None = None,
        visible_projects: Sequence[str] | None = None,
        include_archived: bool = False,
    ) -> list[Application]:
        """`visible_projects=None` means unrestricted (admin/unscoped consumers)."""
        stmt = select(Application).order_by(Application.project_id, Application.name)
        if project is not None:
            stmt = stmt.where(Application.project_id == project)
        if visible_projects is not None:
            stmt = stmt.where(Application.project_id.in_(list(visible_projects)))
        if not include_archived:
            stmt = stmt.where(Application.archived_at.is_(None))
        return list((await self._session.scalars(stmt)).all())

    async def get_application(self, application_id: str) -> Application | None:
        return await self._session.get(Application, application_id)

    async def create_application(
        self, *, project_id: str, name: str, description: str | None = None
    ) -> Application:
        app = Application(project_id=project_id, name=name, description=description)
        self._session.add(app)
        await self._commit_and_refresh(app)
        return app

    async def update_application(self, app: Application, changes: dict[str, Any]) -> Application:
        for field, value in changes.items():
            setattr(app, field, value)
        await self._commit_and_refresh(app)
        return app

    async def set_application_archived(self, app: Application, archived: bool) -> Application:
        app.archived_at = datetime.now(UTC) if archived else None
        await self._commit_and_refresh(app)
        return app

    async def delete_application(self, app: Application) -> None:
        await self._session.delete(app)
        await self._session.commit()

    # ── environments ────────────────────────────────────────────────────────

    async def list_environments(
        self,
        *,
        application_id: str | None = None,
        visible_projects: Sequence[str] | None = None,
    ) -> list[Environment]:
        stmt = (
            _environment_query()
            .join(Application)
            .order_by(Application.project_id, Application.name, Environment.name)
        )
        if application_id is not None:
            stmt = stmt.where(Environment.application_id == application_id)
        if visible_projects is not None:
            stmt = stmt.where(Application.project_id.in_(list(visible_projects)))
        return list((await self._session.scalars(stmt)).all())

    async def get_environment(self, environment_id: str) -> Environment | None:
        """Loaded with .application (for scope checks) and .hosts."""
        return await self._session.scalar(
            _environment_query().where(Environment.id == environment_id)
        )

    async def create_environment(
        self,
        *,
        application_id: str,
        name: str,
        kind: str | None = None,
        base_url: str | None = None,
        target_approved: bool = False,
        target_version: int = 0,
        options: dict[str, Any] | None = None,
        hosts: Sequence[dict[str, Any]] = (),
    ) -> Environment:
        env = Environment(
            application_id=application_id,
            name=name,
            kind=kind,
            base_url=base_url,
            target_approved=target_approved,
            target_version=target_version,
            options=dict(options or {}),
        )
        env.hosts = _build_hosts(hosts)
        self._session.add(env)
        await self._commit()
        return await self._reload_environment(env.id)

    async def update_environment(
        self,
        env: Environment,
        changes: dict[str, Any],
        hosts: Sequence[dict[str, Any]] | None = None,
    ) -> Environment:
        """Patch scalar fields; when `hosts` is given the full list is replaced."""
        for field, value in changes.items():
            setattr(env, field, value)
        if hosts is not None:
            env.hosts = _build_hosts(hosts)  # delete-orphan cascade drops the old rows
        await self._commit()
        return await self._reload_environment(env.id)

    async def delete_environment(self, env: Environment) -> None:
        await self._session.delete(env)
        await self._session.commit()

    async def latest_snapshot(self, environment_id: str) -> EnvironmentSnapshot | None:
        stmt = (
            select(EnvironmentSnapshot)
            .where(EnvironmentSnapshot.environment_id == environment_id)
            .order_by(EnvironmentSnapshot.scanned_at.desc())
            .limit(1)
        )
        return await self._session.scalar(stmt)

    # ── internals ───────────────────────────────────────────────────────────

    async def _commit(self) -> None:
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise DuplicateNameError(str(exc.orig)) from exc

    async def _commit_and_refresh(self, instance: Application) -> None:
        await self._commit()
        await self._session.refresh(instance)

    async def _reload_environment(self, environment_id: str) -> Environment:
        """Re-fetch with eager loads so server defaults and hosts are populated."""
        env = await self.get_environment(environment_id)
        if env is None:  # pragma: no cover — row was just committed
            raise RuntimeError(f"environment {environment_id!r} vanished after commit")
        return env
