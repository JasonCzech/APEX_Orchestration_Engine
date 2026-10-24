"""Which preset a test started from (run → preset provenance).

Launches record one row once their run exists, like the prompt "Used by" index.
Recording is best-effort: a failure is logged and never blocks or fails a launch.
The wizard's claim is checked: a team preset must exist and be one the launcher
may see, and its name and version come from the stored row, not the request.
"""

import asyncio
from collections.abc import Mapping
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from apex.auth.identity import ConsumerIdentity
from apex.domain.diagnostics import bounded_diagnostic, safe_type_name
from apex.domain.input_limits import NoNulStr
from apex.persistence.db import get_sessionmaker
from apex.persistence.models import RunPresetLink, TestPreset

logger = structlog.get_logger(__name__)

# The launch has already succeeded; never hold its response on the index.
RECORD_TIMEOUT_S = 2.0


class PresetProvenance(BaseModel):
    """The preset a launch started from, as the wizard reports it."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["team", "organization"]
    id: NoNulStr = Field(min_length=1, max_length=256)
    version: int | None = Field(default=None, ge=0)
    name: NoNulStr | None = Field(default=None, max_length=255)
    modified: bool = False


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _safe_name(value: str | None) -> str | None:
    """Run pages show this to everyone who can see the run; drop credential-looking text."""
    if not value or bounded_diagnostic(value, max_chars=max(1, len(value))) != value:
        return None
    return _text(value, 255)


def _team_preset_visible(identity: ConsumerIdentity, preset: TestPreset) -> bool:
    if preset.visibility == "personal" and preset.created_by_consumer_id != identity.consumer_id:
        return False
    if preset.application_id:
        return identity.allows_scope(project_id=preset.project_id, app_id=preset.application_id)
    return identity.allows_project(preset.project_id)


async def record_preset_usage(
    thread_id: str | None,
    preset: PresetProvenance | None,
    configurable: Mapping[str, Any],
    identity: ConsumerIdentity,
) -> None:
    if not thread_id or preset is None:
        return
    try:
        await asyncio.wait_for(
            _write(thread_id, preset, configurable, identity), timeout=RECORD_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 - provenance is an index; never fail a launch
        logger.warning(
            "preset_usage_record_failed", thread_id=thread_id, error_type=safe_type_name(exc)
        )


async def _write(
    thread_id: str,
    preset: PresetProvenance,
    configurable: Mapping[str, Any],
    identity: ConsumerIdentity,
) -> None:
    async with get_sessionmaker()() as session:
        if await session.get(RunPresetLink, thread_id) is not None:
            return  # an idempotent replay already recorded it
        name, version = _safe_name(preset.name), preset.version
        if preset.kind == "team":
            stored = await session.get(TestPreset, preset.id)
            if stored is None or not _team_preset_visible(identity, stored):
                logger.info("preset_usage_unknown_preset", thread_id=thread_id)
                return
            name = stored.name
            version = min(version, stored.version) if version else stored.version
        session.add(
            RunPresetLink(
                thread_id=thread_id,
                preset_kind=preset.kind,
                preset_id=preset.id,
                preset_version=version,
                preset_name=name,
                modified=preset.modified,
                project_id=_text(configurable.get("project_id"), 255),
                app_id=_text(configurable.get("app_id"), 255),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # a concurrent replay won the insert
