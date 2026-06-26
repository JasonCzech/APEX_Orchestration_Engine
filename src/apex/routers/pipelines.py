"""/pipelines facade: dashboard-shaped reads + gate CAS resume over the loopback API.

Visibility scoping happens server-side: the loopback client forwards the caller's
API key so the LangGraph @auth.on filters apply exactly as on direct calls — this
router never re-filters threads client-side.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langgraph_sdk.errors import NotFoundError
from pydantic import BaseModel, Field

from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.app.errors import problem
from apex.auth.identity import ConsumerIdentity, Role
from apex.auth.service import extract_api_key
from apex.persistence.repositories.documents import DocumentsRepository
from apex.services.documents import get_documents_repository
from apex.services.langgraph_client import loopback_client
from apex.services.pipeline_read import (
    GateSupersededError,
    InvalidGateActionError,
    NoActiveRunError,
    PipelineReadService,
)
from apex.settings import get_settings

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

ThreadStatusFilter = Literal["idle", "busy", "interrupted", "error"]


def get_pipeline_read_service(request: Request) -> PipelineReadService:
    """Per-request service over the loopback client, forwarding the caller's key."""
    return PipelineReadService(loopback_client(extract_api_key(request.headers)))


def _pipeline_read_service_after_scope(request: Request) -> PipelineReadService:
    overrides: Any = getattr(request.app, "dependency_overrides", {})
    override = overrides.get(get_pipeline_read_service)
    if override is not None:
        return override()
    return get_pipeline_read_service(request)


PipelineService = Annotated[PipelineReadService, Depends(get_pipeline_read_service)]
DocumentsRepo = Annotated[DocumentsRepository, Depends(get_documents_repository)]


async def _documents_to_packets(
    repository: DocumentsRepository, identity: ConsumerIdentity, document_ids: list[str]
) -> list[dict[str, Any]]:
    """Resolve uploaded documents into context packets the agent can read.

    Scoped like GET /v1/documents/{id}: a document outside the caller's scope is 404,
    not leaked. The artifact key is exposed as a /v1/artifacts URL the agent (or a
    human) can dereference.
    """
    per_doc_cap = get_settings().documents.max_context_chars_per_doc
    packets: list[dict[str, Any]] = []
    for document_id in document_ids:
        document = await repository.get(document_id)
        if document is None or not (
            document.project_id is None or identity.allows_project(document.project_id)
        ):
            raise HTTPException(status_code=404, detail=f"document {document_id!r} not found")
        text = document.extracted_text or None
        if text and len(text) > per_doc_cap:
            text = text[:per_doc_cap].rstrip() + "\n\n…[truncated]"
        packets.append(
            {
                "id": f"document-{document.id}",
                "source": "document",
                "title": document.name,
                "summary": document.summary,
                "ref": f"/v1/artifacts/{document.artifact_key}",
                "text": text,
            }
        )
    return packets


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


class StartPipelineRequest(BaseModel):
    """Start a pipeline run. For results analysis, select the analysis phases (e.g.
    ["reporting", "postmortem"]) and supply `external_results`; gates default to auto."""

    title: str = Field(min_length=1, max_length=500)
    request: str = Field(default="", max_length=20000)
    project_id: str | None = None
    app_id: str | None = None
    phases: list[str] | None = None
    gates: dict[str, Any] | None = None
    agent_backend: str | None = None
    model_by_phase: dict[str, str] | None = None
    external_results: dict[str, Any] | None = None
    context_packets: list[dict[str, Any]] | None = None
    document_ids: list[str] | None = None


class StartPipelineResponse(BaseModel):
    thread_id: str
    run_id: str
    stream_url: str = Field(
        description="Join the run's SSE stream: GET this path on the LangGraph "
        "surface (same host) with your API key."
    )


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


@router.post(
    "",
    operation_id="createPipelineRun",
    status_code=202,
    response_model=StartPipelineResponse,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def create_pipeline_run(
    body: StartPipelineRequest,
    identity: CurrentIdentity,
    documents: DocumentsRepo,
    request: Request,
) -> Any:
    """Create a thread and launch a pipeline run; returns the run id + SSE stream URL.

    Convenience entrypoint for external dashboard clients: drive the existing pipeline
    (e.g. an analysis-only run over externally-supplied results) without touching the
    raw LangGraph /threads + /runs API. `document_ids` are resolved into context packets
    (scoped per consumer) and merged with any inline `context_packets`.
    """
    ensure_scope(identity, project_id=body.project_id, app_id=body.app_id)
    service = _pipeline_read_service_after_scope(request)
    context_packets = list(body.context_packets or [])
    if body.document_ids:
        context_packets += await _documents_to_packets(documents, identity, body.document_ids)
    try:
        result = await service.start_run(
            title=body.title,
            request=body.request,
            project_id=body.project_id,
            app_id=body.app_id,
            phases=body.phases,
            gates=body.gates,
            agent_backend=body.agent_backend,
            model_by_phase=body.model_by_phase,
            external_results=body.external_results,
            context_packets=context_packets or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return StartPipelineResponse(**result)


@router.get("", operation_id="listPipelines", response_model=PipelineListResponse)
async def list_pipelines(
    identity: CurrentIdentity,
    request: Request,
    project: str | None = None,
    status: ThreadStatusFilter | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    ensure_scope(identity, project_id=project)
    service = _pipeline_read_service_after_scope(request)
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
