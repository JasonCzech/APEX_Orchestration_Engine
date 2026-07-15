"""Document upload/storage service + minimal multipart parsing.

Deviation note: FastAPI's UploadFile/Form requires the `python-multipart` package,
which is NOT in this project's locked dependencies (pyproject is frozen for M2).
The upload route therefore reads the raw request stream and this module parses
`multipart/form-data` itself. The parser handles the well-formed CRLF multipart
that browsers/httpx produce: one file part + simple text fields. Swap back to
UploadFile if python-multipart ever lands in the lockfile.
"""

import asyncio
import re
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from posixpath import basename
from typing import Annotated, Any
from uuid import uuid4

import structlog
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import PortKind
from apex.auth.identity import ConsumerIdentity
from apex.persistence.db import get_session
from apex.persistence.models import Document
from apex.persistence.repositories.documents import DocumentsRepository
from apex.ports.artifact_store import ArtifactStorePort
from apex.services.connections import get_connection_resolver
from apex.services.text_extraction import (
    PARSE_PARSED,
    ExtractionResult,
    derive_summary,
    extract_text,
)
from apex.settings import get_settings

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024  # 25 MB hard cap per uploaded document
logger = structlog.get_logger(__name__)
_EXTRACTION_LIMITER = threading.BoundedSemaphore(4)


def _extract_text_bounded(*args: Any, **kwargs: Any) -> ExtractionResult:
    with _EXTRACTION_LIMITER:
        return extract_text(*args, **kwargs)


# Stream cap: file cap + slack for multipart framing and small text fields.
MAX_UPLOAD_BODY_BYTES = MAX_DOCUMENT_BYTES + 1024 * 1024

_BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)
_NAME_RE = re.compile(r'\bname="([^"]*)"')
_FILENAME_RE = re.compile(r'\bfilename="([^"]*)"')


class DocumentTooLargeError(Exception):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"upload exceeds the {limit} byte limit")


class MultipartParseError(Exception):
    pass


class DocumentContextNotFoundError(LookupError):
    """An uploaded document is missing or outside the caller's exact scope."""

    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
        super().__init__(f"document {document_id!r} not found")


@dataclass
class UploadedFilePart:
    filename: str
    content_type: str
    data: bytes


@dataclass
class ParsedUpload:
    file: UploadedFilePart | None
    fields: dict[str, str] = field(default_factory=dict)


def extract_boundary(content_type: str | None) -> str | None:
    """Boundary token from a multipart/form-data Content-Type header, else None."""
    if not content_type or "multipart/form-data" not in content_type.lower():
        return None
    match = _BOUNDARY_RE.search(content_type)
    return match.group(1) if match else None


async def read_body_capped(stream: AsyncIterator[bytes], cap: int) -> bytes:
    """Accumulate the request stream, raising DocumentTooLargeError past `cap`."""
    buffer = bytearray()
    async for chunk in stream:
        buffer.extend(chunk)
        if len(buffer) > cap:
            raise DocumentTooLargeError(MAX_DOCUMENT_BYTES)
    return bytes(buffer)


def parse_multipart(body: bytes, boundary: str) -> ParsedUpload:
    """Parse a well-formed multipart/form-data body: first file part + text fields."""
    delimiter = b"--" + boundary.encode("latin-1")
    sections = body.split(delimiter)
    if len(sections) < 2:
        raise MultipartParseError("no multipart sections found")
    parsed = ParsedUpload(file=None)
    for section in sections[1:]:
        if section.startswith(b"--"):  # closing delimiter
            break
        section = section.removeprefix(b"\r\n")
        header_blob, sep, content = section.partition(b"\r\n\r\n")
        if not sep:
            raise MultipartParseError("malformed part: missing header/body separator")
        content = content.removesuffix(b"\r\n")
        headers = _parse_part_headers(header_blob)
        disposition = headers.get("content-disposition", "")
        name_match = _NAME_RE.search(disposition)
        filename_match = _FILENAME_RE.search(disposition)
        if filename_match is not None:
            if parsed.file is None:
                parsed.file = UploadedFilePart(
                    filename=filename_match.group(1),
                    content_type=headers.get("content-type") or "application/octet-stream",
                    data=content,
                )
        elif name_match is not None:
            parsed.fields[name_match.group(1)] = content.decode("utf-8", errors="replace")
    return parsed


def _parse_part_headers(blob: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in blob.split(b"\r\n"):
        name, sep, value = line.partition(b":")
        if sep:
            headers[name.strip().lower().decode("latin-1")] = value.strip().decode("latin-1")
    return headers


def safe_filename(filename: str | None) -> str:
    """Strip any path components; never let an upload pick its own key prefix."""
    cleaned = basename((filename or "").replace("\\", "/")).strip()
    return cleaned or "upload.bin"


def document_artifact_key(document_id: str, filename: str) -> str:
    return f"documents/{document_id}/{filename}"


async def uploaded_document_context_packets(
    repository: DocumentsRepository,
    identity: ConsumerIdentity,
    document_ids: list[str],
) -> list[dict[str, Any]]:
    """Authorize uploaded metadata and convert extracted text to bounded evidence."""

    per_doc_cap = get_settings().documents.max_context_chars_per_doc
    packets: list[dict[str, Any]] = []
    for document_id in document_ids:
        document = await repository.get(document_id)
        if document is None or not (
            document.project_id is None
            or identity.allows_scope(project_id=document.project_id, app_id=document.app_id)
        ):
            # Deliberately indistinguishable: sibling scope must not learn that the
            # document exists.
            raise DocumentContextNotFoundError(document_id)
        text = document.extracted_text or None
        if text and len(text) > per_doc_cap:
            text = text[:per_doc_cap].rstrip() + "\n\n…[truncated]"
        packets.append(
            {
                "id": f"document-{document.id}",
                "source": "document",
                "title": document.name,
                "summary": document.summary,
                "ref": f"/v1/artifacts/{document.artifact_key}",
                "text": text,
            }
        )
    return packets


class DocumentsService:
    """Streams bytes into the artifact store and records Document metadata."""

    def __init__(self, repository: Any, store: ArtifactStorePort) -> None:
        # `repository` is duck-typed (DocumentsRepository or an in-memory fake).
        self._repository = repository
        self._store = store

    async def upload(
        self,
        *,
        filename: str,
        content_type: str,
        data: bytes,
        artifact_connection_id: str,
        project_id: str | None,
        app_id: str | None,
        summary: str | None,
        uploaded_by: str | None,
    ) -> Document:
        if len(data) > MAX_DOCUMENT_BYTES:
            raise DocumentTooLargeError(MAX_DOCUMENT_BYTES)
        document_id = uuid4().hex
        name = safe_filename(filename)
        key = document_artifact_key(document_id, name)
        stored = False
        try:
            await self._store.put(key, data, content_type=content_type)
            stored = True

            # Parse the bytes into text the agent can actually read. Off-thread because
            # pypdf/python-docx are blocking; soft-fails (status set) never break the upload.
            ingest = get_settings().documents
            extraction = await asyncio.to_thread(
                _extract_text_bounded,
                data,
                filename=name,
                content_type=content_type,
                max_chars=ingest.max_extract_chars,
            )
            if not summary and extraction.status == PARSE_PARSED and extraction.text:
                summary = derive_summary(extraction.text, max_chars=ingest.summary_chars)

            document = Document(
                id=document_id,
                name=name,
                media_type=content_type,
                size_bytes=len(data),  # actual bytes read, not the client's claim
                artifact_key=key,
                artifact_connection_id=artifact_connection_id,
                project_id=project_id,
                app_id=app_id,
                summary=summary,
                uploaded_by=uploaded_by,
                extracted_text=extraction.text or None,
                extracted_chars=extraction.char_count,
                parse_status=extraction.status,
                parse_error=extraction.error,
            )
            return await self._repository.add(document)
        except BaseException:
            try:
                delete = getattr(self._store, "delete", None)
                if stored and delete is not None:
                    await delete(key)
            except BaseException:
                logger.warning("documents.orphan_cleanup_failed", key=key, exc_info=True)
            raise


# ── FastAPI dependency providers (shared by /documents and /artifacts) ──────


async def get_artifact_store() -> ArtifactStorePort:
    """Default artifact store via the connection resolver; override in tests."""
    store = await get_connection_resolver().resolve(PortKind.ARTIFACT_STORE)
    return store


def get_documents_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentsRepository:
    return DocumentsRepository(session)
