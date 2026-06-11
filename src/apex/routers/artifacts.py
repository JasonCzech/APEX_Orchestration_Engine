"""/artifacts: same-origin authenticated byte proxy for dashboards and iframes.

Streams artifact-store objects so the dashboard never needs direct store
credentials. Content type comes from the Document row when the key belongs to an
uploaded document; transcript keys ("transcripts/...") are text/plain; everything
else falls back to application/octet-stream.

The ArtifactStorePort returns whole byte payloads (no ranged reads in the port
yet), so the response chunks an in-memory buffer — fine for M2-scale artifacts;
a ranged/streaming port method is an M3 concern alongside the S3 adapter.
"""

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from apex.app.dependencies import CurrentIdentity
from apex.persistence.repositories.documents import DocumentsRepository
from apex.services.documents import get_artifact_store, get_documents_repository

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

RepositoryDep = Annotated[DocumentsRepository, Depends(get_documents_repository)]
ArtifactStoreDep = Annotated[Any, Depends(get_artifact_store)]

_CHUNK_SIZE = 64 * 1024
FALLBACK_MEDIA_TYPE = "application/octet-stream"
TRANSCRIPT_PREFIX = "transcripts/"


def _chunked(data: bytes, size: int = _CHUNK_SIZE) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


@router.get("/{key:path}", operation_id="getArtifact")
async def get_artifact(
    key: str,
    identity: CurrentIdentity,
    store: ArtifactStoreDep,
    repository: RepositoryDep,
) -> StreamingResponse:
    try:
        data = await store.get(key)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail=f"artifact {key!r} not found") from None

    media_type = FALLBACK_MEDIA_TYPE
    document = await repository.get_by_artifact_key(key)
    if document is not None:
        # Document-backed artifacts inherit row scoping: pretend absent when out of scope.
        if document.project_id is not None and not identity.allows_project(document.project_id):
            raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")
        media_type = document.media_type
    elif key.startswith(TRANSCRIPT_PREFIX):
        media_type = "text/plain"

    return StreamingResponse(_chunked(data), media_type=media_type)
