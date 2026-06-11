"""Context routes (`/context`): summary runs + dashboard evidence aggregation.

POST /context/summaries launches a stateless background run on the `context`
assistant through the loopback LangGraph client (caller's API key forwarded, so
auth/scoping match a direct call) and answers 202 with the run id + stream URL.
GET /context/evidence aggregates context packets across recent pipeline threads.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apex.app.dependencies import CurrentIdentity, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.auth.service import extract_api_key
from apex.services.context import collect_context_evidence, start_context_summary
from apex.services.langgraph_client import loopback_client

router = APIRouter(prefix="/context", tags=["context"])


def get_loopback_client(request: Request) -> Any:
    """Loopback LangGraph client carrying the caller's API key (override in tests)."""
    return loopback_client(extract_api_key(request.headers))


LoopbackClient = Annotated[Any, Depends(get_loopback_client)]
OperatorIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))]


# ── Schemas ──────────────────────────────────────────────────────────────────


class ContextSummaryRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=2000)
    work_item_keys: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    project_id: str | None = None


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
    body: ContextSummaryRequest, identity: OperatorIdentity, client: LoopbackClient
) -> ContextSummaryAccepted:
    if body.project_id is not None and not identity.allows_project(body.project_id):
        raise HTTPException(
            status_code=403,
            detail=f"Project '{body.project_id}' is outside this consumer's scopes",
        )
    result = await start_context_summary(
        client,
        subject=body.subject,
        work_item_keys=body.work_item_keys,
        document_ids=body.document_ids,
        project_id=body.project_id,
    )
    return ContextSummaryAccepted(**result)


@router.get("/evidence", operation_id="listContextEvidence")
async def list_context_evidence(
    identity: CurrentIdentity,
    client: LoopbackClient,
    project: Annotated[str | None, Query(description="Filter to one project")] = None,
    thread_id: Annotated[str | None, Query(description="Narrow to one thread")] = None,
) -> list[EvidencePacket]:
    if project is not None and not identity.allows_project(project):
        raise HTTPException(
            status_code=403, detail=f"Project '{project}' is outside this consumer's scopes"
        )
    try:
        packets = await collect_context_evidence(client, project_id=project, thread_id=thread_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [EvidencePacket(**packet) for packet in packets]
