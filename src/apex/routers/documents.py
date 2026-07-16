"""/documents: upload context documents into the artifact store + metadata CRUD.

Upload deviation (documented in apex.services.documents): `python-multipart` is not
in the locked dependencies, so the route parses multipart/form-data from the raw
request stream instead of using UploadFile/Form. The wire contract is unchanged:
POST multipart with a `file` part and optional `project_id`/`app_id`/`summary` fields.

DELETE removes both the metadata row and its stored artifact.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.input_limits import (
    MAX_DB_LIST_OFFSET,
    MAX_SCOPE_ID_CHARS,
    NoNulStr,
    RecordId,
    ScopeId,
)
from apex.domain.pipeline import MAX_CONTEXT_SUMMARY_CHARS
from apex.persistence.db import get_session, get_sessionmaker, release_read_transactions
from apex.persistence.models import Document
from apex.persistence.repositories.catalog import CatalogRepository
from apex.persistence.repositories.documents import DocumentsRepository
from apex.services.connections import ConnectionResolver, close_adapter, get_connection_resolver
from apex.services.documents import (
    MAX_DOCUMENT_BYTES,
    MAX_UPLOAD_BODY_BYTES,
    DocumentsService,
    DocumentTooLargeError,
    InvalidDocumentFilenameError,
    MultipartParseError,
    acquire_document_upload_slot,
    await_task_definitively,
    extract_boundary,
    get_documents_repository,
    parse_multipart,
    purge_document_tombstone,
    read_body_capped,
    safe_filename,
)

router = APIRouter(prefix="/documents", tags=["documents"])
logger = structlog.get_logger(__name__)

RepositoryDep = Annotated[DocumentsRepository, Depends(get_documents_repository)]

DocumentRepositoryFactory = Callable[[], AbstractAsyncContextManager[DocumentsRepository]]


@asynccontextmanager
async def _open_document_repository() -> AsyncIterator[DocumentsRepository]:
    async with get_sessionmaker()() as session:
        yield DocumentsRepository(session)


def get_document_repository_factory() -> DocumentRepositoryFactory:
    return _open_document_repository


DocumentRepositoryFactoryDep = Annotated[
    DocumentRepositoryFactory,
    Depends(get_document_repository_factory),
]


def get_catalog_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CatalogRepository:
    return CatalogRepository(session)


CatalogRepo = Annotated[CatalogRepository, Depends(get_catalog_repository)]
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

    @field_validator("parse_error", mode="before")
    @classmethod
    def sanitize_legacy_parse_error(cls, value: Any) -> str | None:
        if value is None:
            return None
        return bounded_diagnostic(value, max_chars=500)


class DocumentListResponse(BaseModel):
    items: list[DocumentOut]
    limit: int
    offset: int


class DocumentArtifactAffinityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: NoNulStr = Field(min_length=1, max_length=32)


@dataclass(frozen=True)
class _LegacyAffinityCandidate:
    artifact_key: str
    artifact_connection_id: str | None
    project_id: str | None
    app_id: str | None
    upload_pending_at: datetime | None
    deletion_pending_at: datetime | None


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
                detail="consumer is not scoped to the requested project/application",
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
        raise HTTPException(
            status_code=403, detail="consumer is not scoped to the requested project"
        )
    raise HTTPException(
        status_code=422,
        detail="app_id is required when the consumer has multiple app scopes",
    )


def _upload_text(
    value: str | None,
    *,
    label: str,
    max_length: int,
    required: bool = False,
    forbid_controls: bool = False,
) -> str | None:
    normalized = (value or "").strip()
    if required and not normalized:
        raise HTTPException(status_code=422, detail=f"{label} must not be empty")
    if len(normalized) > max_length:
        raise HTTPException(
            status_code=422,
            detail=f"{label} must not exceed {max_length} characters",
        )
    if "\x00" in normalized:
        raise HTTPException(status_code=422, detail=f"{label} contains U+0000")
    if forbid_controls and any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise HTTPException(status_code=422, detail=f"{label} contains control characters")
    return normalized or None


def _legacy_affinity_candidate(document: Document) -> _LegacyAffinityCandidate:
    return _LegacyAffinityCandidate(
        artifact_key=document.artifact_key,
        artifact_connection_id=document.artifact_connection_id,
        project_id=document.project_id,
        app_id=document.app_id,
        upload_pending_at=document.upload_pending_at,
        deletion_pending_at=document.deletion_pending_at,
    )


async def _artifact_key_exists(store: Any, key: str) -> bool:
    """Prove an exact key exists without retaining the full artifact in memory."""

    try:
        iterator = store.iter_bytes(key).__aiter__()
    except (KeyError, FileNotFoundError):
        return False
    try:
        await anext(iterator)
    except StopAsyncIteration:
        # A successful empty stream still proves the provider found the object.
        return True
    except (KeyError, FileNotFoundError):
        return False
    finally:
        await close_adapter(iterator)
    return True


async def _close_artifact_store(store: Any, *, operation: str) -> bool:
    """Settle provider cleanup without letting diagnostics cross the HTTP boundary."""

    try:
        await close_adapter(store)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "documents.artifact_store_close_failed",
            operation=operation,
        )
        return False
    return True


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
    _upload_slot: Annotated[None, Depends(acquire_document_upload_slot)],
    identity: Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))],
    repository: RepositoryDep,
    resolver: ConnectionResolverDep,
    catalog: CatalogRepo,
) -> Any:
    boundary = extract_boundary(request.headers.get("content-type"))
    if boundary is None:
        raise HTTPException(status_code=415, detail="expected a multipart/form-data request")
    upload = None
    malformed_multipart = False
    upload_error: HTTPException | None = None
    try:
        body = await read_body_capped(request.stream(), MAX_UPLOAD_BODY_BYTES)
        # Parsing performs delimiter scans and copies the file slice. Keep it off
        # the event loop; admission above bounds retained bytes and rejects excess.
        parse_task = asyncio.create_task(
            asyncio.to_thread(parse_multipart, body, boundary),
            name="document-multipart-parse",
        )
        upload = await await_task_definitively(parse_task)
        del body
    except TimeoutError:
        upload_error = HTTPException(status_code=408, detail="document upload body timed out")
    except DocumentTooLargeError as exc:
        upload_error = HTTPException(
            status_code=413, detail=f"document exceeds the {exc.limit} byte limit"
        )
    except MultipartParseError:
        # Leave the exception handler before raising. Even ``from None`` keeps
        # the caught parser error in ``__context__``, where it could retain raw
        # multipart input for tracing/exception consumers.
        malformed_multipart = True
    if upload_error is not None:
        raise upload_error
    if malformed_multipart or upload is None:
        raise HTTPException(status_code=422, detail="malformed multipart body")
    if upload.file is None:
        raise HTTPException(status_code=422, detail="multipart body is missing a 'file' part")
    # Multipart framing has a small allowance beyond the document cap. Reject
    # an oversized file before tenant inference or artifact-store resolution.
    if len(upload.file.data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit",
        )

    # Validate every durable metadata field before catalog lookup or artifact
    # resolver/store I/O. This also keeps oversized legacy multipart text from
    # reaching String(255) columns as a database error.
    project_id = _upload_text(
        upload.fields.get("project_id"),
        label="project_id",
        max_length=MAX_SCOPE_ID_CHARS,
        forbid_controls=True,
    )
    app_id = _upload_text(
        upload.fields.get("app_id"),
        label="app_id",
        max_length=MAX_SCOPE_ID_CHARS,
        forbid_controls=True,
    )
    summary = _upload_text(
        upload.fields.get("summary"),
        label="summary",
        max_length=MAX_CONTEXT_SUMMARY_CHARS,
    )
    media_type = _upload_text(
        upload.file.content_type,
        label="document media type",
        max_length=255,
        required=True,
        forbid_controls=True,
    )
    assert media_type is not None
    filename: str | None = None
    invalid_filename = False
    try:
        filename = safe_filename(upload.file.filename)
    except InvalidDocumentFilenameError:
        invalid_filename = True
    if invalid_filename or filename is None:
        raise HTTPException(status_code=422, detail="invalid document filename")
    project_id, app_id = _resolve_upload_scope(
        identity,
        project_id=project_id,
        app_id=app_id,
    )
    if app_id is not None:
        application = await catalog.get_application(app_id)
        if (
            application is None
            or application.archived_at is not None
            or application.project_id != project_id
        ):
            raise HTTPException(status_code=422, detail="app_id is not valid for project_id")

    await release_read_transactions(catalog, repository)

    store: Any | None = None
    artifact_connection_id: str | None = None
    store_unavailable = False
    try:
        store, artifact_connection_id = await resolver.resolve_with_connection_id(
            PortKind.ARTIFACT_STORE,
            project_id=project_id,
        )
    except Exception:
        store_unavailable = True
    if (
        store_unavailable
        or store is None
        or type(artifact_connection_id) is not str
        or not 1 <= len(artifact_connection_id) <= 32
        or "\x00" in artifact_connection_id
    ):
        if store is not None:
            await _close_artifact_store(store, operation="upload_resolution")
        raise HTTPException(status_code=503, detail="artifact store unavailable")

    service = DocumentsService(repository, store)
    document = None
    service_failure: HTTPException | None = None
    try:
        try:
            document = await service.upload(
                filename=filename,
                content_type=media_type,
                data=upload.file.data,
                artifact_connection_id=artifact_connection_id,
                project_id=project_id,
                app_id=app_id,
                summary=summary,
                uploaded_by=identity.name,
            )
        except DocumentTooLargeError as exc:
            service_failure = HTTPException(
                status_code=413, detail=f"document exceeds the {exc.limit} byte limit"
            )
        except InvalidDocumentFilenameError:
            service_failure = HTTPException(status_code=422, detail="invalid document filename")
        except Exception:
            service_failure = HTTPException(status_code=503, detail="document upload failed")
    finally:
        await _close_artifact_store(store, operation="upload")
    if service_failure is not None:
        raise service_failure
    if document is None:  # pragma: no cover - service contract invariant
        raise HTTPException(status_code=503, detail="document upload returned no document")
    return document


@router.get("", operation_id="listDocuments", response_model=DocumentListResponse)
async def list_documents(
    identity: CurrentIdentity,
    repository: RepositoryDep,
    project: Annotated[ScopeId | None, Query()] = None,
    q: Annotated[NoNulStr | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> Any:
    ensure_scope(identity, project_id=project)
    allowed = None if identity.is_unscoped else identity.scopes
    documents = await repository.list(
        project=project, q=q, allowed_scopes=allowed, limit=limit, offset=offset
    )
    return {"items": documents, "limit": limit, "offset": offset}


@router.get("/{document_id}", operation_id="getDocument", response_model=DocumentOut)
async def get_document(
    document_id: Annotated[RecordId, Path()],
    identity: CurrentIdentity,
    repository: RepositoryDep,
) -> Any:
    document = await repository.get(document_id)
    if document is None or not _visible(identity, document):
        raise HTTPException(status_code=404, detail="document not found")
    return document


@router.put(
    "/{document_id}/artifact-connection",
    operation_id="assignDocumentArtifactConnection",
    status_code=204,
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def assign_document_artifact_connection(
    document_id: Annotated[RecordId, Path()],
    body: DocumentArtifactAffinityUpdate,
    identity: CurrentIdentity,
    repository_factory: DocumentRepositoryFactoryDep,
    resolver: ConnectionResolverDep,
) -> None:
    """One-time store-affinity repair for documents created before migration 0013."""

    # Snapshot and close the first session before touching the provider. The
    # second phase re-locks and rejects any row change made during verification.
    async with repository_factory() as repository:
        document = await repository.get_any(document_id)
        if document is None or not _writable(identity, document):
            raise HTTPException(status_code=404, detail="document not found")
        if document.upload_pending_at is not None and document.deletion_pending_at is None:
            raise HTTPException(
                status_code=409,
                detail="cannot change affinity while a document upload is active",
            )
        if document.artifact_connection_id is not None:
            if document.artifact_connection_id == body.connection_id:
                return
            raise HTTPException(
                status_code=409,
                detail="document artifact-store affinity is already fixed",
            )
        candidate = _legacy_affinity_candidate(document)

    store: Any | None = None
    exists = False
    store_unavailable = False
    affinity_mismatch = False
    try:
        store, resolved_connection_id = await resolver.resolve_with_connection_id(
            PortKind.ARTIFACT_STORE,
            connection_id=body.connection_id,
            project_id=candidate.project_id,
        )
        if type(resolved_connection_id) is not str or resolved_connection_id != body.connection_id:
            affinity_mismatch = True
        else:
            exists = await _artifact_key_exists(store, candidate.artifact_key)
    except Exception:
        store_unavailable = True
    finally:
        if store is not None and not await _close_artifact_store(
            store,
            operation="affinity_verification",
        ):
            store_unavailable = True
    if affinity_mismatch:
        raise HTTPException(
            status_code=409,
            detail="resolver did not select the requested artifact-store connection",
        )
    if store_unavailable:
        raise HTTPException(status_code=503, detail="artifact store unavailable")
    if not exists:
        raise HTTPException(
            status_code=409,
            detail="document artifact does not exist in the requested store",
        )

    async with repository_factory() as repository:
        document = await repository.get_any_for_update(document_id)
        if document is None or not _writable(identity, document):
            raise HTTPException(status_code=404, detail="document not found")
        if _legacy_affinity_candidate(document) != candidate:
            raise HTTPException(
                status_code=409,
                detail="document changed while artifact-store affinity was being verified",
            )
        affinity_failure: HTTPException | None = None
        try:
            await repository.assign_artifact_connection(document, body.connection_id)
        except ValueError:
            affinity_failure = HTTPException(
                status_code=409, detail="document affinity update conflict"
            )
        except RuntimeError:
            affinity_failure = HTTPException(status_code=503, detail="artifact store unavailable")
        if affinity_failure is not None:
            raise affinity_failure


@router.delete(
    "/{document_id}",
    operation_id="deleteDocument",
    status_code=204,
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def delete_document(
    document_id: Annotated[RecordId, Path()],
    identity: CurrentIdentity,
    repository: RepositoryDep,
    resolver: ConnectionResolverDep,
) -> None:
    document = await repository.get(document_id)
    if document is None or not _writable(identity, document):
        raise HTTPException(status_code=404, detail="document not found")
    persistence_failed = False
    try:
        # The committed tombstone hides metadata and retains store affinity
        # before any irreversible object deletion. A background reconciler can
        # safely resume every failure/cancellation point after this write.
        await repository.mark_deletion_pending(document)
    except Exception:
        persistence_failed = True
    if persistence_failed:
        raise HTTPException(status_code=503, detail="could not persist document deletion")
    purge_failed = False
    try:
        await purge_document_tombstone(document, repository, resolver)
    except Exception:
        purge_failed = True
    if purge_failed:
        raise HTTPException(
            status_code=503,
            detail="document deletion is queued for retry",
        )
