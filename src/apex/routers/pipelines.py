"""/pipelines facade: dashboard-shaped reads + gate CAS resume over the loopback API.

Visibility scoping happens server-side: the loopback client forwards the caller's
API key so the LangGraph @auth.on filters apply exactly as on direct calls — this
router never re-filters threads client-side.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langgraph_sdk import Auth
from langgraph_sdk.errors import NotFoundError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.app.errors import problem
from apex.auth.handlers import ensure_thread_scope
from apex.auth.identity import ConsumerIdentity, Role
from apex.auth.service import extract_api_key
from apex.domain.pipeline import PHASE_ORDER, ContextPacket, ExternalResults
from apex.persistence.repositories.catalog import CatalogRepository
from apex.persistence.repositories.documents import DocumentsRepository
from apex.routers.catalog import get_catalog_repository
from apex.routers.engines import AbortService
from apex.services.documents import (
    DocumentContextNotFoundError,
    get_documents_repository,
    uploaded_document_context_packets,
)
from apex.services.engine_abort import EngineRunNotFoundError
from apex.services.environments import (
    EnvironmentTargetNotFoundError,
    resolve_environment_target,
)
from apex.services.langgraph_client import loopback_client
from apex.services.pipeline_read import (
    GateSupersededError,
    InvalidGateActionError,
    NoActiveRunError,
    PipelineReadService,
)
from apex.services.run_validation import (
    MAX_GATE_STRING_CHARS_HARD,
    MAX_PROMPT_PART_CHARS_HARD,
)

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

ThreadStatusFilter = Literal["idle", "busy", "interrupted", "error"]


def get_pipeline_read_service(request: Request) -> PipelineReadService:
    """Scoped loopback service allowed to perform facade-owned run cancellation."""
    return PipelineReadService(
        loopback_client(
            extract_api_key(request.headers),
            authorize_destructive=True,
        )
    )


def _pipeline_read_service_after_scope(request: Request) -> PipelineReadService:
    overrides: Any = getattr(request.app, "dependency_overrides", {})
    override = overrides.get(get_pipeline_read_service)
    if override is not None:
        return override()
    return get_pipeline_read_service(request)


PipelineService = Annotated[PipelineReadService, Depends(get_pipeline_read_service)]
DocumentsRepo = Annotated[DocumentsRepository, Depends(get_documents_repository)]
CatalogRepo = Annotated[CatalogRepository, Depends(get_catalog_repository)]


async def _documents_to_packets(
    repository: DocumentsRepository, identity: ConsumerIdentity, document_ids: list[str]
) -> list[dict[str, Any]]:
    """HTTP mapping wrapper around the shared uploaded-document evidence service."""

    try:
        return await uploaded_document_context_packets(repository, identity, document_ids)
    except DocumentContextNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _resolve_pipeline_scope(
    identity: ConsumerIdentity,
    *,
    project_id: str | None,
    app_id: str | None,
    catalog: CatalogRepository,
) -> tuple[str | None, str | None]:
    """Resolve the same project/app ownership LangGraph stamps on a thread.

    Doing this before thread creation also puts the effective ownership into the
    graph configurable, so connection selection and projections cannot silently
    fall back to global scope.
    """

    if app_id is not None and project_id is None:
        raise HTTPException(status_code=422, detail="project_id is required when app_id is set")
    metadata = {
        key: value
        for key, value in (("project_id", project_id), ("app_id", app_id))
        if value is not None
    }
    try:
        ensure_thread_scope(identity, metadata, action="pipelines.create")
    except Auth.exceptions.HTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    resolved_project = metadata.get("project_id")
    resolved_app = metadata.get("app_id")
    if resolved_app is not None:
        get_application = getattr(catalog, "get_application", None)
        if get_application is not None:
            application = await get_application(resolved_app)
            if application is None or application.archived_at is not None:
                raise HTTPException(status_code=404, detail="application not found")
            if resolved_project is not None and application.project_id != resolved_project:
                raise HTTPException(
                    status_code=403,
                    detail="application is outside the requested project",
                )
            resolved_project = application.project_id
            metadata["project_id"] = resolved_project
    return resolved_project, resolved_app


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
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    request: str = Field(default="", max_length=20000)
    assistant_id: str | None = Field(
        default=None,
        min_length=1,
        description="Golden assistant id; defaults to the base pipeline graph.",
    )
    project_id: str | None = None
    app_id: str | None = None
    configurable: dict[str, Any] | None = Field(
        default=None,
        max_length=64,
        description="Full per-run PipelineConfigurable layer retained by the selected assistant.",
    )
    phases: list[str] | None = Field(default=None, max_length=len(PHASE_ORDER))
    gates: dict[str, Any] | None = Field(default=None, max_length=len(PHASE_ORDER))
    agent_backend: str | None = Field(default=None, max_length=32)
    model_by_phase: dict[str, str] | None = Field(default=None, max_length=len(PHASE_ORDER))
    external_results: ExternalResults | None = None
    context_packets: list[ContextPacket] | None = Field(default=None, max_length=64)
    document_ids: list[str] | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_document_ids(self) -> "StartPipelineRequest":
        if self.document_ids:
            if any(
                not document_id.strip() or len(document_id) > 256
                for document_id in self.document_ids
            ):
                raise ValueError("document_ids entries must be 1-256 characters")
            if len(set(self.document_ids)) != len(self.document_ids):
                raise ValueError("document_ids must not contain duplicates")
        return self


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


class GatePromptEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: str | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    user: str | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    application: str | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)


class ResumeGateRequest(BaseModel):
    action: str = Field(min_length=1, max_length=32)
    prompt: GatePromptEdit | None = None
    instructions: str | None = Field(default=None, max_length=MAX_GATE_STRING_CHARS_HARD)
    message: str | None = Field(default=None, max_length=MAX_GATE_STRING_CHARS_HARD)
    note: str | None = Field(default=None, max_length=MAX_GATE_STRING_CHARS_HARD)


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
    system: str = Field(max_length=MAX_PROMPT_PART_CHARS_HARD)
    phase_prompt: str = Field(max_length=MAX_PROMPT_PART_CHARS_HARD)
    application: str | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    additional_context: str = Field(default="", max_length=MAX_GATE_STRING_CHARS_HARD)


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
    catalog: CatalogRepo,
    request: Request,
) -> Any:
    """Create a thread and launch a pipeline run; returns the run id + SSE stream URL.

    Convenience entrypoint for external dashboard clients: drive the existing pipeline
    (e.g. an analysis-only run over externally-supplied results) without touching the
    raw LangGraph /threads + /runs API. `document_ids` are resolved into context packets
    (scoped per consumer) and merged with any inline `context_packets`.
    """
    run_configurable = dict(body.configurable or {})
    configured_project = run_configurable.get("project_id")
    configured_app = run_configurable.get("app_id")
    if configured_project is not None and not isinstance(configured_project, str):
        raise HTTPException(status_code=422, detail="configurable.project_id must be a string")
    if configured_app is not None and not isinstance(configured_app, str):
        raise HTTPException(status_code=422, detail="configurable.app_id must be a string")
    if body.project_id and configured_project and body.project_id != configured_project:
        raise HTTPException(
            status_code=422,
            detail="project_id conflicts with configurable.project_id",
        )
    if body.app_id and configured_app and body.app_id != configured_app:
        raise HTTPException(status_code=422, detail="app_id conflicts with configurable.app_id")
    project_id, app_id = await _resolve_pipeline_scope(
        identity,
        project_id=body.project_id or configured_project,
        app_id=body.app_id or configured_app,
        catalog=catalog,
    )
    load_test = run_configurable.get("load_test")
    if isinstance(load_test, dict) and "target_environment" in load_test:
        raise HTTPException(
            status_code=422,
            detail=(
                "load_test.target_environment cannot be supplied directly; select environment_id"
            ),
        )
    if isinstance(load_test, dict):
        script_refs = load_test.get("script_refs")
        if isinstance(script_refs, list) and any(
            isinstance(ref, str) and ref.lstrip().startswith("{") for ref in script_refs
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "inline load_test.script_refs are not allowed; select an approved environment"
                ),
            )
    environment_id = run_configurable.get("environment_id")
    # Environment targets are server-owned; never honor values inherited from a
    # client-side golden bundle when the current scope omits the environment.
    run_configurable.pop("environment_target", None)
    run_configurable.pop("environment_target_version", None)
    if environment_id is not None:
        if not isinstance(environment_id, str) or not environment_id.strip():
            raise HTTPException(
                status_code=422, detail="configurable.environment_id must be a non-empty string"
            )
        try:
            target = await resolve_environment_target(
                catalog,
                environment_id.strip(),
                project_id=project_id,
                app_id=app_id,
            )
        except EnvironmentTargetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        run_configurable["environment_id"] = environment_id.strip()
        run_configurable["environment_target"] = target.base_url
        run_configurable["environment_target_version"] = target.version
    service = _pipeline_read_service_after_scope(request)
    context_packets = [packet.model_dump(mode="json") for packet in (body.context_packets or [])]
    if body.document_ids:
        context_packets += await _documents_to_packets(documents, identity, body.document_ids)
    try:
        result = await service.start_run(
            title=body.title,
            idempotency_key=body.idempotency_key,
            request=body.request,
            assistant_id=body.assistant_id,
            project_id=project_id,
            app_id=app_id,
            configurable=run_configurable,
            phases=body.phases,
            gates=body.gates,
            agent_backend=body.agent_backend,
            model_by_phase=body.model_by_phase,
            external_results=(
                body.external_results.model_dump(mode="json") if body.external_results else None
            ),
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
                "prompt": body.prompt.model_dump(mode="json") if body.prompt else None,
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
async def abort_pipeline(
    thread_id: str,
    service: PipelineService,
    engine_abort: AbortService,
) -> Any:
    """Stop an external engine first, then cancel the thread's active runs.

    Threads that have not entered execution have no engine handle and fall back
    to graph-only cancellation. Engine abort failures propagate so the API never
    reports success while production load is still running.
    """
    try:
        try:
            result = await engine_abort.abort(thread_id)
            cancelled = result.cancelled_runs
        except EngineRunNotFoundError:
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
