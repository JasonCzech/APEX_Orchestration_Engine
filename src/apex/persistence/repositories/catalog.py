"""Async repository for the application/environment catalog (legacy app_env).

Applications carry project_id; environments inherit it via their application.
Visibility filtering is parameterized (`visible_projects`) so routers translate
identity scopes into a plain allow-list — the repository stays auth-agnostic.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, false, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apex.auth.identity import ScopeRef
from apex.persistence.models import (
    Application,
    Environment,
    EnvironmentHost,
    EnvironmentSnapshot,
)
from apex.services.connection_credentials import (
    environment_target_requires_repair,
    reject_raw_secret_options,
)
from apex.services.connections import (
    TRUSTED_PRIVATE_HOST_OPTION,
    validate_adapter_base_url,
)


class DuplicateNameError(Exception):
    """A unique (parent, name) constraint was violated."""


_ENVIRONMENT_MUTABLE_FIELDS = frozenset(
    {"name", "kind", "base_url", "options", "target_approved", "target_version"}
)
_MAX_ENVIRONMENT_HOSTS = 256


def _bounded_text(
    value: Any,
    *,
    label: str,
    max_chars: int,
    min_chars: int = 1,
    allow_none: bool = False,
) -> str | None:
    if value is None and allow_none:
        return None
    if (
        not isinstance(value, str)
        or len(value) < min_chars
        or len(value) > max_chars
        or "\x00" in value
    ):
        optional = " or null" if allow_none else ""
        raise ValueError(
            f"{label} must be a {min_chars}-{max_chars} character string{optional} "
            "without U+0000"
        )
    return value


def _validate_environment_target_metadata(
    *,
    base_url: Any,
    options: Any,
    target_approved: Any,
    target_version: Any,
) -> dict[str, Any]:
    if not isinstance(options, dict):
        raise ValueError("environment options must be an object")
    normalized_options = dict(options)
    reject_raw_secret_options(
        normalized_options,
        label="environment options",
        reference="a managed connection secret_ref",
    )
    if environment_target_requires_repair(base_url, normalized_options):
        raise ValueError(
            "environment target contains unsafe credential-bearing configuration"
        )
    if base_url is not None:
        _bounded_text(base_url, label="environment base_url", max_chars=1_024)
        validate_adapter_base_url(
            base_url,
            allow_private_hosts=(
                normalized_options.get(TRUSTED_PRIVATE_HOST_OPTION) is True or None
            ),
        )
    if not isinstance(target_approved, bool):
        raise ValueError("environment target_approved must be a boolean")
    if (
        isinstance(target_version, bool)
        or not isinstance(target_version, int)
        or not 0 <= target_version <= 2_147_483_647
    ):
        raise ValueError("environment target_version must be an integer between 0 and 2147483647")
    if target_approved and (base_url is None or target_version < 1):
        raise ValueError("an approved environment target requires a URL and positive version")
    return normalized_options


def _environment_query() -> Select[tuple[Environment]]:
    return select(Environment).options(
        selectinload(Environment.application), selectinload(Environment.hosts)
    )


def _build_hosts(hosts: Sequence[dict[str, Any]]) -> list[EnvironmentHost]:
    if len(hosts) > _MAX_ENVIRONMENT_HOSTS:
        raise ValueError(f"environment may contain at most {_MAX_ENVIRONMENT_HOSTS} hosts")
    built: list[EnvironmentHost] = []
    for host in hosts:
        if not isinstance(host, dict):
            raise ValueError("environment hosts must be objects")
        hostname = _bounded_text(
            host.get("hostname"), label="environment hostname", max_chars=1_024
        )
        role = _bounded_text(
            host.get("role"),
            label="environment host role",
            max_chars=255,
            min_chars=0,
            allow_none=True,
        )
        built.append(EnvironmentHost(hostname=hostname, role=role))
    return built


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── applications ────────────────────────────────────────────────────────

    async def list_applications(
        self,
        *,
        project: str | None = None,
        visible_projects: Sequence[str] | None = None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Application]:
        """`visible_projects=None` means unrestricted (admin/unscoped consumers)."""
        stmt = select(Application).order_by(Application.project_id, Application.name)
        if project is not None:
            stmt = stmt.where(Application.project_id == project)
        if visible_projects is not None:
            stmt = stmt.where(Application.project_id.in_(list(visible_projects)))
        if allowed_scopes is not None:
            scope_filter = _application_scope_filter(allowed_scopes)
            stmt = stmt.where(scope_filter if scope_filter is not None else false())
        if not include_archived:
            stmt = stmt.where(Application.archived_at.is_(None))
        return list((await self._session.scalars(stmt.limit(limit).offset(offset))).all())

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
        allowed_scopes: Sequence[ScopeRef] | None = None,
        limit: int = 100,
        offset: int = 0,
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
        if allowed_scopes is not None:
            scope_filter = _environment_scope_filter(allowed_scopes)
            stmt = stmt.where(scope_filter if scope_filter is not None else false())
        return list((await self._session.scalars(stmt.limit(limit).offset(offset))).all())

    async def get_environment(self, environment_id: str) -> Environment | None:
        """Loaded with .application (for scope checks) and .hosts."""
        return await self._session.scalar(
            _environment_query().where(Environment.id == environment_id)
        )

    async def get_environment_for_update(self, environment_id: str) -> Environment | None:
        """Load and lock the aggregate root before replacing hosts or deleting."""

        return await self._session.scalar(
            _environment_query()
            .where(Environment.id == environment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
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
        _bounded_text(application_id, label="environment application_id", max_chars=32)
        _bounded_text(name, label="environment name", max_chars=255)
        _bounded_text(
            kind, label="environment kind", max_chars=64, min_chars=0, allow_none=True
        )
        normalized_options = _validate_environment_target_metadata(
            base_url=base_url,
            options={} if options is None else options,
            target_approved=target_approved,
            target_version=target_version,
        )
        built_hosts = _build_hosts(hosts)
        env = Environment(
            application_id=application_id,
            name=name,
            kind=kind,
            base_url=base_url,
            target_approved=target_approved,
            target_version=target_version,
            options=normalized_options,
        )
        env.hosts = built_hosts
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
        unknown = sorted(set(changes).difference(_ENVIRONMENT_MUTABLE_FIELDS))
        if unknown:
            raise ValueError(f"unsupported environment fields: {', '.join(unknown)}")
        effective_name = changes.get("name", env.name)
        effective_kind = changes.get("kind", env.kind)
        effective_base_url = changes.get("base_url", env.base_url)
        effective_options = changes.get("options", env.options)
        effective_approved = changes.get("target_approved", env.target_approved)
        effective_version = changes.get("target_version", env.target_version)
        _bounded_text(effective_name, label="environment name", max_chars=255)
        _bounded_text(
            effective_kind,
            label="environment kind",
            max_chars=64,
            min_chars=0,
            allow_none=True,
        )
        normalized_options = _validate_environment_target_metadata(
            base_url=effective_base_url,
            options=effective_options,
            target_approved=effective_approved,
            target_version=effective_version,
        )
        built_hosts = _build_hosts(hosts) if hosts is not None else None
        if "options" in changes:
            changes = {**changes, "options": normalized_options}
        for field, value in changes.items():
            setattr(env, field, value)
        if built_hosts is not None:
            env.hosts = built_hosts  # delete-orphan cascade drops the old rows
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


def _application_scope_filter(scopes: Sequence[ScopeRef]) -> Any | None:
    project_wide = {scope.project_id for scope in scopes if scope.app_id is None}
    clauses = [Application.project_id == project_id for project_id in sorted(project_wide)]
    clauses.extend(
        and_(Application.project_id == scope.project_id, Application.id == scope.app_id)
        for scope in scopes
        if scope.app_id is not None and scope.project_id not in project_wide
    )
    return or_(*clauses) if clauses else None


def _environment_scope_filter(scopes: Sequence[ScopeRef]) -> Any | None:
    project_wide = {scope.project_id for scope in scopes if scope.app_id is None}
    clauses = [Application.project_id == project_id for project_id in sorted(project_wide)]
    clauses.extend(
        and_(
            Application.project_id == scope.project_id,
            Environment.application_id == scope.app_id,
        )
        for scope in scopes
        if scope.app_id is not None and scope.project_id not in project_wide
    )
    return or_(*clauses) if clauses else None
