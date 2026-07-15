"""/pipelines facade: dashboard-shaped reads + gate CAS resume over the loopback API.

Visibility scoping happens server-side: the loopback client forwards the caller's
API key so the LangGraph @auth.on filters apply exactly as on direct calls — this
router never re-filters threads client-side.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from langgraph_sdk import Auth
from langgraph_sdk.errors import ConflictError, NotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.app.errors import problem
from apex.auth.handlers import ensure_thread_scope
from apex.auth.identity import ConsumerIdentity, Role
from apex.auth.service import extract_api_key
from apex.domain.input_limits import (
    MAX_DB_LIST_OFFSET,
    NoNulStr,
    ResourceId,
    ScopeId,
    validate_json_object,
)
from apex.domain.pipeline import (
    ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL,
    PHASE_ORDER,
    ContextPacket,
    EngineConnectionAffinityMissingError,
    ExternalResults,
)
from apex.persistence.db import release_read_transactions
from apex.persistence.repositories.catalog import CatalogRepository
from apex.persistence.repositories.documents import DocumentsRepository
from apex.routers.catalog import get_catalog_repository
from apex.routers.engines import AbortService
from apex.services.documents import (
    DocumentContextNotFoundError,
    get_documents_repository,
    uploaded_document_context_packets,
)
from apex.services.engine_abort import (
    EngineAbortConfirmationPendingError,
    EngineGraphFinalizationPendingError,
    EngineProvisioningAbortPendingError,
    EngineRunNotFoundError,
)
from apex.services.environments import (
    EnvironmentTargetNotFoundError,
    resolve_environment_target,
)
from apex.services.langgraph_client import loopback_client
from apex.services.pipeline_read import (
    MAX_PIPELINE_QUERY_CHARS,
    ActiveRunSnapshotUnstableError,
    GateSupersededError,
    InvalidGateActionError,
    LaunchIdempotencyConflictError,
    NoActiveRunError,
    PipelineReadService,
    PromptReviewConflictError,
    RerunConfigurationConflictError,
    RerunIdempotencyConflictError,
    TooManyActiveRunsError,
)
from apex.services.run_validation import (
    MAX_GATE_STRING_CHARS_HARD,
    MAX_MODEL_NAME_CHARS,
    MAX_PROMPT_PART_CHARS_HARD,
)

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

ThreadStatusFilter = Literal["idle", "busy", "interrupted", "error"]
DocumentIdInput = Annotated[NoNulStr, Field(min_length=1, max_length=256)]
PhaseInput = Annotated[NoNulStr, Field(min_length=1, max_length=64)]
ModelNameInput = Annotated[
    NoNulStr,
    Field(min_length=1, max_length=MAX_MODEL_NAME_CHARS),
]
ThreadId = Annotated[ResourceId, Path(description="Pipeline thread id")]
InterruptId = Annotated[ResourceId, Path(description="Pending interrupt id")]
PhaseParam = Annotated[NoNulStr, Path(min_length=1, max_length=64)]


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
        raise HTTPException(status_code=404, detail="document context not found") from exc


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
        raise HTTPException(
            status_code=exc.status_code,
            detail="pipeline scope is not authorized",
        ) from exc
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

    model_config = ConfigDict(extra="forbid")

    title: NoNulStr = Field(min_length=1, max_length=500)
    idempotency_key: NoNulStr | None = Field(default=None, min_length=1, max_length=128)
    request: NoNulStr = Field(default="", max_length=20000)
    assistant_id: NoNulStr | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Golden assistant id; defaults to the base pipeline graph.",
    )
    project_id: ScopeId | None = None
    app_id: ScopeId | None = None
    configurable: dict[str, Any] | None = Field(
        default=None,
        max_length=64,
        description="Full per-run PipelineConfigurable layer retained by the selected assistant.",
    )
    phases: list[PhaseInput] | None = Field(default=None, max_length=len(PHASE_ORDER))
    gates: dict[str, Any] | None = Field(default=None, max_length=len(PHASE_ORDER))
    agent_backend: NoNulStr | None = Field(default=None, max_length=32)
    model_by_phase: dict[str, ModelNameInput] | None = Field(
        default=None,
        max_length=len(PHASE_ORDER),
    )
    external_results: ExternalResults | None = None
    context_packets: list[ContextPacket] | None = Field(default=None, max_length=64)
    document_ids: list[DocumentIdInput] | None = Field(default=None, max_length=64)

    @field_validator("configurable")
    @classmethod
    def validate_configurable(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return validate_json_object(value, label="pipeline configurable")

    @field_validator("gates")
    @classmethod
    def validate_gates(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return validate_json_object(value, label="pipeline gates")

    @field_validator("model_by_phase")
    @classmethod
    def validate_models(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        validate_json_object(value, label="pipeline model_by_phase")
        known = {phase.value for phase in PHASE_ORDER}
        if any(name not in known for name in value):
            raise ValueError("model_by_phase keys must name known pipeline phases")
        return value

    @field_validator("phases")
    @classmethod
    def validate_phases(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        known = {phase.value for phase in PHASE_ORDER}
        if any(name not in known for name in value):
            raise ValueError("phases entries must name known pipeline phases")
        if len(set(value)) != len(value):
            raise ValueError("phases must not contain duplicates")
        return value

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

    system: NoNulStr | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    user: NoNulStr | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    application: NoNulStr | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)


class ResumeGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: NoNulStr = Field(min_length=1, max_length=32)
    prompt: GatePromptEdit | None = None
    instructions: NoNulStr | None = Field(default=None, max_length=MAX_GATE_STRING_CHARS_HARD)
    message: NoNulStr | None = Field(default=None, max_length=MAX_GATE_STRING_CHARS_HARD)
    note: NoNulStr | None = Field(default=None, max_length=MAX_GATE_STRING_CHARS_HARD)


class ResumeGateResponse(BaseModel):
    run_id: str


class RerunPipelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phases: list[PhaseInput] = Field(min_length=1, max_length=len(PHASE_ORDER))
    gates_mode: Literal["inherit", "gated", "auto"] = "inherit"
    idempotency_key: NoNulStr = Field(min_length=1, max_length=128)

    @field_validator("phases")
    @classmethod
    def validate_rerun_phases(cls, value: list[str]) -> list[str]:
        known = {phase.value for phase in PHASE_ORDER}
        if any(name not in known for name in value):
            raise ValueError("phases entries must name known pipeline phases")
        if len(set(value)) != len(value):
            raise ValueError("phases must not contain duplicates")
        return value


class RerunPipelineResponse(BaseModel):
    run_id: str


class AbortPipelineResponse(BaseModel):
    cancelled_run_ids: list[str]
    phase: str | None = None
    confirmed: bool = False


class PhasePromptReview(BaseModel):
    system: str
    phase_prompt: str
    application: str | None = None
    additional_context: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    updated_at: str
    updated_by: str


class PhasePromptReviewUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: NoNulStr = Field(max_length=MAX_PROMPT_PART_CHARS_HARD)
    phase_prompt: NoNulStr = Field(max_length=MAX_PROMPT_PART_CHARS_HARD)
    application: NoNulStr | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    additional_context: NoNulStr = Field(default="", max_length=MAX_GATE_STRING_CHARS_HARD)


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
        forbidden_selectors = {"script_refs", "test_id", "test_instance_id"}.intersection(load_test)
        if forbidden_selectors:
            raise HTTPException(
                status_code=422,
                detail=(
                    "provider workload selectors are connection/catalog-owned and cannot be "
                    f"overridden per run: {', '.join(sorted(forbidden_selectors))}"
                ),
            )
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
            raise HTTPException(status_code=404, detail="environment target not found") from exc
        if app_id is not None and target.app_id != app_id:
            # The resolver already enforces this; retain the invariant at the
            # stamping boundary for injected/internal repository implementations.
            raise HTTPException(status_code=404, detail="environment target not found")
        project_id = target.project_id
        app_id = target.app_id
        run_configurable["project_id"] = target.project_id
        run_configurable["app_id"] = target.app_id
        run_configurable["environment_id"] = environment_id.strip()
        run_configurable["environment_target"] = target.base_url
        run_configurable["environment_target_version"] = target.version
    service = _pipeline_read_service_after_scope(request)
    context_packets = [packet.model_dump(mode="json") for packet in (body.context_packets or [])]
    if body.document_ids:
        context_packets += await _documents_to_packets(documents, identity, body.document_ids)
    await release_read_transactions(catalog, documents)
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
            principal_id=identity.consumer_id,
        )
    except LaunchIdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail="pipeline launch conflict") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid pipeline configuration") from exc
    return StartPipelineResponse(**result)


@router.get("", operation_id="listPipelines", response_model=PipelineListResponse)
async def list_pipelines(
    identity: CurrentIdentity,
    request: Request,
    project: Annotated[ScopeId | None, Query()] = None,
    status: ThreadStatusFilter | None = None,
    q: Annotated[NoNulStr | None, Query(max_length=MAX_PIPELINE_QUERY_CHARS)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> Any:
    ensure_scope(identity, project_id=project)
    service = _pipeline_read_service_after_scope(request)
    items = await service.list_pipelines(
        project=project, status=status, q=q, limit=limit, offset=offset
    )
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{thread_id}", operation_id="getPipeline", response_model=PipelineDetail)
async def get_pipeline(
    thread_id: ThreadId, identity: CurrentIdentity, service: PipelineService
) -> Any:
    try:
        return await service.get_pipeline(thread_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="pipeline thread not found") from None


@router.get(
    "/{thread_id}/phases/{phase}/prompt-review",
    operation_id="getPhasePromptReview",
    response_model=PhasePromptReview,
)
async def get_phase_prompt_review(
    thread_id: ThreadId,
    phase: PhaseParam,
    identity: CurrentIdentity,
    service: PipelineService,
) -> Any:
    try:
        return await service.get_phase_prompt_review(thread_id, phase)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid pipeline phase") from exc
    except NotFoundError:
        raise HTTPException(status_code=404, detail="pipeline thread not found") from None


@router.patch(
    "/{thread_id}/phases/{phase}/prompt-review",
    operation_id="patchPhasePromptReview",
    response_model=PhasePromptReview,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def patch_phase_prompt_review(
    thread_id: ThreadId,
    phase: PhaseParam,
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
    except PromptReviewConflictError as exc:
        raise HTTPException(
            status_code=409, detail="prompt review can no longer be edited"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid prompt review") from exc
    except NotFoundError:
        raise HTTPException(status_code=404, detail="pipeline thread not found") from None


@router.post(
    "/{thread_id}/rerun",
    operation_id="rerunPipeline",
    status_code=202,
    response_model=RerunPipelineResponse,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def rerun_pipeline(
    thread_id: ThreadId,
    body: RerunPipelineRequest,
    identity: CurrentIdentity,
    service: PipelineService,
) -> Any:
    """Rerun phases using the complete server-side checkpointed configuration."""

    try:
        run_id = await service.rerun_pipeline(
            thread_id,
            phases=list(body.phases),
            gates_mode=body.gates_mode,
            idempotency_key=body.idempotency_key,
            principal_id=identity.consumer_id,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="pipeline thread not found") from None
    except RerunConfigurationConflictError:
        return problem(
            409,
            "rerun_configuration_conflict",
            detail="the stored pipeline configuration cannot be safely rerun",
        )
    except RerunIdempotencyConflictError:
        return problem(
            409,
            "rerun_idempotency_conflict",
            detail="the rerun idempotency claim conflicts with an existing request",
        )
    except ConflictError:
        return problem(
            409,
            "rerun_already_active",
            detail="the pipeline already has an active run",
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid rerun request") from None
    return RerunPipelineResponse(run_id=run_id)


@router.post(
    "/{thread_id}/gates/{interrupt_id}/resume",
    operation_id="resumeGate",
    status_code=202,
    response_model=ResumeGateResponse,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def resume_gate(
    thread_id: ThreadId,
    interrupt_id: InterruptId,
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
        raise HTTPException(status_code=404, detail="pipeline thread not found") from None
    except GateSupersededError as exc:
        return problem(
            409,
            "gate_superseded",
            detail="the requested interrupt is no longer pending; re-fetch the pipeline",
            extra={"pending_gate": exc.pending_gate},
        )
    except InvalidGateActionError as exc:
        raise HTTPException(
            status_code=422,
            detail="action is not allowed for this gate",
        ) from exc
    except RerunConfigurationConflictError:
        return problem(
            409,
            "pipeline_configuration_conflict",
            detail="the stored pipeline configuration cannot be safely resumed",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid gate resume") from exc
    return ResumeGateResponse(run_id=run_id)


@router.post(
    "/{thread_id}/abort",
    operation_id="abortPipeline",
    status_code=202,
    response_model=AbortPipelineResponse,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def abort_pipeline(
    thread_id: ThreadId,
    service: PipelineService,
    engine_abort: AbortService,
) -> Any:
    """Stop an external engine while preserving its graph finalization monitor.

    Threads that have not entered execution have no engine handle and fall back
    to graph-only cancellation. Engine abort failures propagate so the API never
    reports success while production load is still running.
    """
    try:
        phase: str | None = None
        confirmed = False
        try:
            result = await engine_abort.abort(thread_id)
            cancelled = result.cancelled_runs
            phase = result.phase
            confirmed = result.confirmed
        except EngineRunNotFoundError:
            cancelled = await service.abort_pipeline(thread_id)
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
    except NotFoundError:
        raise HTTPException(status_code=404, detail="pipeline thread not found") from None
    except NoActiveRunError:
        return problem(
            409,
            "no_active_run",
            detail="the pipeline has no pending or running run to abort",
        )
    except TooManyActiveRunsError as exc:
        return problem(
            409,
            "active_run_backlog_too_large",
            detail=(
                f"the pipeline exceeds the bounded abort limit ({exc.limit}); "
                "use the operator cleanup runbook"
            ),
        )
    except ActiveRunSnapshotUnstableError:
        return problem(
            409,
            "active_run_snapshot_unstable",
            detail="active runs changed throughout abort; retry the request",
        )
    return AbortPipelineResponse(
        cancelled_run_ids=cancelled,
        phase=phase,
        confirmed=confirmed,
    )
