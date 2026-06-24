"""/artifacts: same-origin authenticated byte proxy for dashboards and iframes.

Streams artifact-store objects so the dashboard never needs direct store
credentials. Bytes are served only when the key can be tied to an object the
caller is allowed to read: uploaded document metadata, engine-run projection
ownership, or a transcript key carrying a readable thread id.

The ArtifactStorePort returns whole byte payloads (no ranged reads in the port
yet), so the response chunks an in-memory buffer — fine for M2-scale artifacts;
a ranged/streaming port method is an M3 concern alongside the S3 adapter.
"""

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph_sdk.errors import NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity
from apex.auth.identity import ConsumerIdentity
from apex.auth.service import extract_api_key
from apex.persistence.db import get_session
from apex.persistence.repositories.documents import DocumentsRepository
from apex.persistence.repositories.engine_runs import EngineRunsRepository
from apex.services.documents import get_artifact_store, get_documents_repository
from apex.services.langgraph_client import loopback_client

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

RepositoryDep = Annotated[DocumentsRepository, Depends(get_documents_repository)]
ArtifactStoreDep = Annotated[Any, Depends(get_artifact_store)]

_CHUNK_SIZE = 64 * 1024
FALLBACK_MEDIA_TYPE = "application/octet-stream"
TRANSCRIPT_PREFIX = "transcripts/"
ENGINE_RUN_PREFIX = "engine-runs/"


def get_engine_runs_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EngineRunsRepository:
    return EngineRunsRepository(session)


EngineRunsRepoDep = Annotated[EngineRunsRepository, Depends(get_engine_runs_repository)]


def _chunked(data: bytes, size: int = _CHUNK_SIZE) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


@router.get("/{key:path}", operation_id="getArtifact")
async def get_artifact(
    key: str,
    identity: CurrentIdentity,
    store: ArtifactStoreDep,
    repository: RepositoryDep,
    engine_runs: EngineRunsRepoDep,
    request: Request,
) -> StreamingResponse:
    media_type = await _authorize_artifact_key(key, identity, repository, engine_runs, request)
    document = await repository.get_by_artifact_key(key)
    if document is not None:
        media_type = document.media_type

    try:
        data = await store.get(key)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail=f"artifact {key!r} not found") from None

    return StreamingResponse(_chunked(data), media_type=media_type)


async def _authorize_artifact_key(
    key: str,
    identity: ConsumerIdentity,
    documents: DocumentsRepository,
    engine_runs: EngineRunsRepository,
    request: Request,
) -> str:
    document = await documents.get_by_artifact_key(key)
    if document is not None:
        if document.project_id is None or identity.allows_project(document.project_id):
            return document.media_type
        raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")

    if key.startswith(ENGINE_RUN_PREFIX):
        external_run_id = key.removeprefix(ENGINE_RUN_PREFIX).split("/", 1)[0]
        if not external_run_id:
            raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")
        allowed = None if identity.is_unscoped else identity.scoped_project_ids()
        row = await engine_runs.get_by_external_run_id(external_run_id, allowed_project_ids=allowed)
        if row is not None:
            return FALLBACK_MEDIA_TYPE
        raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")

    if key.startswith(TRANSCRIPT_PREFIX):
        thread_id = key.removeprefix(TRANSCRIPT_PREFIX).split("/", 1)[0]
        if not thread_id:
            raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")
        try:
            await loopback_client(extract_api_key(request.headers)).threads.get(thread_id)
        except NotFoundError:
            raise HTTPException(status_code=404, detail=f"artifact {key!r} not found") from None
        return "text/plain"

    raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")
