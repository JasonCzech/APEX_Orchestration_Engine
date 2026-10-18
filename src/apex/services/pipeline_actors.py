"""Bounded display names for actors already recorded on one authorized run."""

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.auth.identity import ConsumerIdentity
from apex.persistence.models import ApiConsumer
from apex.services.connection_credentials import sanitize_credential_text_for_output


class PipelineActors(BaseModel):
    names: dict[str, str]
    truncated: bool = False


async def pipeline_actor_names(
    snapshot: dict[str, Any], session: AsyncSession, identity: ConsumerIdentity
) -> PipelineActors:
    # The caller-scoped native read happens first. Check the public scope again
    # before looking up names, and never accept a caller-provided identity list.
    project_id = snapshot.get("project_id")
    app_id = snapshot.get("app_id")
    if not identity.is_unscoped and (
        not isinstance(project_id, str)
        or not identity.allows_scope(project_id=project_id, app_id=app_id)
    ):
        raise HTTPException(404, "pipeline thread not found")
    values = snapshot.get("values")
    phases = values.get("phase_results") if isinstance(values, dict) else None
    ids: dict[str, None] = {}
    if isinstance(phases, dict):
        for phase in phases.values():
            approvals = phase.get("approvals") if isinstance(phase, dict) else None
            if not isinstance(approvals, list):
                continue
            for approval in reversed(approvals):
                actor = approval.get("actor") if isinstance(approval, dict) else None
                if isinstance(actor, str) and 0 < len(actor) <= 255:
                    ids.setdefault(actor, None)
                if len(ids) > 256:
                    break
            if len(ids) > 256:
                break
    selected = list(ids)[:256]
    if not selected:
        return PipelineActors(names={})
    rows = await session.execute(
        select(ApiConsumer.id, ApiConsumer.name).where(
            ApiConsumer.id.in_(selected), ApiConsumer.deleted_at.is_(None)
        )
    )
    names = {
        row.id: name for row in rows if (name := sanitize_credential_text_for_output(row.name))
    }
    current_name = sanitize_credential_text_for_output(identity.name)
    if identity.consumer_id in selected and current_name:
        names.setdefault(identity.consumer_id, current_name)
    return PipelineActors(names=names, truncated=len(ids) > len(selected))
