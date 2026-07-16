"""Async repository for the `engine_runs` history projection (M3).

Read side of the projection that the execution phase upserts best-effort (see
apex.services.engine_runs) — dashboard history queries plus the abort path's
direct status update. Rows carry project_id so /v1 reads can enforce the same
scope boundary as LangGraph thread metadata.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, and_, false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apex.auth.identity import ScopeRef
from apex.persistence.models import EngineRun

TERMINAL_STATUSES = ("completed", "failed", "aborted")


def _visibility_filter(
    *,
    allowed_scopes: Sequence[ScopeRef] | None,
    allowed_project_ids: tuple[str, ...] | None,
) -> ColumnElement[bool] | None:
    """Translate project/app grants into one ownership predicate.

    ``allowed_project_ids`` remains as a compatibility seam for callers that have
    not yet adopted app-aware scopes. New API callers must pass ``allowed_scopes``.
    A project-wide scope sees all apps in that project; an app scope sees only the
    exact app plus deliberately project-level rows. Rows migrated from before app
    ownership existed remain hidden from app-only scopes because their owner is
    unknown; project-wide scopes can still inspect and remediate them.
    """
    if allowed_scopes is not None:
        if not allowed_scopes:
            return false()
        scoped_projects = {scope.project_id for scope in allowed_scopes}
        project_wide = {scope.project_id for scope in allowed_scopes if scope.app_id is None}
        clauses: list[ColumnElement[bool]] = [
            and_(
                EngineRun.project_id.in_(sorted(scoped_projects)),
                EngineRun.app_id.is_(None),
                EngineRun.ownership_known.is_(True),
                EngineRun.scope_ownership_known.is_(True),
            )
        ]
        if project_wide:
            clauses.append(EngineRun.project_id.in_(sorted(project_wide)))
        clauses.extend(
            and_(
                EngineRun.project_id == scope.project_id,
                EngineRun.app_id == scope.app_id,
                # App-scoped reads must never trust a row that
                # migration/rolling-write provenance deliberately quarantined.
                # A project-wide administrator can still inspect and remediate
                # ambiguous rows for the whole project.
                EngineRun.ownership_known.is_(True),
                EngineRun.scope_ownership_known.is_(True),
            )
            for scope in allowed_scopes
            if scope.app_id is not None and scope.project_id not in project_wide
        )
        return or_(*clauses) if clauses else false()
    if allowed_project_ids is not None:
        return EngineRun.project_id.in_(allowed_project_ids) if allowed_project_ids else false()
    return None


def _append_visibility(
    filters: list[ColumnElement[bool]],
    *,
    allowed_scopes: Sequence[ScopeRef] | None,
    allowed_project_ids: tuple[str, ...] | None,
) -> None:
    visibility = _visibility_filter(
        allowed_scopes=allowed_scopes,
        allowed_project_ids=allowed_project_ids,
    )
    if visibility is not None:
        filters.append(visibility)


def _mutation_filter(
    *,
    allowed_scopes: Sequence[ScopeRef] | None,
    allowed_project_ids: tuple[str, ...] | None,
) -> ColumnElement[bool] | None:
    """Strict ownership predicate for destructive engine-run operations.

    App-only scopes may read explicitly-known project-level history for context,
    but they cannot mutate that wider audience. Project-level rows require a
    project-wide grant; app grants mutate only the exact app owner.
    """

    if allowed_scopes is not None:
        if not allowed_scopes:
            return false()
        project_wide = {scope.project_id for scope in allowed_scopes if scope.app_id is None}
        clauses: list[ColumnElement[bool]] = []
        if project_wide:
            clauses.append(EngineRun.project_id.in_(sorted(project_wide)))
        clauses.extend(
            and_(
                EngineRun.project_id == scope.project_id,
                EngineRun.app_id == scope.app_id,
                EngineRun.ownership_known.is_(True),
                EngineRun.scope_ownership_known.is_(True),
            )
            for scope in allowed_scopes
            if scope.app_id is not None and scope.project_id not in project_wide
        )
        return or_(*clauses) if clauses else false()
    if allowed_project_ids is not None:
        return EngineRun.project_id.in_(allowed_project_ids) if allowed_project_ids else false()
    return None


def _append_mutation_scope(
    filters: list[ColumnElement[bool]],
    *,
    allowed_scopes: Sequence[ScopeRef] | None,
    allowed_project_ids: tuple[str, ...] | None,
) -> None:
    predicate = _mutation_filter(
        allowed_scopes=allowed_scopes,
        allowed_project_ids=allowed_project_ids,
    )
    if predicate is not None:
        filters.append(predicate)


class EngineRunsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def release_read_transaction(self) -> None:
        """Release a request-scoped read snapshot before slow external I/O."""

        if self._session.in_transaction():
            await self._session.rollback()

    async def list_runs(
        self,
        *,
        engine: str | None = None,
        status: str | None = None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EngineRun], int]:
        """One page of runs, newest started first, plus the unpaged filtered total.

        (Named list_runs, not list: a method named `list` would shadow the builtin
        in the class scope used by later methods' annotations.)
        """
        filters: list[ColumnElement[bool]] = []
        if engine is not None:
            filters.append(EngineRun.engine == engine)
        if status is not None:
            filters.append(EngineRun.status == status)
        _append_visibility(
            filters,
            allowed_scopes=allowed_scopes,
            allowed_project_ids=allowed_project_ids,
        )
        stmt = (
            select(EngineRun)
            .where(*filters)
            .order_by(EngineRun.started_at.desc(), EngineRun.id)
            .limit(limit)
            .offset(offset)
        )
        rows = list(await self._session.scalars(stmt))
        total = await self._session.scalar(
            select(func.count()).select_from(EngineRun).where(*filters)
        )
        return rows, int(total or 0)

    async def list_for_thread(
        self,
        thread_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EngineRun]:
        """One bounded page of attempts for a thread, newest attempt first."""
        filters: list[ColumnElement[bool]] = [EngineRun.thread_id == thread_id]
        _append_visibility(
            filters,
            allowed_scopes=allowed_scopes,
            allowed_project_ids=allowed_project_ids,
        )
        stmt = (
            select(EngineRun)
            .where(*filters)
            .order_by(EngineRun.attempt.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await self._session.scalars(stmt))

    async def get_latest_for_thread(
        self,
        thread_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None:
        rows = await self.list_for_thread(
            thread_id,
            allowed_scopes=allowed_scopes,
            allowed_project_ids=allowed_project_ids,
            limit=1,
        )
        return rows[0] if rows else None

    async def get_latest_abortable_for_thread(
        self,
        thread_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None:
        filters: list[ColumnElement[bool]] = [
            EngineRun.thread_id == thread_id,
            EngineRun.status.not_in(TERMINAL_STATUSES),
        ]
        _append_mutation_scope(
            filters,
            allowed_scopes=allowed_scopes,
            allowed_project_ids=allowed_project_ids,
        )
        return await self._session.scalar(
            select(EngineRun).where(*filters).order_by(EngineRun.attempt.desc()).limit(1)
        )

    async def get_by_external_run_id(
        self,
        external_run_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None:
        filters: list[ColumnElement[bool]] = [EngineRun.external_run_id == external_run_id]
        _append_visibility(
            filters,
            allowed_scopes=allowed_scopes,
            allowed_project_ids=allowed_project_ids,
        )
        stmt = (
            select(EngineRun)
            .where(*filters)
            .order_by(EngineRun.started_at.desc(), EngineRun.id)
            .limit(1)
        )
        return await self._session.scalar(stmt)

    async def get_by_artifact_namespace(
        self,
        artifact_namespace: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None:
        """Resolve the internal, collision-resistant namespace for artifact auth."""
        filters: list[ColumnElement[bool]] = [
            EngineRun.artifact_namespace == artifact_namespace.rstrip("/")
        ]
        _append_visibility(
            filters,
            allowed_scopes=allowed_scopes,
            allowed_project_ids=allowed_project_ids,
        )
        return await self._session.scalar(select(EngineRun).where(*filters))

    async def mark_aborted(
        self,
        thread_id: str,
        *,
        projection_id: str,
        attempt: int,
        expected_external_run_id: str | None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> int:
        return await self.mark_terminal(
            thread_id,
            "aborted",
            projection_id=projection_id,
            attempt=attempt,
            expected_external_run_id=expected_external_run_id,
            allowed_scopes=allowed_scopes,
            allowed_project_ids=allowed_project_ids,
        )

    async def mark_terminal(
        self,
        thread_id: str,
        status: str,
        *,
        projection_id: str,
        attempt: int,
        expected_external_run_id: str | None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> int:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"nonterminal engine status {status!r}")
        filters = [
            EngineRun.id == projection_id,
            EngineRun.thread_id == thread_id,
            EngineRun.attempt == attempt,
            EngineRun.status.not_in(TERMINAL_STATUSES),
        ]
        if expected_external_run_id is None:
            filters.append(EngineRun.external_run_id.is_(None))
        else:
            filters.append(EngineRun.external_run_id == expected_external_run_id)
        _append_mutation_scope(
            filters,
            allowed_scopes=allowed_scopes,
            allowed_project_ids=allowed_project_ids,
        )
        result = await self._session.execute(
            update(EngineRun)
            .where(*filters)
            .values(status=status, ended_at=datetime.now(UTC), connection_id=None)
        )
        await self._session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
