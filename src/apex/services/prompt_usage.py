"""Which tests pinned which prompt versions: the catalog's "Used by".

Launches record one row per pinned slot once their run exists. Recording is
best-effort: a failure is logged and never blocks or fails the launch.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from apex.auth.identity import ConsumerIdentity
from apex.domain.diagnostics import safe_type_name
from apex.domain.prompt_selection import PromptManifestEntry, PromptManifestV1
from apex.persistence.db import get_sessionmaker
from apex.persistence.models import PromptUsage
from apex.services.analytics_scope import analytics_scope_filter

logger = structlog.get_logger(__name__)

MAX_TITLE_CHARS = 500
# The launch has already succeeded; never hold its response on the index.
RECORD_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class PromptTestUsage:
    thread_id: str
    title: str | None
    version: int
    slot: str
    recorded_at: datetime | None


def manifest_slots(manifest: PromptManifestV1) -> list[tuple[str, PromptManifestEntry]]:
    slots: list[tuple[str, PromptManifestEntry]] = [("system", manifest.system)]
    slots += [(f"phase:{phase.value}", entry) for phase, entry in manifest.phases.items()]
    slots += [("application", entry) for entry in manifest.application]
    slots += [("environment", entry) for entry in manifest.environment]
    return slots


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


async def record_prompt_usage(
    thread_id: str | None, configurable: Mapping[str, Any], *, title: Any = None
) -> None:
    raw = configurable.get("prompt_manifest")
    if not thread_id or raw is None:
        return
    try:
        manifest = (
            raw if isinstance(raw, PromptManifestV1) else PromptManifestV1.model_validate(raw)
        )
        pinned = {(entry.prompt_id, slot): entry for slot, entry in manifest_slots(manifest)}
        await asyncio.wait_for(
            _write_usage(thread_id, pinned, configurable, title), timeout=RECORD_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 - usage is an index; never fail a launch
        logger.warning(
            "prompt_usage_record_failed", thread_id=thread_id, error_type=safe_type_name(exc)
        )


async def _write_usage(
    thread_id: str,
    pinned: Mapping[tuple[str, str], PromptManifestEntry],
    configurable: Mapping[str, Any],
    title: Any,
) -> None:
    # A concurrent recorder can win the insert race; the retry sees its rows.
    for attempt in range(2):
        try:
            async with get_sessionmaker()() as session:
                recorded = set(
                    (
                        await session.execute(
                            select(PromptUsage.prompt_id, PromptUsage.slot).where(
                                PromptUsage.thread_id == thread_id
                            )
                        )
                    ).tuples()
                )
                for (prompt_id, slot), entry in pinned.items():
                    if (prompt_id, slot) in recorded:
                        continue
                    session.add(
                        PromptUsage(
                            thread_id=thread_id,
                            prompt_id=prompt_id,
                            slot=slot,
                            version_id=entry.version_id,
                            version=entry.version,
                            project_id=_text(configurable.get("project_id"), 255),
                            app_id=_text(configurable.get("app_id"), 255),
                            title=_text(title, MAX_TITLE_CHARS),
                        )
                    )
                await session.commit()
            return
        except IntegrityError:
            if attempt:
                raise


async def list_prompt_usage(
    prompt_id: str, identity: ConsumerIdentity, *, limit: int
) -> tuple[int, list[PromptTestUsage]]:
    """Return (visible test count, most recent visible tests).

    Visibility is the same as the run lists': app-only scopes need the row's app,
    and a combined run is hidden unless every participant is visible.
    """

    visible = analytics_scope_filter(
        PromptUsage, None if identity.is_unscoped else tuple(identity.scopes)
    )
    conditions = [PromptUsage.prompt_id == prompt_id]
    if visible is not None:
        conditions.append(visible)
    async with get_sessionmaker()() as session:
        total = await session.scalar(
            select(func.count(func.distinct(PromptUsage.thread_id))).where(*conditions)
        )
        # One test can pin a prompt in more than one slot; read enough rows to fill
        # `limit` distinct tests and keep the first slot of each.
        rows = (
            await session.scalars(
                select(PromptUsage)
                .where(*conditions)
                .order_by(PromptUsage.recorded_at.desc(), PromptUsage.thread_id, PromptUsage.slot)
                .limit(limit * 8)
            )
        ).all()
    tests: dict[str, PromptTestUsage] = {}
    for row in rows:
        if row.thread_id not in tests:
            tests[row.thread_id] = PromptTestUsage(
                thread_id=row.thread_id,
                title=row.title,
                version=row.version,
                slot=row.slot,
                recorded_at=row.recorded_at,
            )
    return int(total or 0), list(tests.values())[:limit]
