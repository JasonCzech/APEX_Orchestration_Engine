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

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
from apex.persistence.models import Document
from apex.persistence.repositories.documents import DocumentsRepository
from apex.services.connections import ConnectionResolver, get_connection_resolver
from apex.services.documents import (
    MAX_DOCUMENT_BYTES,
    MAX_UPLOAD_BODY_BYTES,
    DocumentsService,
    DocumentTooLargeError,
    MultipartParseError,
    extract_boundary,
    get_documents_repository,
    parse_multipart,
    read_body_capped,
)

router = APIRouter(prefix="/documents", tags=["documents"])

RepositoryDep = Annotated[DocumentsRepository, Depends(get_documents_repository)]
ConnectionResolverDep = Annotated[ConnectionResolver, Depends(get_connection_resolver)]


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
    parse_status: str | None = None
    extracted_chars: int | None = None
    parse_error: str | None = None
    text_preview: str | None = None


class DocumentListResponse(BaseModel):
    items: list[DocumentOut]
    limit: int
    offset: int


# ── Helpers ──────────────────────────────────────────────────────────────────


def _visible(identity: ConsumerIdentity, document: Document) -> bool:
    """Global rows (project_id NULL) are visible to everyone; scoped rows need scope."""
    return document.project_id is None or identity.allows_scope(
        project_id=document.project_id, app_id=document.app_id
    )


def _writable(identity: ConsumerIdentity, document: Document) -> bool:
    if document.project_id is None:
        return identity.is_unscoped and identity.role.at_least(Role.ADMIN)
    return identity.contains_scope(ScopeRef(project_id=document.project_id, app_id=document.app_id))


def _resolve_upload_scope(
    identity: ConsumerIdentity,
    *,
    project_id: str | None,
    app_id: str | None,
) -> tuple[str | None, str | None]:
    """Prevent a scoped writer from creating a broader document than it owns."""

    if app_id is not None and project_id is None:
        raise HTTPException(status_code=422, detail="app_id requires project_id")
    if identity.is_unscoped:
        return project_id, app_id

    if project_id is None:
        projects = identity.scoped_project_ids()
        if len(projects) != 1:
            raise HTTPException(
                status_code=422,
                detail="project_id is required when the consumer has multiple project scopes",
            )
        project_id = projects[0]

    if app_id is not None:
        if not identity.contains_scope(ScopeRef(project_id=project_id, app_id=app_id)):
            raise HTTPException(
                status_code=403,
                detail=f"consumer is not scoped to project {project_id!r}, app {app_id!r}",
            )
        return project_id, app_id

    project_scope = ScopeRef(project_id=project_id)
    if identity.contains_scope(project_scope):
        return project_id, None
    apps = tuple(
        dict.fromkeys(
            scope.app_id
            for scope in identity.scopes
            if scope.project_id == project_id and scope.app_id is not None
        )
    )
    if len(apps) == 1:
        return project_id, apps[0]
    if not apps:
        raise HTTPException(status_code=403, detail=f"consumer is not scoped to {project_id!r}")
    raise HTTPException(
        status_code=422,
        detail="app_id is required when the consumer has multiple app scopes",
    )


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
    resolver: ConnectionResolverDep,
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
    # Multipart framing has a small allowance beyond the document cap. Reject
    # an oversized file before tenant inference or artifact-store resolution.
    if len(upload.file.data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit",
        )

    project_id = (upload.fields.get("project_id") or "").strip() or None
    app_id = (upload.fields.get("app_id") or "").strip() or None
    project_id, app_id = _resolve_upload_scope(
        identity,
        project_id=project_id,
        app_id=app_id,
    )

    try:
        store, artifact_connection_id = await resolver.resolve_with_connection_id(
            PortKind.ARTIFACT_STORE,
            project_id=project_id,
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="artifact store unavailable") from exc

    service = DocumentsService(repository, store)
    try:
        document = await service.upload(
            filename=upload.file.filename,
            content_type=upload.file.content_type,
            data=upload.file.data,
            artifact_connection_id=artifact_connection_id,
            project_id=project_id,
            app_id=app_id,
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
    ensure_scope(identity, project_id=project)
    allowed = None if identity.is_unscoped else identity.scopes
    documents = await repository.list(
        project=project, q=q, allowed_scopes=allowed, limit=limit, offset=offset
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
    if document is None or not _writable(identity, document):
        raise HTTPException(status_code=404, detail=f"document {document_id!r} not found")
    # Metadata row only; artifact bytes are left for a future GC pass (out of scope).
    await repository.delete(document)
