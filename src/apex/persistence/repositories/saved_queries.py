"""Async repository for saved work-tracking queries (JQL/WIQL snippets by name)."""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.domain.input_limits import MAX_DESCRIPTION_CHARS, MAX_SCOPE_ID_CHARS
from apex.persistence.models import SavedQuery
from apex.persistence.repositories._conflicts import (
    bounded_driver_message,
    driver_constraint_name,
)
from apex.services.connection_credentials import reject_credential_text

_MUTABLE_FIELDS = frozenset({"name", "provider", "query", "connection_id", "description"})
_TEXT_BOUNDS = {
    "id": (1, 32),
    "name": (1, 255),
    "provider": (1, 64),
    "query": (1, 20_000),
    "connection_id": (1, 32),
    "project_id": (1, MAX_SCOPE_ID_CHARS),
    "description": (0, MAX_DESCRIPTION_CHARS),
    "created_by": (1, 255),
}
_NULLABLE_TEXT_FIELDS = frozenset(
    {"project_id", "connection_id", "description", "created_by"}
)


class SavedQueryNameConflictError(ValueError):
    """A unique global or project-scoped saved-query name was reused."""


def _validate_saved_query_values(values: dict[str, Any]) -> None:
    """Defend direct writers and legacy-row updates at the repository boundary."""

    for field, (minimum, maximum) in _TEXT_BOUNDS.items():
        value = values[field]
        if value is None and field in _NULLABLE_TEXT_FIELDS:
            continue
        if type(value) is not str or not minimum <= len(value) <= maximum or "\x00" in value:
            optional = " or null" if field in _NULLABLE_TEXT_FIELDS else ""
            raise ValueError(
                f"saved query {field} must be a {minimum}-{maximum} character "
                f"string{optional} without U+0000"
            )
        reject_credential_text(value, label=f"saved query {field}")


def _saved_query_values(row: SavedQuery, changes: dict[str, Any] | None = None) -> dict[str, Any]:
    changes = {} if changes is None else changes
    return {
        "id": row.id,
        "name": changes["name"] if "name" in changes else row.name,
        "provider": changes["provider"] if "provider" in changes else row.provider,
        "query": changes["query"] if "query" in changes else row.query,
        "connection_id": (
            changes["connection_id"] if "connection_id" in changes else row.connection_id
        ),
        "project_id": row.project_id,
        "description": (changes["description"] if "description" in changes else row.description),
        "created_by": row.created_by,
    }


class SavedQueriesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, row: SavedQuery) -> SavedQuery:
        values = _saved_query_values(row)
        if values["id"] is None:
            values["id"] = uuid4().hex
        _validate_saved_query_values(values)
        row.id = values["id"]
        self._session.add(row)
        duplicate_name = False
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_duplicate_saved_query_name(exc):
                duplicate_name = True
            else:
                raise
        if duplicate_name:
            raise SavedQueryNameConflictError("saved query name already exists")
        await self._session.refresh(row)
        return row

    async def get(self, saved_query_id: str) -> SavedQuery | None:
        return await self._session.get(SavedQuery, saved_query_id)

    async def get_for_update(self, saved_query_id: str) -> SavedQuery | None:
        """Lock and refresh a mutation target before its ownership is authorized."""

        return await self._session.scalar(
            select(SavedQuery)
            .where(SavedQuery.id == saved_query_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )

    async def list(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        allowed_project_ids: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SavedQuery]:
        """Name-ordered listing. `allowed_project_ids=None` means unrestricted
        (unscoped admin); otherwise rows must be global (project_id NULL) or
        belong to one of the allowed projects."""
        stmt = select(SavedQuery).order_by(SavedQuery.name, SavedQuery.id)
        if allowed_project_ids is not None:
            stmt = stmt.where(
                or_(
                    SavedQuery.project_id.is_(None),
                    SavedQuery.project_id.in_(list(allowed_project_ids)),
                )
            )
        if project is not None:
            stmt = stmt.where(SavedQuery.project_id == project)
        if provider is not None:
            stmt = stmt.where(SavedQuery.provider == provider)
        stmt = stmt.limit(limit).offset(offset)
        return list(await self._session.scalars(stmt))

    async def update(self, row: SavedQuery, changes: dict[str, Any]) -> SavedQuery:
        if type(changes) is not dict or len(changes) > len(_MUTABLE_FIELDS):
            raise ValueError("unsupported saved query fields")
        if any(
            type(field) is not str or field not in _MUTABLE_FIELDS for field in dict.keys(changes)
        ):
            raise ValueError("unsupported saved query fields")
        _validate_saved_query_values(_saved_query_values(row, changes))
        for key, value in changes.items():
            setattr(row, key, value)
        duplicate_name = False
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_duplicate_saved_query_name(exc):
                duplicate_name = True
            else:
                raise
        if duplicate_name:
            raise SavedQueryNameConflictError("saved query name already exists")
        await self._session.refresh(row)
        return row

    async def delete(self, row: SavedQuery) -> None:
        await self._session.delete(row)
        await self._session.commit()


def _is_duplicate_saved_query_name(exc: IntegrityError) -> bool:
    constraint_name = driver_constraint_name(exc.orig)
    duplicate_constraints = {
        "uq_saved_queries_project_id",
        "uq_saved_queries_global_name",
    }
    if constraint_name in duplicate_constraints:
        return True
    message = bounded_driver_message(exc.orig)
    return any(name in message for name in duplicate_constraints) or (
        "unique constraint failed" in message
        and "saved_queries.name" in message
        and (
            "saved_queries.project_id" in message
            or "saved_queries.name" == message.rsplit(":", maxsplit=1)[-1].strip()
        )
    )
