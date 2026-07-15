"""Admin compliance tooling for audit chain verification and export."""

from datetime import datetime
from threading import Lock
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.persistence.db import get_session
from apex.services.audit import AuditService, validate_retention_cutoff

AdminIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.ADMIN))]


async def require_unscoped_admin(identity: AdminIdentity) -> ConsumerIdentity:
    """Compliance exports and retention are global until audit rows carry ownership."""

    if not identity.is_unscoped:
        raise HTTPException(status_code=403, detail="Compliance access requires platform admin")
    return identity


router = APIRouter(
    prefix="/admin/compliance",
    tags=["admin-compliance"],
    dependencies=[Depends(require_unscoped_admin)],
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
MAX_CONCURRENT_AUDIT_EXPORTS = 4


class _AuditExportLease:
    def __init__(self, limiter: "_AuditExportLimiter") -> None:
        self._limiter = limiter
        self._released = False
        self._lock = Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._limiter.release()


class _AuditExportLimiter:
    """Small per-process cap for slow, global compliance downloads."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("audit export capacity must be positive")
        self._capacity = capacity
        self._active = 0
        self._lock = Lock()

    def try_acquire(self) -> _AuditExportLease | None:
        with self._lock:
            if self._active >= self._capacity:
                return None
            self._active += 1
        return _AuditExportLease(self)

    def release(self) -> None:
        with self._lock:
            if self._active < 1:
                raise RuntimeError("audit export lease released without acquisition")
            self._active -= 1


class _LeasedStreamingResponse(StreamingResponse):
    """Release admission even if header/body sending fails before iteration."""

    def __init__(self, *args, lease: _AuditExportLease, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self._lease = lease

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.release()


_audit_export_limiter = _AuditExportLimiter(MAX_CONCURRENT_AUDIT_EXPORTS)


def _acquire_audit_export_lease() -> _AuditExportLease:
    lease = _audit_export_limiter.try_acquire()
    if lease is None:
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent audit exports",
            headers={"Retry-After": "5"},
        )
    return lease


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


def _validate_before(before: datetime | None) -> datetime:
    if before is None:
        raise HTTPException(status_code=422, detail="before query parameter is required")
    try:
        validate_retention_cutoff(before)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid audit retention cutoff") from exc
    return before


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
    response_class=StreamingResponse,
)
async def export_audit_jsonl(session: SessionDep) -> StreamingResponse:
    lease = _acquire_audit_export_lease()
    service = AuditService(session)

    async def body():
        async for line in service.iter_jsonl():
            yield line + "\n"

    try:
        return _LeasedStreamingResponse(
            body(),
            media_type="application/x-ndjson",
            lease=lease,
        )
    except BaseException:
        lease.release()
        raise


@router.get(
    "/audit/export.cef",
    operation_id="exportAuditCef",
    response_class=StreamingResponse,
)
async def export_audit_cef(session: SessionDep) -> StreamingResponse:
    lease = _acquire_audit_export_lease()
    service = AuditService(session)

    async def body():
        async for line in service.iter_cef():
            yield line + "\n"

    try:
        return _LeasedStreamingResponse(
            body(),
            media_type="text/plain; charset=utf-8",
            lease=lease,
        )
    except BaseException:
        lease.release()
        raise


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
    before = _validate_before(before)
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
    before = _validate_before(before)
    deleted = await AuditService(session).prune_before(before=before, retain_anchor=retain_anchor)
    return AuditPruneOut(deleted=deleted, retained_anchor=retain_anchor)
