"""Server-side new-test wizard drafts (`/drafts`).

Visibility: unscoped admins see everything; everyone else sees global drafts
(project_id NULL), drafts in their scoped projects, and drafts they created.
Out-of-scope rows answer 404 (not 403) so ids don't leak across projects.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
from apex.persistence.db import get_session
from apex.persistence.models import Draft
from apex.persistence.repositories.drafts import DraftsRepository

router = APIRouter(prefix="/drafts", tags=["drafts"])


def get_drafts_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DraftsRepository:
    return DraftsRepository(session)


DraftsRepo = Annotated[DraftsRepository, Depends(get_drafts_repository)]
OperatorIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))]
DraftId = Annotated[str, Path(description="Draft id")]


# ── Schemas ──────────────────────────────────────────────────────────────────


class DraftRead(BaseModel):
    id: str
    title: str
    project_id: str | None
    payload: dict[str, Any]
    created_by: str | None
    created_at: datetime | None
    updated_at: datetime | None


class DraftCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=1024)
    project_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class DraftUpdateRequest(BaseModel):
    """Full replace of editable fields; omitted project_id keeps legacy ownership."""

    title: str = Field(min_length=1, max_length=1024)
    project_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _read_model(draft: Draft) -> DraftRead:
    return DraftRead(
        id=draft.id,
        title=draft.title,
        project_id=draft.project_id,
        payload=draft.payload,
        created_by=draft.created_by,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


def _visible(identity: ConsumerIdentity, draft: Draft) -> bool:
    if identity.is_unscoped or draft.project_id is None:
        return True
    # Creation provenance is not an authorization grant. If an operator's
    # project scope is revoked, their old drafts must be revoked with it.
    return identity.allows_project(draft.project_id)


def _writable(identity: ConsumerIdentity, draft: Draft) -> bool:
    if draft.project_id is None:
        return identity.is_unscoped
    return identity.contains_scope(ScopeRef(project_id=draft.project_id))


def _ensure_create_scope(identity: ConsumerIdentity, project_id: str | None) -> None:
    allowed = (
        identity.is_unscoped
        if project_id is None
        else identity.contains_scope(ScopeRef(project_id=project_id))
    )
    if not allowed:
        audience = "global" if project_id is None else "project-wide"
        raise HTTPException(
            status_code=403,
            detail=f"{audience} draft creation requires matching full-audience scope",
        )


async def _visible_or_404(
    repo: DraftsRepository, identity: ConsumerIdentity, draft_id: str
) -> Draft:
    draft = await repo.get(draft_id)
    if draft is None or not _visible(identity, draft):
        raise HTTPException(status_code=404, detail=f"Draft '{draft_id}' not found")
    return draft


async def _writable_or_404(
    repo: DraftsRepository, identity: ConsumerIdentity, draft_id: str
) -> Draft:
    draft = await repo.get_for_update(draft_id)
    if draft is None or not _writable(identity, draft):
        raise HTTPException(status_code=404, detail=f"Draft '{draft_id}' not found")
    return draft


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("", operation_id="listDrafts")
async def list_drafts(
    identity: CurrentIdentity,
    repo: DraftsRepo,
    project: Annotated[str | None, Query(description="Filter to one project")] = None,
) -> list[DraftRead]:
    ensure_scope(identity, project_id=project)
    drafts = await repo.list_all(project_id=project)
    return [_read_model(draft) for draft in drafts if _visible(identity, draft)]


@router.post("", operation_id="createDraft", status_code=201)
async def create_draft(
    body: DraftCreateRequest, identity: OperatorIdentity, repo: DraftsRepo
) -> DraftRead:
    _ensure_create_scope(identity, body.project_id)
    draft = await repo.create(
        title=body.title,
        project_id=body.project_id,
        payload=body.payload,
        created_by=identity.name,
        created_by_consumer_id=identity.consumer_id,
    )
    return _read_model(draft)


@router.get("/{draft_id}", operation_id="getDraft")
async def get_draft(draft_id: DraftId, identity: CurrentIdentity, repo: DraftsRepo) -> DraftRead:
    return _read_model(await _visible_or_404(repo, identity, draft_id))


@router.put("/{draft_id}", operation_id="updateDraft")
async def update_draft(
    draft_id: DraftId, body: DraftUpdateRequest, identity: OperatorIdentity, repo: DraftsRepo
) -> DraftRead:
    current = await _writable_or_404(repo, identity, draft_id)
    project_id = body.project_id if "project_id" in body.model_fields_set else current.project_id
    if project_id != current.project_id:
        _ensure_create_scope(identity, project_id)
    updated = await repo.replace_existing(
        current,
        title=body.title,
        project_id=project_id,
        payload=body.payload,
    )
    return _read_model(updated)


@router.delete("/{draft_id}", operation_id="deleteDraft", status_code=204)
async def delete_draft(draft_id: DraftId, identity: OperatorIdentity, repo: DraftsRepo) -> None:
    draft = await _writable_or_404(repo, identity, draft_id)
    if not await repo.delete_existing(draft):
        raise HTTPException(status_code=404, detail=f"Draft '{draft_id}' not found")
