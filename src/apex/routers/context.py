"""Context routes (`/context`): summary runs + dashboard evidence aggregation.

POST /context/summaries launches a durable background run on the `context`
assistant through the loopback LangGraph client (caller's API key forwarded, so
auth/scoping match a direct call) and answers 202 with the run id + stream URL.
GET /context/evidence aggregates context packets across recent pipeline threads.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.auth.service import extract_api_key
from apex.domain.input_limits import NoNulStr, RecordId, ResourceId, ScopeId
from apex.persistence.db import release_read_transactions
from apex.persistence.repositories.documents import DocumentsRepository
from apex.routers.work_tracking import (
    resolve_work_tracking_connection_identity,
    select_work_tracking_project,
)
from apex.services.connections import ConnectionResolver
from apex.services.context import (
    ContextRunStartError,
    collect_context_evidence,
    start_context_summary,
)
from apex.services.documents import (
    DocumentContextNotFoundError,
    get_documents_repository,
    uploaded_document_context_packets,
)
from apex.services.langgraph_client import loopback_client
from apex.services.run_validation import (
    MAX_WORK_ITEM_KEY_CHARS,
    MAX_WORK_ITEM_KEYS_HARD,
    ContextRunInput,
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
DocumentsRepo = Annotated[DocumentsRepository, Depends(get_documents_repository)]
WorkTrackingResolver = Annotated[ConnectionResolver, Depends(get_work_tracking_resolver)]


# ── Schemas ──────────────────────────────────────────────────────────────────

ContextWorkItemKey = Annotated[
    NoNulStr,
    Field(min_length=1, max_length=MAX_WORK_ITEM_KEY_CHARS),
]
ContextDocumentId = Annotated[NoNulStr, Field(min_length=1, max_length=128)]


class ContextSummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: NoNulStr = Field(min_length=1, max_length=2000)
    work_item_keys: list[ContextWorkItemKey] = Field(
        default_factory=list, max_length=MAX_WORK_ITEM_KEYS_HARD
    )
    document_ids: list[ContextDocumentId] = Field(default_factory=list, max_length=64)
    project_id: ScopeId | None = None
    work_tracking_connection_id: RecordId | None = None


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
    documents: DocumentsRepo,
    resolver: WorkTrackingResolver,
) -> ContextSummaryAccepted:
    selected_project = select_work_tracking_project(identity, body.project_id)
    limits = get_settings().runs
    if len(body.work_item_keys) > limits.max_work_item_keys:
        raise HTTPException(
            status_code=422,
            detail=(
                "work_item_keys exceeds the deployment limit "
                f"({limits.max_work_item_keys})"
            ),
        )
    if len(set(body.work_item_keys)) != len(body.work_item_keys):
        raise HTTPException(status_code=422, detail="work_item_keys must not contain duplicates")
    if len(body.document_ids) > limits.max_context_packets:
        raise HTTPException(
            status_code=422,
            detail=(f"document_ids exceeds the deployment limit ({limits.max_context_packets})"),
        )
    if len(set(body.document_ids)) != len(body.document_ids):
        raise HTTPException(status_code=422, detail="document_ids must not contain duplicates")
    resolved_work_tracking_connection_id: str | None = None
    if body.work_item_keys:
        binding = await resolve_work_tracking_connection_identity(
            resolver,
            identity,
            body.work_tracking_connection_id,
            selected_project,
        )
        resolved_work_tracking_connection_id = binding.connection_id
    elif body.work_tracking_connection_id is not None:
        raise HTTPException(
            status_code=422,
            detail="work_tracking_connection_id requires at least one work_item_key",
        )
    request_error: HTTPException | None = None
    validated: ContextRunInput | None = None
    try:
        validated = validate_context_run_input(
            {
                "subject": body.subject,
                "work_item_keys": body.work_item_keys,
                "document_packets": [],
                "project_id": selected_project,
                "work_tracking_connection_id": resolved_work_tracking_connection_id,
            }
        )
    except ValueError:
        request_error = HTTPException(status_code=422, detail="invalid context summary request")
    if request_error is not None:
        raise request_error
    assert validated is not None
    request_error = None
    document_packets: list[dict[str, Any]] | None = None
    try:
        document_packets = await uploaded_document_context_packets(
            documents, identity, body.document_ids
        )
    except DocumentContextNotFoundError:
        request_error = HTTPException(status_code=404, detail="document context not found")
    if request_error is not None:
        raise request_error
    assert document_packets is not None
    request_error = None
    try:
        validated = validate_context_run_input(
            {
                "subject": validated.subject,
                "work_item_keys": validated.work_item_keys,
                "document_packets": document_packets,
                "project_id": validated.project_id,
                "work_tracking_connection_id": validated.work_tracking_connection_id,
            }
        )
    except ValueError:
        request_error = HTTPException(status_code=422, detail="invalid context summary request")
    if request_error is not None:
        raise request_error
    assert validated is not None
    await release_read_transactions(documents)
    client = _loopback_client_after_scope(request)
    request_error = None
    result: dict[str, str] | None = None
    try:
        result = await start_context_summary(
            client,
            subject=validated.subject,
            work_item_keys=validated.work_item_keys,
            document_packets=[
                packet.model_dump(mode="json", exclude_none=True)
                for packet in validated.document_packets
            ],
            project_id=validated.project_id,
            work_tracking_connection_id=validated.work_tracking_connection_id,
        )
    except ValueError:
        request_error = HTTPException(status_code=422, detail="invalid context summary request")
    except ContextRunStartError:
        request_error = HTTPException(status_code=502, detail="context runtime unavailable")
    if request_error is not None:
        raise request_error
    assert result is not None
    return ContextSummaryAccepted(**result)


@router.get("/evidence", operation_id="listContextEvidence")
async def list_context_evidence(
    identity: CurrentIdentity,
    request: Request,
    project: Annotated[ScopeId | None, Query(description="Filter to one project")] = None,
    thread_id: Annotated[ResourceId | None, Query(description="Narrow to one thread")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> list[EvidencePacket]:
    ensure_scope(identity, project_id=project)
    client = _loopback_client_after_scope(request)
    lookup_error: HTTPException | None = None
    packets: list[dict[str, Any]] | None = None
    try:
        packets = await collect_context_evidence(
            client,
            project_id=project,
            thread_id=thread_id,
            limit=limit,
            offset=offset,
        )
    except LookupError:
        lookup_error = HTTPException(status_code=404, detail="context thread not found")
    except Exception:
        lookup_error = HTTPException(status_code=502, detail="context runtime unavailable")
    if lookup_error is not None:
        raise lookup_error
    assert packets is not None
    return [EvidencePacket(**packet) for packet in packets]
