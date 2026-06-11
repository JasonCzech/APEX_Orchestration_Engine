"""/documents: upload context documents into the artifact store + metadata CRUD.

Upload deviation (documented in apex.services.documents): `python-multipart` is not
in the locked dependencies, so the route parses multipart/form-data from the raw
request stream instead of using UploadFile/Form. The wire contract is unchanged:
POST multipart with a `file` part and optional `project_id`/`app_id`/`summary` fields.

DELETE removes the metadata row only — artifact-store garbage collection is out of
scope for M2 (the orphaned object stays in the store until an M3+ GC pass).
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from apex.app.dependencies import CurrentIdentity, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.persistence.models import Document
from apex.persistence.repositories.documents import DocumentsRepository
from apex.services.documents import (
    MAX_UPLOAD_BODY_BYTES,
    DocumentsService,
    DocumentTooLargeError,
    MultipartParseError,
    extract_boundary,
    get_artifact_store,
    get_documents_repository,
    parse_multipart,
    read_body_capped,
)

router = APIRouter(prefix="/documents", tags=["documents"])

RepositoryDep = Annotated[DocumentsRepository, Depends(get_documents_repository)]
ArtifactStoreDep = Annotated[Any, Depends(get_artifact_store)]


# ── Schemas ──────────────────────────────────────────────────────────────────


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    media_type: str
    size_bytes: int
    artifact_key: str
    project_id: str | None = None
    app_id: str | None = None
    summary: str | None = None
    uploaded_by: str | None = None
    created_at: datetime | None = None


class DocumentListResponse(BaseModel):
    items: list[DocumentOut]
    limit: int
    offset: int


# ── Helpers ──────────────────────────────────────────────────────────────────


def _visible(identity: ConsumerIdentity, document: Document) -> bool:
    """Global rows (project_id NULL) are visible to everyone; scoped rows need scope."""
    return document.project_id is None or identity.allows_project(document.project_id)


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post(
    "",
    operation_id="uploadDocument",
    status_code=201,
    response_model=DocumentOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "project_id": {"type": "string"},
                            "app_id": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                    }
                }
            },
        }
    },
)
async def upload_document(
    request: Request,
    identity: Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))],
    repository: RepositoryDep,
    store: ArtifactStoreDep,
) -> Any:
    boundary = extract_boundary(request.headers.get("content-type"))
    if boundary is None:
        raise HTTPException(status_code=415, detail="expected a multipart/form-data request")
    try:
        body = await read_body_capped(request.stream(), MAX_UPLOAD_BODY_BYTES)
        upload = parse_multipart(body, boundary)
    except DocumentTooLargeError as exc:
        raise HTTPException(
            status_code=413, detail=f"document exceeds the {exc.limit} byte limit"
        ) from exc
    except MultipartParseError as exc:
        raise HTTPException(status_code=422, detail=f"malformed multipart body: {exc}") from exc
    if upload.file is None:
        raise HTTPException(status_code=422, detail="multipart body is missing a 'file' part")

    project_id = upload.fields.get("project_id") or None
    if project_id is not None and not identity.allows_project(project_id):
        raise HTTPException(
            status_code=403, detail=f"consumer is not scoped to project {project_id!r}"
        )

    service = DocumentsService(repository, store)
    try:
        document = await service.upload(
            filename=upload.file.filename,
            content_type=upload.file.content_type,
            data=upload.file.data,
            project_id=project_id,
            app_id=upload.fields.get("app_id") or None,
            summary=upload.fields.get("summary") or None,
            uploaded_by=identity.name,
        )
    except DocumentTooLargeError as exc:
        raise HTTPException(
            status_code=413, detail=f"document exceeds the {exc.limit} byte limit"
        ) from exc
    return document


@router.get("", operation_id="listDocuments", response_model=DocumentListResponse)
async def list_documents(
    identity: CurrentIdentity,
    repository: RepositoryDep,
    project: str | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    allowed = None if identity.is_unscoped else identity.scoped_project_ids()
    documents = await repository.list(
        project=project, q=q, allowed_project_ids=allowed, limit=limit, offset=offset
    )
    return {"items": documents, "limit": limit, "offset": offset}


@router.get("/{document_id}", operation_id="getDocument", response_model=DocumentOut)
async def get_document(
    document_id: str, identity: CurrentIdentity, repository: RepositoryDep
) -> Any:
    document = await repository.get(document_id)
    if document is None or not _visible(identity, document):
        raise HTTPException(status_code=404, detail=f"document {document_id!r} not found")
    return document


@router.delete(
    "/{document_id}",
    operation_id="deleteDocument",
    status_code=204,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def delete_document(
    document_id: str, identity: CurrentIdentity, repository: RepositoryDep
) -> None:
    document = await repository.get(document_id)
    if document is None or not _visible(identity, document):
        raise HTTPException(status_code=404, detail=f"document {document_id!r} not found")
    # Metadata row only; artifact bytes are left for a future GC pass (out of scope).
    await repository.delete(document)
