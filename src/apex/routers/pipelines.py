"""/pipelines facade: dashboard-shaped reads + gate CAS resume over the loopback API.

Visibility scoping happens server-side: the loopback client forwards the caller's
API key so the LangGraph @auth.on filters apply exactly as on direct calls — this
router never re-filters threads client-side.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from langgraph_sdk import Auth
from langgraph_sdk.errors import NotFoundError
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

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
    Phase,
)
from apex.domain.prompt_selection import PromptContextEdit, launch_config_json_schema
from apex.domain.test_scope import TestScopeV1
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.persistence.db import get_session, release_read_transactions
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
    EngineProjectionFinalizationPendingError,
    EngineProviderAbortError,
    EngineProvisioningAbortPendingError,
    EngineRunNotFoundError,
)
from apex.services.environments import (
    EnvironmentTargetNotApprovedError,
    EnvironmentTargetNotFoundError,
)
from apex.services.execution_readiness import (
    ExecutionReadiness,
    LaunchReadinessError,
    execution_readiness,
    require_execution_readiness,
)
from apex.services.langgraph_client import loopback_client
from apex.services.pipeline_actors import PipelineActors, pipeline_actor_names
from apex.services.pipeline_read import (
    MAX_PIPELINE_QUERY_CHARS,
    ActiveRunSnapshotUnstableError,
    CycleRerunConflictError,
    GateSupersededError,
    InvalidGateActionError,
    LaunchIdempotencyConflictError,
    LaunchProviderError,
    LaunchValidationError,
    NoActiveRunError,
    PipelineReadService,
    PromptReviewConflictError,
    RerunActiveRunConflictError,
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


def _pipeline_runtime_unavailable() -> HTTPException:
    return HTTPException(status_code=502, detail="pipeline runtime unavailable")


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

    packets: list[dict[str, Any]] | None = None
    not_found = False
    try:
        packets = await uploaded_document_context_packets(repository, identity, document_ids)
    except DocumentContextNotFoundError:
        not_found = True
    if not_found or packets is None:
        raise HTTPException(status_code=404, detail="document context not found")
    return packets


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
    scope_error_status: int | None = None
    try:
        ensure_thread_scope(identity, metadata, action="pipelines.create")
    except Auth.exceptions.HTTPException as exc:
        scope_error_status = exc.status_code
    if scope_error_status is not None:
        raise HTTPException(
            status_code=scope_error_status,
            detail="pipeline scope is not authorized",
        )
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
    console_url: str | None = None


class PipelineSummary(BaseModel):
    test_scope: dict[str, Any] | None = None
    thread_id: str
    title: str | None = None
    project_id: str | None = None
    app_id: str | None = None
    thread_status: str | None = None
    current_phase: str | None = None
    phase_strip: list[PhaseStripEntry]
    execution_result: (
        Literal["passed", "failed", "inconclusive", "not_assessed", "unavailable"] | None
    ) = None
    engine: PipelineEngineInfo | None = None
    created_at: str | None = None
    updated_at: str | None = None
    pending_gate: PendingGate | None = None
    rerun_blocked_reason: Literal["cycle_preparation_required"] | None = None


class PipelineListResponse(BaseModel):
    items: list[PipelineSummary]
    limit: int
    offset: int
    partial: bool = False


class StartPipelineRequest(BaseModel):
    """Start a pipeline run. For results analysis, select the analysis phases (e.g.
    ["reporting", "postmortem"]) and supply `external_results`; gates default to auto."""

    model_config = ConfigDict(extra="forbid")

    test_scope: TestScopeV1 | None = None
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
    configurable: Annotated[dict[str, Any], WithJsonSchema(launch_config_json_schema())] | None = (
        Field(
            default=None,
            max_length=64,
            description=("Full per-run configuration retained by the selected assistant."),
        )
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
        if value.get("rerun_phases") is not None:
            raise ValueError("rerun_phases is server-owned; use the phase rerun endpoint")
        if value.get("prompt_manifest") is not None:
            raise ValueError("prompt_manifest is server-owned")
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


class ExecutionReadinessRequest(BaseModel):
    test_scope: TestScopeV1 | None = None
    """The caller's selected contract, with preset values already resolved."""

    model_config = ConfigDict(extra="forbid")
    project_id: ScopeId | None = None
    app_id: ScopeId | None = None
    engine: NoNulStr = Field(min_length=1, max_length=64)
    connection_id: NoNulStr | None = Field(default=None, min_length=1, max_length=256)
    artifact_connection_id: NoNulStr | None = Field(default=None, min_length=1, max_length=256)
    phases: list[PhaseInput] = Field(max_length=len(PHASE_ORDER))

    @field_validator("phases")
    @classmethod
    def validate_phases(cls, value: list[str]) -> list[str]:
        return StartPipelineRequest.validate_phases(value) or []


class GateInterrupt(BaseModel):
    interrupt_id: str | None = None
    kind: str | None = None
    phase: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class PipelineDetail(PipelineSummary):
    values: dict[str, Any] = Field(default_factory=dict)
    interrupts: list[GateInterrupt] = Field(default_factory=list)


class PipelineSetupConfiguration(BaseModel):
    title: str
    request: str
    project_id: str | None
    app_id: str | None
    environment_id: str | None
    configurable: Annotated[dict[str, Any], WithJsonSchema(launch_config_json_schema())]


class GatePromptEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_edits: list[PromptContextEdit] | None = Field(default=None, max_length=64)

    system: NoNulStr | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    user: NoNulStr | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    application: NoNulStr | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)


class ResumeGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: NoNulStr = Field(min_length=1, max_length=32)
    task_id: NoNulStr | None = Field(default=None, max_length=256)
    expected_revision: int | None = Field(default=None, ge=1)
    output: dict[str, Any] | None = None
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
    system_locked: bool | None = None
    contexts: list[dict[str, Any]] | None = None
    selections: list[dict[str, Any]] | None = None
    system: str
    phase_prompt: str
    application: str | None = None
    additional_context: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    updated_at: str
    updated_by: str


class PhasePromptReviewUpdate(BaseModel):
    context_edits: list[PromptContextEdit] | None = Field(default=None, max_length=64)
    model_config = ConfigDict(extra="forbid")

    system: NoNulStr = Field(max_length=MAX_PROMPT_PART_CHARS_HARD)
    phase_prompt: NoNulStr = Field(max_length=MAX_PROMPT_PART_CHARS_HARD)
    application: NoNulStr | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    additional_context: NoNulStr = Field(default="", max_length=MAX_GATE_STRING_CHARS_HARD)


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post(
    "/execution-readiness",
    operation_id="getPipelineExecutionReadiness",
    response_model=ExecutionReadiness,
)
async def get_pipeline_execution_readiness(
    body: ExecutionReadinessRequest, identity: CurrentIdentity, catalog: CatalogRepo
) -> ExecutionReadiness:
    """Check scoped connection metadata, without testing provider connectivity."""
    if body.test_scope:
        from apex.services.test_scope import allows_test_scope

        if not allows_test_scope(identity, body.test_scope):
            raise HTTPException(403, "Access to every test participant is required")
    project_id, app_id = await _resolve_pipeline_scope(
        identity, project_id=body.project_id, app_id=body.app_id, catalog=catalog
    )
    await release_read_transactions(catalog)
    config = None
    try:
        config = PipelineConfigurable.model_validate(
            {
                "project_id": project_id,
                "app_id": app_id,
                "test_scope": body.test_scope,
                "engine": body.engine,
                "phases": body.phases,
                "connections": {
                    **({"execution_engine": body.connection_id} if body.connection_id else {}),
                    **(
                        {"artifact_store": body.artifact_connection_id}
                        if body.artifact_connection_id
                        else {}
                    ),
                },
            }
        )
    except ValueError:
        pass
    if config is None:
        raise HTTPException(status_code=422, detail="invalid execution readiness configuration")
    return await execution_readiness(config)


async def _recover_legacy_launch(body, identity, documents, request):
    """Read-only retry of a pre-topology launch, never admission of a new run."""
    config = dict(body.configurable or {})
    metadata = {
        key: value
        for key, value in {
            "project_id": body.project_id or config.get("project_id"),
            "app_id": body.app_id or config.get("app_id"),
        }.items()
        if value is not None
    }
    ensure_thread_scope(identity, metadata, action="pipelines.create")
    project_id, app_id = metadata.get("project_id"), metadata.get("app_id")
    if any(
        body_value and config.get(key) and body_value != config[key]
        for key, body_value in (("project_id", body.project_id), ("app_id", body.app_id))
    ):
        raise HTTPException(409, "pipeline launch conflict")
    for key in ("environment_target", "environment_target_version"):
        config.pop(key, None)
    packets = [packet.model_dump(mode="json") for packet in (body.context_packets or [])]
    if body.document_ids:
        packets += await _documents_to_packets(documents, identity, body.document_ids)
    await release_read_transactions(documents)
    service = _pipeline_read_service_after_scope(request)
    error = None
    result = None
    try:
        result = await service.start_run(
            title=body.title,
            request=body.request,
            idempotency_key=body.idempotency_key,
            assistant_id=body.assistant_id,
            project_id=project_id,
            app_id=app_id,
            configurable=config,
            phases=body.phases,
            gates=body.gates,
            agent_backend=body.agent_backend,
            model_by_phase=body.model_by_phase,
            external_results=body.external_results.model_dump(mode="json")
            if body.external_results
            else None,
            context_packets=packets or None,
            principal_id=identity.consumer_id,
            recovery_only=True,
        )
    except LaunchIdempotencyConflictError:
        error = (409, "pipeline launch conflict")
    except ValueError:
        error = (422, "test_scope is required; no matching legacy launch can be recovered")
    except LaunchProviderError:
        error = (503, "pipeline launch recovery unavailable")
    if error:
        raise HTTPException(*error)
    return result


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
    if "cycle_dispatch_id" in run_configurable:
        raise HTTPException(
            422, "Cycle dispatch bindings are server-owned; use the cycle dispatch endpoint"
        )
    from apex.services.test_scope import resolve_test_scope

    raw_scope = body.test_scope or run_configurable.get("test_scope")
    if raw_scope is None:
        claimed_scope = {
            key: value
            for key, value in {
                "project_id": body.project_id or run_configurable.get("project_id"),
                "app_id": body.app_id or run_configurable.get("app_id"),
            }.items()
            if value is not None
        }
        denied = False
        try:
            if claimed_scope:
                ensure_thread_scope(identity, claimed_scope, action="pipelines.create")
        except Auth.exceptions.HTTPException:
            denied = True
        if denied:
            raise HTTPException(403, "pipeline scope is not authorized")
        if body.idempotency_key:
            return await _recover_legacy_launch(body, identity, documents, request)
        raise HTTPException(
            422, "test_scope is required; select layers, a backend and a primary system"
        )
    import hashlib
    import json

    request_intent = hashlib.sha256(
        json.dumps(
            body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    scope_error: tuple[int, str] | None = None
    snapshot: dict[str, Any] = {}
    topology: TestScopeV1 | None = None
    try:
        topology = TestScopeV1.model_validate(raw_scope)
        from apex.services.test_scope import allows_test_scope

        if not allows_test_scope(identity, topology):
            raise HTTPException(403, "Access to every participating application is required")
        if body.idempotency_key:
            service = _pipeline_read_service_after_scope(request)
            recovered = await service.recover_scope_launch(
                principal_id=identity.consumer_id,
                project_id=topology.primary.project_id,
                app_id=topology.primary.application_id,
                idempotency_key=body.idempotency_key,
                request_intent=request_intent,
                test_scope=topology.model_dump(mode="json"),
            )
            if recovered is not None:
                return StartPipelineResponse(**recovered)
        topology.validate_admission()
        service = _pipeline_read_service_after_scope(request)
        defaults = await service.assistant_launch_defaults(body.assistant_id)
        run_configurable = {**defaults, **run_configurable}
        effective = PipelineConfigurable.model_validate(
            {
                **run_configurable,
                "test_scope": topology,
                "test_scope_snapshot": None,
                "project_id": topology.primary.project_id,
                "app_id": topology.primary.application_id,
                **{
                    key: value
                    for key, value in {
                        "phases": body.phases,
                        "gates": body.gates,
                        "agent_backend": body.agent_backend,
                        "model_by_phase": body.model_by_phase,
                    }.items()
                    if value is not None
                },
            }
        )
        live = effective.engine != "sim" and Phase.EXECUTION in effective.agent_phases()
        snapshot = await resolve_test_scope(catalog, identity, topology, live=live)
    except LaunchIdempotencyConflictError:
        scope_error = (409, "pipeline launch conflict")
    except LaunchProviderError:
        scope_error = (503, "pipeline launch recovery unavailable")
    except ValueError:
        scope_error = (422, "invalid pipeline configuration")
    except EnvironmentTargetNotApprovedError:
        scope_error = (422, "Traffic entry requires an approved target")
    except EnvironmentTargetNotFoundError:
        scope_error = (404, "Test participant is unavailable")
    if scope_error:
        raise HTTPException(*scope_error)
    assert topology is not None
    run_configurable["test_scope"] = topology.model_dump(mode="json")
    run_configurable["test_scope_snapshot"] = snapshot
    run_configurable["project_id"] = topology.primary.project_id
    run_configurable["app_id"] = topology.primary.application_id
    run_configurable["environment_id"] = (
        topology.traffic_entry.environment_id if topology.traffic_entry else None
    )
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
    run_configurable.pop("environment_target", None)
    run_configurable.pop("environment_target_version", None)
    if target := snapshot.get("execution_target"):
        run_configurable["environment_target"] = target["base_url"]
        run_configurable["environment_target_version"] = target["version"]
    service = _pipeline_read_service_after_scope(request)
    context_packets = [packet.model_dump(mode="json") for packet in (body.context_packets or [])]
    if body.document_ids:
        context_packets += await _documents_to_packets(documents, identity, body.document_ids)
    await release_read_transactions(catalog, documents)
    # A canonical graph defaults to all phases; sparse custom assistants retain
    # their inheritance. The wizard sends its explicit reviewed engine and plan.
    # Check only after root reconciliation so later edits cannot block a replay.
    readiness_config = None
    if "engine" in run_configurable and (
        body.phases is not None
        or "phases" in run_configurable
        or body.assistant_id in (None, "pipeline")
    ):
        readiness_config = {
            # The service validates the effective sparse launch contract first.
            # Only execution-selection fields belong in this metadata check;
            # unrelated top-level overrides may have cleared inherited values.
            **{
                key: run_configurable[key]
                for key in (
                    "engine",
                    "connections",
                    "phases",
                    "start_phase",
                    "stop_after",
                    "phase_execution",
                    "analysis",
                    "test_scope",
                )
                if key in run_configurable
            },
            "project_id": project_id,
            "app_id": app_id,
            **({"phases": body.phases} if body.phases is not None else {}),
        }

    async def check_execution_before_start(_thread_id: str) -> None:
        from apex.services.test_scope import persist_test_scope

        await persist_test_scope(_thread_id, topology)
        if readiness_config is not None:
            checked = PipelineConfigurable.model_validate(readiness_config)
            from apex.services.analysis_readiness import require_analysis_readiness

            await require_analysis_readiness(checked)
            await require_execution_readiness(checked)

    result = None
    launch_error: HTTPException | None = None
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
            request_intent=request_intent if body.idempotency_key else None,
            before_run_start=(check_execution_before_start),
        )
    except LaunchIdempotencyConflictError:
        launch_error = HTTPException(status_code=409, detail="pipeline launch conflict")
    except LaunchValidationError:
        launch_error = HTTPException(
            status_code=422,
            detail=(
                "Pipeline launch configuration was rejected. "
                "Review the selected preset and configuration."
            ),
        )
    except LaunchProviderError:
        launch_error = _pipeline_runtime_unavailable()
    except LaunchReadinessError as exc:
        launch_error = HTTPException(status_code=422, detail=str(exc))
    except ValueError:
        launch_error = HTTPException(status_code=422, detail="invalid pipeline configuration")
    except Exception:
        launch_error = _pipeline_runtime_unavailable()
    if launch_error is not None:
        # Raise after leaving the handler: provider/config validation exceptions
        # may retain caller input even when an explicit ``from None`` suppresses
        # their display.
        raise launch_error
    if result is None:  # pragma: no cover - service contract invariant
        raise HTTPException(status_code=502, detail="pipeline launch returned no result")
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
    result: Any = None
    runtime_failed = False
    try:
        result = await service.list_pipelines(
            project=project, status=status, q=q, limit=limit, offset=offset
        )
    except Exception:
        runtime_failed = True
    if runtime_failed:
        raise _pipeline_runtime_unavailable()
    if result is None:  # pragma: no cover - service contract invariant
        raise _pipeline_runtime_unavailable()
    return {
        "items": result.items,
        "limit": limit,
        "offset": offset,
        "partial": result.partial,
    }


class AnalysisConnectionChoice(BaseModel):
    id: str
    name: str
    provider: str
    kind: str


@router.get(
    "/analysis/connections",
    operation_id="listAnalysisConnections",
    response_model=list[AnalysisConnectionChoice],
)
async def list_analysis_connections(
    project_id: ScopeId,
    identity: CurrentIdentity,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    from sqlalchemy import or_, select

    from apex.persistence.models import Connection

    ensure_scope(identity, project_id=project_id)
    rows = (
        await session.scalars(
            select(Connection)
            .where(
                Connection.enabled.is_(True),
                or_(Connection.project_id == project_id, Connection.project_id.is_(None)),
                or_(
                    Connection.kind == "analysis",
                    (Connection.kind == "execution_engine") & (Connection.provider == "apex_load"),
                ),
            )
            .order_by(Connection.name)
            .limit(200)
        )
    ).all()
    return [
        AnalysisConnectionChoice(id=row.id, name=row.name, provider=row.provider, kind=row.kind)
        for row in rows
    ]


@router.get("/{thread_id}", operation_id="getPipeline", response_model=PipelineDetail)
async def get_pipeline(
    thread_id: ThreadId, identity: CurrentIdentity, service: PipelineService
) -> Any:
    result: Any = None
    not_found = False
    runtime_failed = False
    try:
        result = await service.get_pipeline(thread_id)
    except NotFoundError:
        not_found = True
    except Exception:
        runtime_failed = True
    if runtime_failed:
        raise _pipeline_runtime_unavailable()
    if not_found or result is None:
        raise HTTPException(status_code=404, detail="pipeline thread not found")
    return result


@router.get("/{thread_id}/actors", operation_id="getPipelineActors", response_model=PipelineActors)
async def get_pipeline_actors(
    thread_id: ThreadId,
    identity: CurrentIdentity,
    service: PipelineService,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PipelineActors:
    snapshot = await get_pipeline(thread_id, identity, service)
    return await pipeline_actor_names(snapshot, session, identity)


@router.get(
    "/{thread_id}/setup",
    operation_id="getPipelineSetup",
    response_model=PipelineSetupConfiguration,
)
async def get_pipeline_setup(
    thread_id: ThreadId,
    identity: CurrentIdentity,
    service: PipelineService,
) -> Any:
    try:
        return await service.get_test_setup(thread_id)
    except NotFoundError:
        raise HTTPException(404, "Pipeline thread not found") from None
    except (ValueError, RerunConfigurationConflictError):
        raise HTTPException(409, "The stored setup cannot be copied safely") from None


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
    result: Any = None
    failure: HTTPException | None = None
    try:
        result = await service.get_phase_prompt_review(thread_id, phase)
    except ValueError:
        failure = HTTPException(status_code=422, detail="invalid pipeline phase")
    except NotFoundError:
        failure = HTTPException(status_code=404, detail="pipeline thread not found")
    except Exception:
        failure = _pipeline_runtime_unavailable()
    if failure is not None:
        raise failure
    if result is None:  # pragma: no cover - service contract invariant
        raise HTTPException(status_code=502, detail="prompt review returned no result")
    return result


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
    result: Any = None
    failure: HTTPException | None = None
    try:
        result = await service.update_phase_prompt_review(
            thread_id,
            phase,
            body.model_dump(mode="json"),
            actor=identity.name or identity.consumer_id,
        )
    except PromptReviewConflictError:
        failure = HTTPException(status_code=409, detail="prompt review can no longer be edited")
    except ValueError:
        failure = HTTPException(status_code=422, detail="invalid prompt review")
    except NotFoundError:
        failure = HTTPException(status_code=404, detail="pipeline thread not found")
    except Exception:
        failure = _pipeline_runtime_unavailable()
    if failure is not None:
        raise failure
    if result is None:  # pragma: no cover - service contract invariant
        raise HTTPException(status_code=502, detail="prompt review update returned no result")
    return result


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

    run_id: str | None = None
    failure: HTTPException | None = None
    try:
        run_id = await service.rerun_pipeline(
            thread_id,
            phases=list(body.phases),
            gates_mode=body.gates_mode,
            idempotency_key=body.idempotency_key,
            principal_id=identity.consumer_id,
        )
    except NotFoundError:
        failure = HTTPException(status_code=404, detail="pipeline thread not found")
    except CycleRerunConflictError:
        return problem(
            409,
            "rerun_configuration_conflict",
            detail="Cycle executions cannot be rerun; prepare a new approved cycle binding",
        )
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
    except RerunActiveRunConflictError:
        return problem(
            409,
            "rerun_already_active",
            detail="the pipeline already has an active run",
        )
    except ValueError:
        failure = HTTPException(status_code=422, detail="invalid rerun request")
    except Exception:
        failure = _pipeline_runtime_unavailable()
    if failure is not None:
        raise failure
    if run_id is None:  # pragma: no cover - service contract invariant
        raise HTTPException(status_code=502, detail="pipeline rerun returned no run")
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
    identity: CurrentIdentity,
    service: PipelineService,
) -> Any:
    """Compare-and-set resume: 409 gate_superseded when the interrupt is stale."""
    run_id: str | None = None
    failure: HTTPException | None = None
    try:
        run_id = await service.resume_gate(
            thread_id,
            interrupt_id,
            body.action,
            {
                "prompt": body.prompt.model_dump(mode="json", exclude_unset=True)
                if body.prompt
                else None,
                "task_id": body.task_id,
                "expected_revision": body.expected_revision,
                "output": body.output,
                "instructions": body.instructions,
                "message": body.message,
                "note": body.note,
            },
            principal_id=identity.consumer_id,
        )
    except NotFoundError:
        failure = HTTPException(status_code=404, detail="pipeline thread not found")
    except GateSupersededError as exc:
        return problem(
            409,
            "gate_superseded",
            detail="the requested interrupt is no longer pending; re-fetch the pipeline",
            extra={"pending_gate": exc.pending_gate},
        )
    except InvalidGateActionError:
        failure = HTTPException(
            status_code=422,
            detail="action is not allowed for this gate",
        )
    except RerunConfigurationConflictError:
        return problem(
            409,
            "pipeline_configuration_conflict",
            detail="the stored pipeline configuration cannot be safely resumed",
        )
    except ValueError:
        failure = HTTPException(status_code=422, detail="invalid gate resume")
    except Exception:
        failure = _pipeline_runtime_unavailable()
    if failure is not None:
        raise failure
    if run_id is None:  # pragma: no cover - service contract invariant
        raise HTTPException(status_code=502, detail="pipeline gate resume returned no run")
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
    phase: str | None = None
    confirmed = False
    cancelled: list[str] | None = None
    use_graph_fallback = False
    failure: HTTPException | None = None
    try:
        try:
            result = await engine_abort.abort(thread_id)
            cancelled = result.cancelled_runs
            phase = result.phase
            confirmed = result.confirmed
        except EngineRunNotFoundError:
            use_graph_fallback = True
        except EngineConnectionAffinityMissingError:
            failure = HTTPException(
                status_code=409,
                detail=ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL,
            )
        except EngineAbortConfirmationPendingError:
            failure = HTTPException(
                status_code=503,
                detail="external engine is still stopping; retry abort to confirm termination",
                headers={"Retry-After": "1"},
            )
        except EngineProvisioningAbortPendingError:
            failure = HTTPException(
                status_code=503,
                detail="engine provisioning is still establishing an abort handle; retry abort",
                headers={"Retry-After": "1"},
            )
        except EngineGraphFinalizationPendingError:
            failure = HTTPException(
                status_code=503,
                detail=(
                    "external engine stopped but graph finalization is pending recovery; "
                    "resume the pipeline"
                ),
                headers={"Retry-After": "1"},
            )
        except EngineProjectionFinalizationPendingError:
            failure = HTTPException(
                status_code=503,
                detail=(
                    "external engine stopped but provider cleanup or durable projection is "
                    "pending; retry abort"
                ),
                headers={"Retry-After": "1"},
            )
        except EngineProviderAbortError:
            failure = HTTPException(
                status_code=502,
                detail="engine provider abort failed",
            )
        if use_graph_fallback:
            cancelled = await service.abort_pipeline(thread_id)
    except NotFoundError:
        failure = HTTPException(status_code=404, detail="pipeline thread not found")
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
    except Exception:
        failure = _pipeline_runtime_unavailable()
    if failure is not None:
        raise failure
    if cancelled is None:  # pragma: no cover - abort service contract invariant
        raise HTTPException(status_code=502, detail="pipeline abort returned no result")
    return AbortPipelineResponse(
        cancelled_run_ids=cancelled,
        phase=phase,
        confirmed=confirmed,
    )
