"""AIFx Context Services onboarding: organizations and application repo mappings."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import AifxApplicationSource, AifxOrganization, Application
from apex.persistence.repositories._conflicts import bounded_driver_message, driver_constraint_name

_ORGANIZATION_FIELDS = frozenset(
    {
        "name",
        "connection_id",
        "ghe_owner",
        "global_rules_repo",
        "team_documents_repo_pattern",
        "acb_repo_pattern",
        "default_ref",
        "enabled",
    }
)
_SOURCE_FIELDS = frozenset(
    {"organization_id", "apm_id", "team_documents_repo", "acb_repo", "ref", "enabled"}
)


class AifxConflictError(Exception):
    """A unique name or APM ID is taken; the message is safe to return."""


class AifxInUseError(Exception):
    """The row is still referenced; the message names what references it."""


_CONFLICTS = (
    ("uq_aifx_organizations_name", "an AIFx organization with this name already exists"),
    ("uq_aifx_application_sources_apm_id", "this APM ID is already mapped to another application"),
)


def _conflict_message(exc: IntegrityError) -> str:
    """Name the constraint a write lost to; never surface driver text.

    asyncpg exposes the constraint through ``diag`` or its message, SQLite only
    through its message ("UNIQUE constraint failed: aifx_organizations.name").
    Anything else (a foreign key racing a concurrent delete, a primary key racing
    a concurrent insert) is a stale-write conflict, not a server error.
    """

    constraint = driver_constraint_name(exc.orig)
    message = bounded_driver_message(exc.orig)
    for name, text in _CONFLICTS:
        if constraint == name or name in message:
            return text
    if "aifx_organizations.name" in message:
        return _CONFLICTS[0][1]
    if "aifx_application_sources.apm_id" in message:
        return _CONFLICTS[1][1]
    return "AIFx onboarding changed while saving; reload and retry"


class AifxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _commit(self) -> None:
        conflict: str | None = None
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            conflict = _conflict_message(exc)
        if conflict is not None:
            raise AifxConflictError(conflict)

    # --- organizations -----------------------------------------------------------

    async def list_organizations(self) -> Sequence[AifxOrganization]:
        rows = await self._session.scalars(
            select(AifxOrganization).order_by(func.lower(AifxOrganization.name))
        )
        return rows.all()

    async def get_organization(self, organization_id: str) -> AifxOrganization | None:
        return await self._session.get(AifxOrganization, organization_id)

    async def create_organization(self, fields: dict[str, Any]) -> AifxOrganization:
        row = AifxOrganization(
            **{key: fields[key] for key in fields if key in _ORGANIZATION_FIELDS}
        )
        self._session.add(row)
        await self._commit()
        await self._session.refresh(row)
        return row

    async def update_organization(
        self, row: AifxOrganization, changes: dict[str, Any]
    ) -> AifxOrganization:
        for key, value in changes.items():
            if key in _ORGANIZATION_FIELDS:
                setattr(row, key, value)
        await self._commit()
        await self._session.refresh(row)
        return row

    async def delete_organization(self, row: AifxOrganization) -> None:
        mapped = await self._session.scalar(
            select(AifxApplicationSource.application_id)
            .where(AifxApplicationSource.organization_id == row.id)
            .limit(1)
        )
        if mapped is not None:
            raise AifxInUseError("remove its application mappings first")
        await self._session.delete(row)
        await self._commit()

    # --- application mappings ----------------------------------------------------

    async def list_sources(
        self,
        application_ids: Sequence[str] | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[AifxApplicationSource]:
        statement = select(AifxApplicationSource).order_by(AifxApplicationSource.apm_id)
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        if application_ids is not None:
            if not application_ids:
                return []
            statement = statement.where(
                AifxApplicationSource.application_id.in_(list(application_ids))
            )
        rows = await self._session.scalars(statement)
        return rows.all()

    async def applications_by_id(self, application_ids: Sequence[str]) -> dict[str, Application]:
        """One query for the applications behind a page of mappings (with project links)."""

        if not application_ids:
            return {}
        rows = await self._session.scalars(
            select(Application).where(Application.id.in_(list(application_ids)))
        )
        return {row.id: row for row in rows.all()}

    async def get_source(self, application_id: str) -> AifxApplicationSource | None:
        return await self._session.get(AifxApplicationSource, application_id)

    async def apm_taken(self, apm_id: str, *, except_application_id: str) -> bool:
        """APM IDs are unique case-insensitively, not just byte-for-byte."""

        other = await self._session.scalar(
            select(AifxApplicationSource.application_id)
            .where(
                func.lower(AifxApplicationSource.apm_id) == apm_id.lower(),
                AifxApplicationSource.application_id != except_application_id,
            )
            .limit(1)
        )
        return other is not None

    async def put_source(
        self, application_id: str, fields: dict[str, Any]
    ) -> AifxApplicationSource:
        row = await self.get_source(application_id)
        if row is None:
            row = AifxApplicationSource(application_id=application_id)
            self._session.add(row)
        for key, value in fields.items():
            if key in _SOURCE_FIELDS:
                setattr(row, key, value)
        await self._commit()
        await self._session.refresh(row)
        return row

    async def delete_source(self, row: AifxApplicationSource) -> None:
        await self._session.delete(row)
        await self._commit()
