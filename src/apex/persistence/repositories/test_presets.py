"""Async repository for team and personal test presets (`test_presets`)."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.auth.identity import ScopeRef
from apex.persistence.models import RunPresetLink, TestPreset, TestPresetVersion
from apex.services.analytics_scope import analytics_scope_filter


class PresetVersionConflictError(Exception):
    """The preset changed since the caller loaded it."""


class TestPresetsRepository:
    __test__ = False  # not a pytest class

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        viewer_consumer_id: str | None = None,
        project_id: str | None = None,
        application_id: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[TestPreset]:
        """Storage filter only; the rest of identity visibility is the router's job.
        ``project_ids=None`` means every project (unscoped callers); with
        ``viewer_consumer_id``, other people's personal presets are left out."""
        stmt = select(TestPreset).order_by(TestPreset.name, TestPreset.id)
        if project_ids is not None:
            stmt = stmt.where(TestPreset.project_id.in_(list(project_ids)))
        if viewer_consumer_id is not None:
            stmt = stmt.where(
                or_(
                    TestPreset.visibility != "personal",
                    TestPreset.created_by_consumer_id == viewer_consumer_id,
                )
            )
        if project_id is not None:
            stmt = stmt.where(TestPreset.project_id == project_id)
        if application_id is not None:
            stmt = stmt.where(TestPreset.application_id == application_id)
        if not include_archived:
            stmt = stmt.where(TestPreset.archived_at.is_(None))
        return list((await self._session.scalars(stmt.limit(limit).offset(offset))).all())

    async def get(self, preset_id: str) -> TestPreset | None:
        return await self._session.get(TestPreset, preset_id)

    async def get_for_update(self, preset_id: str) -> TestPreset | None:
        return await self._session.scalar(
            select(TestPreset).where(TestPreset.id == preset_id).with_for_update()
        )

    async def create(
        self, *, created_by: str, created_by_consumer_id: str, **fields: Any
    ) -> TestPreset:
        preset = TestPreset(
            **fields,
            version=1,
            created_by=created_by,
            created_by_consumer_id=created_by_consumer_id,
            updated_by=created_by,
        )
        self._session.add(preset)
        await self._session.flush()
        self._session.add(_version_row(preset, note="Created", created_by=created_by))
        await self._session.commit()
        await self._session.refresh(preset)
        return preset

    async def update(
        self,
        preset: TestPreset,
        *,
        expected_version: int | None,
        updated_by: str,
        note: str | None,
        **fields: Any,
    ) -> TestPreset:
        if expected_version is not None and preset.version != expected_version:
            preset_id = preset.id
            await self._session.rollback()
            raise PresetVersionConflictError(preset_id)
        for key, value in fields.items():
            setattr(preset, key, value)
        preset.version += 1
        preset.updated_by = updated_by
        preset.updated_at = datetime.now(UTC)
        self._session.add(_version_row(preset, note=note, created_by=updated_by))
        await self._session.commit()
        await self._session.refresh(preset)
        return preset

    async def set_archived(self, preset: TestPreset, archived: bool) -> TestPreset:
        preset.archived_at = datetime.now(UTC) if archived else None
        await self._session.commit()
        await self._session.refresh(preset)
        return preset

    async def delete(self, preset: TestPreset) -> None:
        await self._session.delete(preset)
        await self._session.commit()

    async def versions(self, preset_id: str) -> list[TestPresetVersion]:
        stmt = (
            select(TestPresetVersion)
            .where(TestPresetVersion.preset_id == preset_id)
            .order_by(TestPresetVersion.version.desc())
        )
        return list((await self._session.scalars(stmt)).all())

    async def usage(
        self,
        *,
        kind: str,
        preset_ids: Sequence[str],
        visible_scopes: Sequence[ScopeRef] | None,
    ) -> dict[str, tuple[int, datetime | None]]:
        """Tests started from each preset that the caller may see: (count, last used)."""
        if not preset_ids:
            return {}
        stmt = (
            select(
                RunPresetLink.preset_id,
                func.count(RunPresetLink.thread_id),
                func.max(RunPresetLink.recorded_at),
            )
            .where(
                RunPresetLink.preset_kind == kind,
                RunPresetLink.preset_id.in_(list(preset_ids)),
            )
            .group_by(RunPresetLink.preset_id)
        )
        visible = analytics_scope_filter(RunPresetLink, visible_scopes)
        if visible is not None:
            stmt = stmt.where(visible)
        rows = (await self._session.execute(stmt)).tuples()
        return {preset_id: (count, last) for preset_id, count, last in rows}

    async def link_for_thread(self, thread_id: str) -> RunPresetLink | None:
        return await self._session.get(RunPresetLink, thread_id)


def _version_row(preset: TestPreset, *, note: str | None, created_by: str) -> TestPresetVersion:
    return TestPresetVersion(
        preset_id=preset.id,
        version=preset.version,
        name=preset.name,
        description=preset.description,
        payload=preset.payload,
        note=note,
        created_by=created_by,
    )
