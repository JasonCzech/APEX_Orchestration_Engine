"""/engines: engine-run history (projection reads) + the engine-level kill switch.

History reads serve the `engine_runs` projection rows written best-effort by the
execution phase. Rows carry project ownership so reads and projection fallback
aborts can enforce the caller's project scopes. Abort also forwards the caller's
API key to the loopback LangGraph API so state and active-monitor reads are scoped
like direct calls.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, require_role
from apex.auth.identity import Role
from apex.auth.service import extract_api_key
from apex.domain.input_limits import MAX_DB_LIST_OFFSET, NoNulStr, ResourceId
from apex.domain.pipeline import (
    ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL,
    EngineConnectionAffinityMissingError,
)
from apex.persistence.db import get_session
from apex.persistence.models import EngineRun
from apex.persistence.repositories.engine_runs import EngineRunsRepository
from apex.ports.execution_engine import EngineRunPhase
from apex.services.engine_abort import (
    EngineAbortConfirmationPendingError,
    EngineAbortService,
    EngineGraphFinalizationPendingError,
    EngineProjectionFinalizationPendingError,
    EngineProviderAbortError,
    EngineProvisioningAbortPendingError,
    EngineRunNotFoundError,
)
from apex.services.langgraph_client import loopback_client
from apex.services.pipeline_read import ActiveRunSnapshotUnstableError, TooManyActiveRunsError
from apex.services.public_projection import public_test_result_summary

router = APIRouter(prefix="/engines", tags=["engines"])


def get_engine_runs_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EngineRunsRepository:
    return EngineRunsRepository(session)


def get_engine_abort_service(
    request: Request,
    repo: Annotated[EngineRunsRepository, Depends(get_engine_runs_repository)],
    identity: CurrentIdentity,
) -> EngineAbortService:
    """Per-request service over the loopback client, forwarding the caller's key."""
    allowed = None if identity.is_unscoped else identity.scopes
    return EngineAbortService(
        loopback_client(
            extract_api_key(request.headers),
            authorize_destructive=True,
        ),
        repo,
        allowed_scopes=allowed,
    )


EngineRunsRepo = Annotated[EngineRunsRepository, Depends(get_engine_runs_repository)]
AbortService = Annotated[EngineAbortService, Depends(get_engine_abort_service)]
ThreadId = Annotated[
    ResourceId,
    Path(description="Pipeline thread id the engine run belongs to"),
]


# ── Schemas ──────────────────────────────────────────────────────────────────


class EngineRunRead(BaseModel):
    id: str
    thread_id: str
    project_id: str | None = None
    app_id: str | None = None
    attempt: int
    engine: str
    status: str
    summary: dict[str, Any] | None
    started_at: datetime | None
    ended_at: datetime | None


class EngineRunListResponse(BaseModel):
    items: list[EngineRunRead]
    total: int
    limit: int
    offset: int


class AbortEngineRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: NoNulStr | None = Field(default=None, max_length=1024)


class AbortEngineRunResponse(BaseModel):
    thread_id: str
    engine: str
    external_run_id: str | None
    cancelled_runs: list[str]
    phase: EngineRunPhase | None = None
    confirmed: bool = False


def _read_model(run: EngineRun) -> EngineRunRead:
    return EngineRunRead(
        id=run.id,
        thread_id=run.thread_id,
        project_id=run.project_id,
        app_id=run.app_id,
        attempt=run.attempt,
        engine=run.engine,
        status=run.status,
        summary=public_test_result_summary(run.summary),
        started_at=run.started_at,
        ended_at=run.ended_at,
    )


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/runs", operation_id="listEngineRuns", response_model=EngineRunListResponse)
async def list_engine_runs(
    identity: CurrentIdentity,
    repo: EngineRunsRepo,
    engine: Annotated[
        NoNulStr | None,
        Query(max_length=64, description="Filter by engine provider"),
    ] = None,
    status: Annotated[EngineRunPhase | None, Query(description="Filter by run status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> EngineRunListResponse:
    """Engine-run history, newest started first, scoped by project ownership."""
    allowed = None if identity.is_unscoped else identity.scopes
    rows, total = await repo.list_runs(
        engine=engine,
        status=status.value if status is not None else None,
        allowed_scopes=allowed,
        limit=limit,
        offset=offset,
    )
    return EngineRunListResponse(
        items=[_read_model(run) for run in rows], total=total, limit=limit, offset=offset
    )


@router.get("/runs/{thread_id}", operation_id="getEngineRuns", response_model=list[EngineRunRead])
async def get_engine_runs(
    thread_id: ThreadId,
    identity: CurrentIdentity,
    repo: EngineRunsRepo,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> list[EngineRunRead]:
    """One bounded page of thread attempts, newest first ([] when none)."""
    allowed = None if identity.is_unscoped else identity.scopes
    return [
        _read_model(run)
        for run in await repo.list_for_thread(
            thread_id,
            allowed_scopes=allowed,
            limit=limit,
            offset=offset,
        )
    ]


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
    """Engine-level kill switch that preserves the graph's finalization monitor.

    For when graph-level cancel isn't enough — the external load run keeps burning
    even after the poll loop dies. An active monitor is left alive to collect results
    and checkpoint the terminal execution state. 404 when no engine handle is
    discoverable from thread state or the projection.
    """
    try:
        result = await service.abort(thread_id, reason=body.reason if body is not None else None)
    except EngineRunNotFoundError:
        raise HTTPException(status_code=404, detail="engine run not found") from None
    except EngineConnectionAffinityMissingError:
        raise HTTPException(
            status_code=409,
            detail=ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL,
        ) from None
    except EngineAbortConfirmationPendingError:
        raise HTTPException(
            status_code=503,
            detail="external engine is still stopping; retry abort to confirm termination",
            headers={"Retry-After": "1"},
        ) from None
    except EngineProvisioningAbortPendingError:
        raise HTTPException(
            status_code=503,
            detail="engine provisioning is still establishing an abort handle; retry abort",
            headers={"Retry-After": "1"},
        ) from None
    except EngineGraphFinalizationPendingError:
        raise HTTPException(
            status_code=503,
            detail=(
                "external engine stopped but graph finalization is pending recovery; "
                "resume the pipeline"
            ),
            headers={"Retry-After": "1"},
        ) from None
    except EngineProjectionFinalizationPendingError:
        raise HTTPException(
            status_code=503,
            detail=(
                "external engine stopped but provider cleanup or durable projection is "
                "pending; retry abort"
            ),
            headers={"Retry-After": "1"},
        ) from None
    except EngineProviderAbortError:
        raise HTTPException(
            status_code=502,
            detail="engine provider abort failed",
        ) from None
    except ActiveRunSnapshotUnstableError:
        raise HTTPException(
            status_code=409,
            detail="active graph runs changed throughout the abort snapshot; retry",
        ) from None
    except TooManyActiveRunsError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"thread {thread_id!r} exceeds the bounded abort limit ({exc.limit}); "
                "use the operator cleanup runbook"
            ),
        ) from None
    return AbortEngineRunResponse(
        thread_id=result.thread_id,
        engine=result.engine,
        external_run_id=result.external_run_id,
        cancelled_runs=result.cancelled_runs,
        phase=EngineRunPhase(result.phase) if result.phase is not None else None,
        confirmed=result.confirmed,
    )
