"""/pipelines facade: dashboard-shaped reads + gate CAS resume over the loopback API.

Visibility scoping happens server-side: the loopback client forwards the caller's
API key so the LangGraph @auth.on filters apply exactly as on direct calls — this
router never re-filters threads client-side.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langgraph_sdk.errors import NotFoundError
from pydantic import BaseModel, Field

from apex.app.dependencies import CurrentIdentity, require_role
from apex.app.errors import problem
from apex.auth.identity import Role
from apex.auth.service import extract_api_key
from apex.services.langgraph_client import loopback_client
from apex.services.pipeline_read import (
    GateSupersededError,
    InvalidGateActionError,
    NoActiveRunError,
    PipelineReadService,
)

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

ThreadStatusFilter = Literal["idle", "busy", "interrupted", "error"]


def get_pipeline_read_service(request: Request) -> PipelineReadService:
    """Per-request service over the loopback client, forwarding the caller's key."""
    return PipelineReadService(loopback_client(extract_api_key(request.headers)))


PipelineService = Annotated[PipelineReadService, Depends(get_pipeline_read_service)]


# ── Schemas ──────────────────────────────────────────────────────────────────


class PhaseStripEntry(BaseModel):
    phase: str
    status: str
    attempt: int | None = None


class PendingGate(BaseModel):
    interrupt_id: str | None = None
    kind: str | None = None
    phase: str | None = None


class PipelineEngineInfo(BaseModel):
    engine: str | None = None
    external_run_id: str | None = None


class PipelineSummary(BaseModel):
    thread_id: str
    title: str | None = None
    project_id: str | None = None
    app_id: str | None = None
    thread_status: str | None = None
    current_phase: str | None = None
    phase_strip: list[PhaseStripEntry]
    engine: PipelineEngineInfo | None = None
    created_at: str | None = None
    updated_at: str | None = None
    pending_gate: PendingGate | None = None


class PipelineListResponse(BaseModel):
    items: list[PipelineSummary]
    limit: int
    offset: int


class GateInterrupt(BaseModel):
    interrupt_id: str | None = None
    kind: str | None = None
    phase: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class PipelineDetail(PipelineSummary):
    values: dict[str, Any] = Field(default_factory=dict)
    interrupts: list[GateInterrupt] = Field(default_factory=list)


class ResumeGateRequest(BaseModel):
    action: str
    prompt: dict[str, Any] | None = None
    instructions: str | None = None
    message: str | None = None
    note: str | None = None


class ResumeGateResponse(BaseModel):
    run_id: str


class AbortPipelineResponse(BaseModel):
    cancelled_run_ids: list[str]


class PhasePromptReview(BaseModel):
    system: str
    phase_prompt: str
    application: str | None = None
    additional_context: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    updated_at: str
    updated_by: str


class PhasePromptReviewUpdate(BaseModel):
    system: str
    phase_prompt: str
    application: str | None = None
    additional_context: str = ""


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("", operation_id="listPipelines", response_model=PipelineListResponse)
async def list_pipelines(
    identity: CurrentIdentity,
    service: PipelineService,
    project: str | None = None,
    status: ThreadStatusFilter | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    items = await service.list_pipelines(
        project=project, status=status, q=q, limit=limit, offset=offset
    )
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{thread_id}", operation_id="getPipeline", response_model=PipelineDetail)
async def get_pipeline(thread_id: str, identity: CurrentIdentity, service: PipelineService) -> Any:
    try:
        return await service.get_pipeline(thread_id)
    except NotFoundError:
        raise HTTPException(
            status_code=404, detail=f"pipeline thread {thread_id!r} not found"
        ) from None


@router.get(
    "/{thread_id}/phases/{phase}/prompt-review",
    operation_id="getPhasePromptReview",
    response_model=PhasePromptReview,
)
async def get_phase_prompt_review(
    thread_id: str,
    phase: str,
    identity: CurrentIdentity,
    service: PipelineService,
) -> Any:
    try:
        return await service.get_phase_prompt_review(thread_id, phase)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NotFoundError:
        raise HTTPException(
            status_code=404, detail=f"pipeline thread {thread_id!r} not found"
        ) from None


@router.patch(
    "/{thread_id}/phases/{phase}/prompt-review",
    operation_id="patchPhasePromptReview",
    response_model=PhasePromptReview,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def patch_phase_prompt_review(
    thread_id: str,
    phase: str,
    body: PhasePromptReviewUpdate,
    identity: CurrentIdentity,
    service: PipelineService,
) -> Any:
    try:
        return await service.update_phase_prompt_review(
            thread_id,
            phase,
            body.model_dump(mode="json"),
            actor=identity.name or identity.consumer_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NotFoundError:
        raise HTTPException(
            status_code=404, detail=f"pipeline thread {thread_id!r} not found"
        ) from None


@router.post(
    "/{thread_id}/gates/{interrupt_id}/resume",
    operation_id="resumeGate",
    status_code=202,
    response_model=ResumeGateResponse,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def resume_gate(
    thread_id: str,
    interrupt_id: str,
    body: ResumeGateRequest,
    service: PipelineService,
) -> Any:
    """Compare-and-set resume: 409 gate_superseded when the interrupt is stale."""
    try:
        run_id = await service.resume_gate(
            thread_id,
            interrupt_id,
            body.action,
            {
                "prompt": body.prompt,
                "instructions": body.instructions,
                "message": body.message,
                "note": body.note,
            },
        )
    except NotFoundError:
        raise HTTPException(
            status_code=404, detail=f"pipeline thread {thread_id!r} not found"
        ) from None
    except GateSupersededError as exc:
        return problem(
            409,
            "gate_superseded",
            detail=(
                f"interrupt {interrupt_id!r} is no longer pending on thread {thread_id!r}; "
                "re-fetch the pipeline and present the current gate"
            ),
            extra={"pending_gate": exc.pending_gate},
        )
    except InvalidGateActionError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"action {exc.action!r} not allowed for this gate; "
            f"allowed: {sorted(exc.allowed)}",
        ) from exc
    return ResumeGateResponse(run_id=run_id)


@router.post(
    "/{thread_id}/abort",
    operation_id="abortPipeline",
    status_code=202,
    response_model=AbortPipelineResponse,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def abort_pipeline(thread_id: str, service: PipelineService) -> Any:
    """Cancel the thread's active run(s) via the loopback API (engine abort lands M3)."""
    try:
        cancelled = await service.abort_pipeline(thread_id)
    except NotFoundError:
        raise HTTPException(
            status_code=404, detail=f"pipeline thread {thread_id!r} not found"
        ) from None
    except NoActiveRunError:
        return problem(
            409,
            "no_active_run",
            detail=f"thread {thread_id!r} has no pending or running run to abort",
        )
    return AbortPipelineResponse(cancelled_run_ids=cancelled)
