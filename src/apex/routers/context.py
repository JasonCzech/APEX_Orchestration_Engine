"""Context routes (`/context`): summary runs + dashboard evidence aggregation.

POST /context/summaries launches a stateless background run on the `context`
assistant through the loopback LangGraph client (caller's API key forwarded, so
auth/scoping match a direct call) and answers 202 with the run id + stream URL.
GET /context/evidence aggregates context packets across recent pipeline threads.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.auth.service import extract_api_key
from apex.persistence.repositories.documents import DocumentsRepository
from apex.routers.work_tracking import (
    resolve_scoped_work_tracking_adapter,
    select_work_tracking_project,
)
from apex.services.connections import ConnectionResolver
from apex.services.context import collect_context_evidence, start_context_summary
from apex.services.documents import (
    DocumentContextNotFoundError,
    get_documents_repository,
    uploaded_document_context_packets,
)
from apex.services.langgraph_client import loopback_client
from apex.services.run_validation import (
    MAX_WORK_ITEM_KEY_CHARS,
    MAX_WORK_ITEM_KEYS_HARD,
    validate_context_run_input,
)
from apex.services.work_tracking import get_work_tracking_resolver
from apex.settings import get_settings

router = APIRouter(prefix="/context", tags=["context"])


def get_loopback_client(request: Request) -> Any:
    """Loopback LangGraph client carrying the caller's API key (override in tests)."""
    return loopback_client(extract_api_key(request.headers))


def _loopback_client_after_scope(request: Request) -> Any:
    overrides: Any = getattr(request.app, "dependency_overrides", {})
    override = overrides.get(get_loopback_client)
    if override is not None:
        return override()
    return get_loopback_client(request)


LoopbackClient = Annotated[Any, Depends(get_loopback_client)]
OperatorIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))]
WorkTrackingResolver = Annotated[ConnectionResolver, Depends(get_work_tracking_resolver)]
DocumentsRepo = Annotated[DocumentsRepository, Depends(get_documents_repository)]


# ── Schemas ──────────────────────────────────────────────────────────────────


class ContextSummaryRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=2000)
    work_item_keys: list[
        Annotated[str, Field(min_length=1, max_length=MAX_WORK_ITEM_KEY_CHARS)]
    ] = Field(default_factory=list, max_length=MAX_WORK_ITEM_KEYS_HARD)
    document_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=64
    )
    project_id: str | None = Field(default=None, min_length=1, max_length=256)


class ContextSummaryAccepted(BaseModel):
    run_id: str
    stream_url: str = Field(
        description="Join the run's SSE stream: GET this path on the LangGraph "
        "surface (same host) with your API key."
    )


class EvidencePacket(BaseModel):
    id: str | None
    source: str
    title: str
    summary: str | None
    ref: str | None
    thread_id: str | None


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("/summaries", operation_id="createContextSummary", status_code=202)
async def create_context_summary(
    body: ContextSummaryRequest,
    identity: OperatorIdentity,
    request: Request,
    resolver: WorkTrackingResolver,
    documents: DocumentsRepo,
) -> ContextSummaryAccepted:
    selected_project = select_work_tracking_project(identity, body.project_id)
    limits = get_settings().runs
    if len(body.document_ids) > limits.max_context_packets:
        raise HTTPException(
            status_code=422,
            detail=(f"document_ids exceeds the deployment limit ({limits.max_context_packets})"),
        )
    if len(set(body.document_ids)) != len(body.document_ids):
        raise HTTPException(status_code=422, detail="document_ids must not contain duplicates")
    try:
        validated = validate_context_run_input(
            {
                "subject": body.subject,
                "work_item_keys": body.work_item_keys,
                "document_packets": [],
                "project_id": selected_project,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if validated.work_item_keys:
        # The graph fetches keys outside the HTTP dependency chain. Prove here
        # that its resolved real-provider adapter is bound to the caller's project.
        await resolve_scoped_work_tracking_adapter(
            resolver,
            identity,
            project=selected_project,
        )
    try:
        document_packets = await uploaded_document_context_packets(
            documents, identity, body.document_ids
        )
    except DocumentContextNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        validated = validate_context_run_input(
            {
                "subject": validated.subject,
                "work_item_keys": validated.work_item_keys,
                "document_packets": document_packets,
                "project_id": validated.project_id,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    client = _loopback_client_after_scope(request)
    result = await start_context_summary(
        client,
        subject=validated.subject,
        work_item_keys=validated.work_item_keys,
        document_packets=[
            packet.model_dump(mode="json", exclude_none=True)
            for packet in validated.document_packets
        ],
        project_id=validated.project_id,
    )
    return ContextSummaryAccepted(**result)


@router.get("/evidence", operation_id="listContextEvidence")
async def list_context_evidence(
    identity: CurrentIdentity,
    request: Request,
    project: Annotated[str | None, Query(description="Filter to one project")] = None,
    thread_id: Annotated[str | None, Query(description="Narrow to one thread")] = None,
) -> list[EvidencePacket]:
    ensure_scope(identity, project_id=project)
    client = _loopback_client_after_scope(request)
    try:
        packets = await collect_context_evidence(client, project_id=project, thread_id=thread_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [EvidencePacket(**packet) for packet in packets]
