"""Admin compliance tooling for audit chain verification and export."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import require_role
from apex.auth.identity import Role
from apex.persistence.db import get_session
from apex.services.audit import AuditService

router = APIRouter(
    prefix="/admin/compliance",
    tags=["admin-compliance"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class AuditChainVerificationOut(BaseModel):
    ok: bool
    checked: int
    first_error: str | None = None
    last_hash: str | None = None


class AuditRetentionOut(BaseModel):
    before: datetime
    candidates: int
    preserved_anchor_id: str | None = None


class AuditPruneOut(BaseModel):
    deleted: int
    retained_anchor: bool


BeforeParam = Annotated[
    datetime | None,
    Query(description="Delete or inspect audit rows before this timestamp"),
]
RetainAnchorParam = Annotated[
    bool,
    Query(description="Keep the newest pruned-window row as a truncated-chain anchor"),
]


@router.get(
    "/audit/chain",
    operation_id="verifyAuditChain",
    response_model=AuditChainVerificationOut,
)
async def verify_audit_chain(
    session: SessionDep,
    allow_truncated: Annotated[
        bool,
        Query(description="Allow the first retained row to reference an archived prior hash"),
    ] = False,
) -> AuditChainVerificationOut:
    result = await AuditService(session).verify_chain(allow_truncated=allow_truncated)
    return AuditChainVerificationOut.model_validate(result.__dict__)


@router.get(
    "/audit/export.jsonl",
    operation_id="exportAuditJsonl",
    response_class=Response,
)
async def export_audit_jsonl(session: SessionDep) -> Response:
    body = await AuditService(session).export_jsonl()
    return Response(content=body, media_type="application/x-ndjson")


@router.get(
    "/audit/export.cef",
    operation_id="exportAuditCef",
    response_class=Response,
)
async def export_audit_cef(session: SessionDep) -> Response:
    body = await AuditService(session).export_cef()
    return Response(content=body, media_type="text/plain; charset=utf-8")


@router.get(
    "/audit/retention",
    operation_id="getAuditRetention",
    response_model=AuditRetentionOut,
)
async def get_audit_retention(
    session: SessionDep,
    before: BeforeParam = None,
    retain_anchor: RetainAnchorParam = True,
) -> AuditRetentionOut:
    if before is None:
        raise HTTPException(status_code=422, detail="before query parameter is required")
    result = await AuditService(session).retention_summary(
        before=before, retain_anchor=retain_anchor
    )
    return AuditRetentionOut.model_validate(result.__dict__)


@router.delete(
    "/audit/retention",
    operation_id="pruneAuditRetention",
    response_model=AuditPruneOut,
)
async def prune_audit_retention(
    session: SessionDep,
    before: BeforeParam = None,
    retain_anchor: RetainAnchorParam = True,
) -> AuditPruneOut:
    if before is None:
        raise HTTPException(status_code=422, detail="before query parameter is required")
    deleted = await AuditService(session).prune_before(before=before, retain_anchor=retain_anchor)
    return AuditPruneOut(deleted=deleted, retained_anchor=retain_anchor)
