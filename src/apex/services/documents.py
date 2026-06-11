"""Document upload/storage service + minimal multipart parsing.

Deviation note: FastAPI's UploadFile/Form requires the `python-multipart` package,
which is NOT in this project's locked dependencies (pyproject is frozen for M2).
The upload route therefore reads the raw request stream and this module parses
`multipart/form-data` itself. The parser handles the well-formed CRLF multipart
that browsers/httpx produce: one file part + simple text fields. Swap back to
UploadFile if python-multipart ever lands in the lockfile.
"""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from posixpath import basename
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import PortKind
from apex.persistence.db import get_session
from apex.persistence.models import Document
from apex.persistence.repositories.documents import DocumentsRepository
from apex.ports.artifact_store import ArtifactStorePort
from apex.services.connections import get_connection_resolver

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024  # 25 MB hard cap per uploaded document
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
        await self._store.put(key, data, content_type=content_type)
        document = Document(
            id=document_id,
            name=name,
            media_type=content_type,
            size_bytes=len(data),  # actual bytes read, not the client's claim
            artifact_key=key,
            project_id=project_id,
            app_id=app_id,
            summary=summary,
            uploaded_by=uploaded_by,
        )
        return await self._repository.add(document)


# ── FastAPI dependency providers (shared by /documents and /artifacts) ──────


async def get_artifact_store() -> ArtifactStorePort:
    """Default artifact store via the connection resolver; override in tests."""
    store = await get_connection_resolver().resolve(PortKind.ARTIFACT_STORE)
    return store


def get_documents_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentsRepository:
    return DocumentsRepository(session)
