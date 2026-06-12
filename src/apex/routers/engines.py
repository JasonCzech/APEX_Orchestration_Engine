"""/engines: engine-run history (projection reads) + the engine-level kill switch.

History reads serve the `engine_runs` projection rows written best-effort by the
execution phase. The table has no project column in v1, so reads are gated by
authentication only (any role); abort requires operator+. Abort forwards the
caller's API key to the loopback LangGraph API so state reads and run cancels are
scoped exactly like direct calls (same pattern as /pipelines).
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, require_role
from apex.auth.identity import Role
from apex.auth.service import extract_api_key
from apex.persistence.db import get_session
from apex.persistence.models import EngineRun
from apex.persistence.repositories.engine_runs import EngineRunsRepository
from apex.ports.execution_engine import EngineRunPhase
from apex.services.engine_abort import EngineAbortService, EngineRunNotFoundError
from apex.services.langgraph_client import loopback_client

router = APIRouter(prefix="/engines", tags=["engines"])


def get_engine_runs_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EngineRunsRepository:
    return EngineRunsRepository(session)


def get_engine_abort_service(
    request: Request,
    repo: Annotated[EngineRunsRepository, Depends(get_engine_runs_repository)],
) -> EngineAbortService:
    """Per-request service over the loopback client, forwarding the caller's key."""
    return EngineAbortService(loopback_client(extract_api_key(request.headers)), repo)


EngineRunsRepo = Annotated[EngineRunsRepository, Depends(get_engine_runs_repository)]
AbortService = Annotated[EngineAbortService, Depends(get_engine_abort_service)]
ThreadId = Annotated[str, Path(description="Pipeline thread id the engine run belongs to")]


# ── Schemas ──────────────────────────────────────────────────────────────────


class EngineRunRead(BaseModel):
    id: str
    thread_id: str
    attempt: int
    engine: str
    external_run_id: str | None
    status: str
    handle: dict[str, Any]
    summary: dict[str, Any] | None
    started_at: datetime | None
    ended_at: datetime | None


class EngineRunListResponse(BaseModel):
    items: list[EngineRunRead]
    total: int
    limit: int
    offset: int


class AbortEngineRunRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1024)


class AbortEngineRunResponse(BaseModel):
    thread_id: str
    engine: str
    external_run_id: str | None
    cancelled_runs: list[str]


def _read_model(run: EngineRun) -> EngineRunRead:
    return EngineRunRead(
        id=run.id,
        thread_id=run.thread_id,
        attempt=run.attempt,
        engine=run.engine,
        external_run_id=run.external_run_id,
        status=run.status,
        handle=run.handle or {},
        summary=run.summary,
        started_at=run.started_at,
        ended_at=run.ended_at,
    )


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/runs", operation_id="listEngineRuns", response_model=EngineRunListResponse)
async def list_engine_runs(
    identity: CurrentIdentity,
    repo: EngineRunsRepo,
    engine: Annotated[str | None, Query(description="Filter by engine provider")] = None,
    status: Annotated[EngineRunPhase | None, Query(description="Filter by run status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EngineRunListResponse:
    """Engine-run history, newest started first (projection; no project column in v1)."""
    rows, total = await repo.list_runs(
        engine=engine,
        status=status.value if status is not None else None,
        limit=limit,
        offset=offset,
    )
    return EngineRunListResponse(
        items=[_read_model(run) for run in rows], total=total, limit=limit, offset=offset
    )


@router.get("/runs/{thread_id}", operation_id="getEngineRuns", response_model=list[EngineRunRead])
async def get_engine_runs(
    thread_id: ThreadId, identity: CurrentIdentity, repo: EngineRunsRepo
) -> list[EngineRunRead]:
    """All engine-run attempts for one thread, newest attempt first ([] when none)."""
    return [_read_model(run) for run in await repo.list_for_thread(thread_id)]


@router.post(
    "/runs/{thread_id}/abort",
    operation_id="abortEngineRun",
    status_code=202,
    response_model=AbortEngineRunResponse,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def abort_engine_run(
    thread_id: ThreadId,
    service: AbortService,
    body: AbortEngineRunRequest | None = None,
) -> AbortEngineRunResponse:
    """Engine-level kill switch: abort the external run, then cancel graph runs.

    For when graph-level cancel isn't enough — the external load run keeps burning
    even after the poll loop dies. 404 when no engine handle is discoverable from
    thread state or the projection.
    """
    try:
        result = await service.abort(thread_id, reason=body.reason if body is not None else None)
    except EngineRunNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"no engine run found for thread {thread_id!r}"
        ) from None
    return AbortEngineRunResponse(
        thread_id=result.thread_id,
        engine=result.engine,
        external_run_id=result.external_run_id,
        cancelled_runs=result.cancelled_runs,
    )
