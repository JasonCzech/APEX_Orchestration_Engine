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
import unicodedata
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from posixpath import basename
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4
from weakref import WeakKeyDictionary

import structlog
from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import PortKind
from apex.auth.identity import ConsumerIdentity
from apex.domain.diagnostics import contains_credential_material, safe_type_name
from apex.domain.pipeline import MAX_CONTEXT_SUMMARY_CHARS, MAX_CONTEXT_TEXT_CHARS
from apex.persistence.db import get_session, get_sessionmaker
from apex.persistence.models import Document
from apex.persistence.repositories.documents import DocumentsRepository, sanitize_document_text
from apex.ports.artifact_store import ArtifactStorePort, validate_stored_artifact_ack
from apex.services.connections import close_adapter, get_connection_resolver
from apex.services.text_extraction import (
    PARSE_PARSED,
    ExtractionResult,
    derive_summary,
    extract_text,
)
from apex.settings import get_settings

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024  # 25 MB hard cap per uploaded document
logger = structlog.get_logger(__name__)
MAX_CONCURRENT_DOCUMENT_UPLOADS = 4
DOCUMENT_UPLOAD_BODY_TIMEOUT_S = 120.0
MAX_MULTIPART_PARTS = 4
MAX_MULTIPART_PART_HEADER_BYTES = 8 * 1024
MAX_MULTIPART_BOUNDARY_CHARS = 70
_MULTIPART_TEXT_FIELD_BYTE_LIMITS = {
    "project_id": 4 * 255,
    "app_id": 4 * 255,
    "summary": 4 * MAX_CONTEXT_SUMMARY_CHARS,
}
_LIMITERS_LOCK = threading.Lock()
_EXTRACTION_LIMITERS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    WeakKeyDictionary()
)
_UPLOAD_ADMISSION_LOCK = threading.Lock()
_ACTIVE_DOCUMENT_UPLOADS = 0
DOCUMENT_DELETION_RETRY_INTERVAL_S = 30.0
STALE_DOCUMENT_UPLOAD_AGE = timedelta(hours=1)
DOCUMENT_UPLOAD_LEASE_RENEW_INTERVAL_S = 5 * 60.0


def _safe_connection_id(value: Any) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 255
        and value == value.strip()
        and "\x00" not in value
        and not contains_credential_material(value)
    )


def _extract_text_bounded(*args: Any, **kwargs: Any) -> ExtractionResult:
    return extract_text(*args, **kwargs)


async def await_task_definitively[TaskResult](
    task: asyncio.Task[TaskResult],
) -> TaskResult:
    """On caller cancellation, wait until non-cancellable worker IO is truly done.

    `asyncio.to_thread` cancellation only detaches the awaiting coroutine; it
    cannot stop the underlying thread. Repeatedly shield the worker, including
    from repeated cancellation requests, so memory and concurrency permits are
    not released while that thread still owns its inputs.
    """

    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    break
                continue
            except BaseException:
                break
        if task.done():
            try:
                task.result()
            except BaseException:
                pass
        raise


async def cancel_task_definitively(task: asyncio.Task[Any]) -> None:
    """Cancel one owned task, settle its cleanup, and preserve caller cancellation."""

    if not task.done():
        task.cancel()
    interrupted = False
    current = asyncio.current_task()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if current is not None and current.cancelling():
                interrupted = True
            if task.done():
                break
            continue
        except BaseException:
            break
    if task.done():
        try:
            task.result()
        except BaseException:
            pass
    if interrupted:
        raise asyncio.CancelledError from None


def _loop_limiter(
    registry: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore],
) -> asyncio.Semaphore:
    """Return a loop-local limiter without binding one semaphore across test/worker loops."""

    loop = asyncio.get_running_loop()
    with _LIMITERS_LOCK:
        limiter = registry.get(loop)
        if limiter is None:
            limiter = asyncio.Semaphore(MAX_CONCURRENT_DOCUMENT_UPLOADS)
            registry[loop] = limiter
        return limiter


@asynccontextmanager
async def document_upload_admission() -> AsyncIterator[None]:
    """Fail-fast process admission before an upload retains body or provider state."""

    global _ACTIVE_DOCUMENT_UPLOADS
    with _UPLOAD_ADMISSION_LOCK:
        if _ACTIVE_DOCUMENT_UPLOADS >= MAX_CONCURRENT_DOCUMENT_UPLOADS:
            raise DocumentUploadBusyError
        _ACTIVE_DOCUMENT_UPLOADS += 1
    try:
        yield
    finally:
        with _UPLOAD_ADMISSION_LOCK:
            _ACTIVE_DOCUMENT_UPLOADS -= 1


async def acquire_document_upload_slot() -> AsyncIterator[None]:
    """FastAPI yield dependency holding admission through upload finalization."""

    busy = False
    try:
        async with document_upload_admission():
            yield
    except DocumentUploadBusyError:
        busy = True
    if busy:
        raise HTTPException(
            status_code=503,
            detail="document upload capacity is busy; retry shortly",
            headers={"Retry-After": "1"},
        )


# Stream cap: file cap + slack for multipart framing and small text fields.
MAX_UPLOAD_BODY_BYTES = MAX_DOCUMENT_BYTES + 1024 * 1024
MAX_DOCUMENT_FILENAME_CHARS = 255
MAX_DOCUMENT_FILENAME_BYTES = 512

_MULTIPART_CONTENT_TYPE_RE = re.compile(
    r'\Amultipart/form-data\s*;\s*boundary=(?:"([^"\r\n]+)"|([^;"\s]+))\s*\Z',
    re.IGNORECASE,
)
_DISPOSITION_PARAM_RE = re.compile(
    r'\s*;\s*([!#$%&\'*+.^_`|~0-9A-Za-z-]+)\s*=\s*"((?:[^"\\\r\n]|\\[^\r\n])*)"'
)


class DocumentTooLargeError(Exception):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"upload exceeds the {limit} byte limit")


class DocumentUploadBusyError(RuntimeError):
    """All process upload slots are active; callers must retry without queueing."""


class MultipartParseError(Exception):
    pass


class InvalidDocumentFilenameError(ValueError):
    pass


class DocumentContextNotFoundError(LookupError):
    """An uploaded document is missing or outside the caller's exact scope."""

    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
        super().__init__(f"document {document_id!r} not found")


class UnknownDocumentArtifactAffinityError(RuntimeError):
    """A legacy durable document has not been mapped to its original store."""


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
    if type(content_type) is not str or not 1 <= len(content_type) <= 512:
        return None
    match = _MULTIPART_CONTENT_TYPE_RE.fullmatch(content_type)
    if match is None:
        return None
    raw_boundary = match.group(1) or match.group(2)
    boundary = raw_boundary.strip()
    if boundary != raw_boundary:
        return None
    if not 1 <= len(boundary) <= MAX_MULTIPART_BOUNDARY_CHARS:
        return None
    if any(unicodedata.category(char).startswith("C") for char in boundary):
        return None
    return boundary


async def read_body_capped(
    stream: AsyncIterator[bytes],
    cap: int,
    *,
    timeout_s: float = DOCUMENT_UPLOAD_BODY_TIMEOUT_S,
) -> bytes:
    """Accumulate one request stream under aggregate byte and wall-time limits."""
    buffer = bytearray()
    # A slow/stalled client must not hold one of the process-wide upload slots
    # forever. The deadline covers the full body, not just individual chunks,
    # so drip-fed requests cannot continually reset it.
    async with asyncio.timeout(timeout_s):
        async for chunk in stream:
            # Check before copying so one oversized ASGI frame cannot make the
            # supposedly hard cap itself allocate attacker-controlled memory.
            if len(chunk) > cap - len(buffer):
                raise DocumentTooLargeError(MAX_DOCUMENT_BYTES)
            buffer.extend(chunk)
    return bytes(buffer)


def parse_multipart(body: bytes, boundary: str) -> ParsedUpload:
    """Parse a well-formed multipart/form-data body: first file part + text fields."""
    try:
        delimiter: bytes | None = b"--" + boundary.encode("latin-1")
    except UnicodeEncodeError:
        # UnicodeEncodeError retains the complete caller-owned boundary on
        # ``.object``.  Detach it before surfacing the stable parser error.
        delimiter = None
    if delimiter is None:
        raise MultipartParseError("multipart boundary is not latin-1")
    if not body.startswith(delimiter):
        raise MultipartParseError("no multipart sections found")

    parsed = ParsedUpload(file=None)
    cursor = len(delimiter)
    part_count = 0
    while True:
        if body[cursor : cursor + 2] == b"--":
            trailer = body[cursor + 2 :]
            if trailer not in {b"", b"\r\n"}:
                raise MultipartParseError("malformed closing multipart boundary")
            break
        if body[cursor : cursor + 2] != b"\r\n":
            raise MultipartParseError("malformed multipart boundary")
        header_start = cursor + 2
        header_end = body.find(b"\r\n\r\n", header_start)
        if header_end < 0:
            raise MultipartParseError("malformed part: missing header/body separator")
        content_start = header_end + 4
        boundary_start = _next_multipart_boundary(body, delimiter, content_start)
        if boundary_start < 0:
            raise MultipartParseError("multipart body is missing its closing boundary")
        header_blob = body[header_start:header_end]
        part_count += 1
        if part_count > MAX_MULTIPART_PARTS:
            raise MultipartParseError(
                f"multipart body contains more than {MAX_MULTIPART_PARTS} parts"
            )
        if len(header_blob) > MAX_MULTIPART_PART_HEADER_BYTES:
            raise MultipartParseError("multipart part headers are too large")
        content = body[content_start:boundary_start]
        headers = _parse_part_headers(header_blob)
        disposition = headers.get("content-disposition")
        if disposition is None:
            raise MultipartParseError("multipart part is missing Content-Disposition")
        field_name, filename = _parse_content_disposition(disposition)
        if filename is not None:
            if field_name != "file":
                raise MultipartParseError(f"unsupported multipart file field {field_name!r}")
            if parsed.file is not None:
                raise MultipartParseError("multipart body contains duplicate file parts")
            parsed.file = UploadedFilePart(
                filename=filename,
                content_type=headers.get("content-type") or "application/octet-stream",
                data=content,
            )
        else:
            limit = _MULTIPART_TEXT_FIELD_BYTE_LIMITS.get(field_name)
            if limit is None:
                raise MultipartParseError(f"unsupported multipart field {field_name!r}")
            if field_name in parsed.fields:
                raise MultipartParseError(f"multipart field {field_name!r} is duplicated")
            if len(content) > limit:
                raise MultipartParseError(f"multipart field {field_name!r} is too large")
            try:
                decoded: str | None = content.decode("utf-8")
            except UnicodeDecodeError:
                # UnicodeDecodeError retains raw multipart bytes.  Raise after
                # leaving the handler so the parser error has no raw context.
                decoded = None
            if decoded is None:
                raise MultipartParseError(f"multipart field {field_name!r} is not valid UTF-8")
            parsed.fields[field_name] = decoded
        # Skip the CRLF that introduced the delimiter. The next iteration validates
        # whether this is another part or the closing `--` marker.
        cursor = boundary_start + 2 + len(delimiter)
    return parsed


def _next_multipart_boundary(body: bytes, delimiter: bytes, start: int) -> int:
    """Return the next syntactically valid CRLF-delimited boundary position."""

    marker = b"\r\n" + delimiter
    cursor = start
    while True:
        found = body.find(marker, cursor)
        if found < 0:
            return -1
        suffix = body[found + len(marker) : found + len(marker) + 2]
        if suffix in {b"\r\n", b"--"}:
            return found
        # Boundary-like bytes inside a binary part are payload unless the token is
        # followed by the exact multipart continuation/closing grammar.
        cursor = found + len(marker)


def _parse_part_headers(blob: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in blob.split(b"\r\n"):
        name, sep, value = line.partition(b":")
        if (
            not sep
            or not name
            or name != name.strip()
            or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
        ):
            raise MultipartParseError("multipart part contains a malformed header")
        normalized = name.lower().decode("ascii")
        if normalized in headers:
            raise MultipartParseError(f"duplicate multipart header {normalized!r}")
        headers[normalized] = value.strip().decode("latin-1")
    return headers


def _parse_content_disposition(value: str) -> tuple[str, str | None]:
    """Parse the exact bounded form-data parameter grammar without substring matches."""

    token, separator, _remainder = value.partition(";")
    if token.strip().casefold() != "form-data":
        raise MultipartParseError("multipart Content-Disposition must be form-data")
    cursor = len(token)
    parameters: dict[str, str] = {}
    while cursor < len(value):
        match = _DISPOSITION_PARAM_RE.match(value, cursor)
        if match is None:
            raise MultipartParseError("multipart Content-Disposition is malformed")
        name = match.group(1).casefold()
        if name not in {"name", "filename"}:
            raise MultipartParseError("multipart Content-Disposition has an unsupported parameter")
        if name in parameters:
            raise MultipartParseError("multipart Content-Disposition has a duplicate parameter")
        parameters[name] = re.sub(r"\\(.)", r"\1", match.group(2))
        cursor = match.end()
    if not separator or "name" not in parameters:
        raise MultipartParseError("multipart part is missing a field name")
    return parameters["name"], parameters.get("filename")


def safe_filename(filename: str | None) -> str:
    """Strip any path components; never let an upload pick its own key prefix."""
    cleaned = unicodedata.normalize("NFC", basename((filename or "").replace("\\", "/")).strip())
    if not cleaned or cleaned in {".", ".."}:
        return "upload.bin"
    if any(unicodedata.category(char).startswith("C") for char in cleaned):
        raise InvalidDocumentFilenameError("document filename contains control characters")
    if len(cleaned) > MAX_DOCUMENT_FILENAME_CHARS or len(cleaned.encode("utf-8")) > (
        MAX_DOCUMENT_FILENAME_BYTES
    ):
        raise InvalidDocumentFilenameError("document filename is too long")
    return cleaned


def document_artifact_key(document_id: str, filename: str) -> str:
    return f"documents/{document_id}/{filename}"


def _truncate_context_value(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    marker = "\n\n…[truncated]"
    prefix = value[: max(limit - len(marker), 0)].rstrip()
    return (prefix + marker)[:limit]


async def uploaded_document_context_packets(
    repository: DocumentsRepository,
    identity: ConsumerIdentity,
    document_ids: list[str],
) -> list[dict[str, Any]]:
    """Authorize uploaded metadata and convert extracted text to bounded evidence."""

    per_doc_cap = min(
        get_settings().documents.max_context_chars_per_doc,
        MAX_CONTEXT_TEXT_CHARS,
    )
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
        text = _truncate_context_value(document.extracted_text or None, per_doc_cap)
        summary = _truncate_context_value(
            document.summary or None,
            MAX_CONTEXT_SUMMARY_CHARS,
        )
        packets.append(
            {
                "id": f"document-{document.id}",
                "source": "document",
                "title": document.name,
                "summary": summary,
                "ref": f"/v1/artifacts/{quote(document.artifact_key, safe='/')}",
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
        summary = sanitize_document_text(summary)
        document_id = uuid4().hex
        name = safe_filename(filename)
        key = document_artifact_key(document_id, name)
        document = Document(
            id=document_id,
            name=name,
            media_type=content_type,
            size_bytes=len(data),
            artifact_key=key,
            artifact_connection_id=artifact_connection_id,
            project_id=project_id,
            app_id=app_id,
            summary=summary,
            uploaded_by=uploaded_by,
            extracted_text=None,
            extracted_chars=None,
            parse_status=None,
            parse_error=None,
        )
        # This hidden row is the durable ownership/cleanup intent. A crash after
        # the following commit can always be reconciled, even if no PUT response
        # or metadata-finalization step is observed.
        await self._repository.stage_upload(document)
        heartbeat_stop: asyncio.Event | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        if isinstance(self._repository, DocumentsRepository):
            heartbeat_stop = asyncio.Event()
            heartbeat_task = asyncio.create_task(
                _renew_document_upload_lease(document_id, heartbeat_stop),
                name=f"document-upload-lease-{document_id}",
            )
        write_started = False
        finalize_started = False
        try:
            # Shield the provider write from caller cancellation, then wait for
            # its definitive outcome before compensating. S3's blocking worker
            # can otherwise commit after this coroutine already deleted/noped.
            write_started = True
            put_task = asyncio.create_task(
                self._store.put(key, data, content_type=content_type),
                name=f"document-put-{document_id}",
            )
            stored = await await_task_definitively(put_task)
            validate_stored_artifact_ack(
                stored,
                key,
                expected_size=len(data),
            )

            # Parse the bytes into text the agent can actually read. Off-thread because
            # pypdf/python-docx are blocking; soft-fails (status set) never break the upload.
            ingest = get_settings().documents
            async with _loop_limiter(_EXTRACTION_LIMITERS):
                extraction_task = asyncio.create_task(
                    asyncio.to_thread(
                        _extract_text_bounded,
                        data,
                        filename=name,
                        content_type=content_type,
                        max_chars=ingest.max_extract_chars,
                    ),
                    name=f"document-extract-{document_id}",
                )
                extraction = await await_task_definitively(extraction_task)
            extracted_text = sanitize_document_text(extraction.text) or None
            parse_error = sanitize_document_text(extraction.error)
            if not summary and extraction.status == PARSE_PARSED and extracted_text:
                summary = sanitize_document_text(
                    derive_summary(extracted_text, max_chars=ingest.summary_chars)
                )

            document.summary = summary
            document.extracted_text = extracted_text
            document.extracted_chars = extraction.char_count
            document.parse_status = extraction.status
            document.parse_error = parse_error
            finalize_started = True
            return await self._repository.finalize_upload(document)
        except BaseException as exc:
            if finalize_started:
                resolver = getattr(self._repository, "resolve_finalized_upload", None)
                if resolver is None:
                    # Finalization may already be durable. Without an
                    # authoritative read, deleting the object could corrupt a
                    # live row. A still-hidden intent is safely recoverable by
                    # the stale-upload reconciler.
                    logger.warning(
                        "documents.finalize_resolution_unavailable",
                        key=key,
                    )
                    raise
                finalized: Document | None = None
                resolution_failed = False
                try:
                    finalized = await resolver(document_id)
                except BaseException as resolution_exc:
                    resolution_failed = True
                    logger.warning(
                        "documents.finalize_resolution_failed",
                        key=key,
                        error_type=safe_type_name(resolution_exc),
                    )
                if resolution_failed:
                    # Preserve bytes on an ambiguous finalize. The database may
                    # already expose a live document, and deleting here would
                    # leave that durable row permanently broken.
                    raise
                if finalized:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return finalized
            tombstone: Document | None = None
            try:
                tombstone = await self._repository.mark_upload_deletion_pending(document_id)
            except BaseException as tombstone_exc:
                logger.warning(
                    "documents.upload_tombstone_failed",
                    key=key,
                    error_type=safe_type_name(tombstone_exc),
                )
            object_absent = not write_started
            if tombstone is not None:
                try:
                    delete = getattr(self._store, "delete", None)
                    # A raised/lost PUT acknowledgement is ambiguous: the UUID
                    # key may already exist remotely. Compensation is safe only
                    # after the exact pending upload has a durable tombstone.
                    if write_started and delete is not None:
                        await delete(key)
                        object_absent = True
                except (KeyError, FileNotFoundError):
                    object_absent = True
                except BaseException as cleanup_exc:
                    logger.warning(
                        "documents.orphan_cleanup_failed",
                        key=key,
                        error_type=safe_type_name(cleanup_exc),
                    )
            if tombstone is not None and object_absent:
                try:
                    await self._repository.complete_deletion(document_id)
                except BaseException as finalize_exc:
                    logger.warning(
                        "documents.upload_intent_finalize_failed",
                        key=key,
                        error_type=safe_type_name(finalize_exc),
                    )
            raise
        finally:
            if heartbeat_stop is not None and heartbeat_task is not None:
                heartbeat_stop.set()
                await cancel_task_definitively(heartbeat_task)


async def _renew_document_upload_lease(document_id: str, stop: asyncio.Event) -> None:
    """Refresh an active upload intent from an independent database session."""

    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=DOCUMENT_UPLOAD_LEASE_RENEW_INTERVAL_S,
            )
            return
        except TimeoutError:
            pass
        try:
            async with get_sessionmaker()() as session:
                renewed = await DocumentsRepository(session).renew_upload_lease(document_id)
            if not renewed:
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A transient database failure should not cancel provider IO. Retry
            # well inside the one-hour stale threshold; the CAS still arbitrates
            # correctly if a cleanup worker has already claimed the row.
            logger.warning(
                "documents.upload_lease_renewal_failed",
                document_id=document_id,
                error_type=safe_type_name(exc),
            )


async def purge_document_tombstone(
    document: Document,
    repository: DocumentsRepository,
    resolver: Any,
) -> None:
    """Idempotently delete bytes, then finalize an already-durable tombstone."""

    if document.artifact_connection_id is None and get_settings().is_locked_down:
        # Migration 0013 could not infer historical store ownership. Resolving
        # today's default could delete metadata while leaving bytes in the old
        # store, so production must wait for an explicit operator mapping.
        raise UnknownDocumentArtifactAffinityError(
            "document artifact-store affinity must be mapped before deletion"
        )

    store, resolved_connection_id = await resolver.resolve_with_connection_id(
        PortKind.ARTIFACT_STORE,
        connection_id=document.artifact_connection_id,
        project_id=document.project_id,
    )
    try:
        if not _safe_connection_id(resolved_connection_id):
            raise RuntimeError("artifact-store resolver returned an invalid connection id")
        if (
            document.artifact_connection_id is not None
            and resolved_connection_id != document.artifact_connection_id
        ):
            raise RuntimeError("artifact-store resolver did not honor document affinity")
        delete_object = getattr(store, "delete", None)
        if delete_object is None:
            raise RuntimeError("artifact store does not support object deletion")
        try:
            await delete_object(document.artifact_key)
        except (KeyError, FileNotFoundError):
            # A prior attempt may have deleted the deterministic object before its
            # metadata-finalization acknowledgement was lost.
            pass
        await repository.complete_deletion(document.id)
    finally:
        await close_adapter(store)


async def purge_stale_upload_intent(
    document: Document,
    repository: DocumentsRepository,
    resolver: Any,
) -> None:
    """Convert an abandoned hidden upload intent into a resumable tombstone."""

    claim = getattr(repository, "claim_stale_upload", None)
    if claim is None:
        # Compatibility for test/dry-run repositories. Production persistence
        # always uses the atomic claim above.
        await repository.mark_deletion_pending(document)
        claimed = document
    else:
        claimed = await claim(document)
        if claimed is None:
            # A concurrent finalizer won; its live object must not be deleted.
            return
    await purge_document_tombstone(claimed, repository, resolver)


async def reconcile_pending_document_deletions_once(
    *, heartbeat: Callable[[], None] | None = None
) -> None:
    """Drain one bounded tombstone batch; safe to run concurrently on replicas."""

    if heartbeat is not None:
        heartbeat()
    resolver = get_connection_resolver()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        repository = DocumentsRepository(session)
        pending_ids = [row.id for row in await repository.list_pending_deletions()]
        stale_ids = [
            row.id
            for row in await repository.list_stale_pending_uploads(
                before=datetime.now(UTC) - STALE_DOCUMENT_UPLOAD_AGE
            )
        ]

    # Isolate each intent in its own session. A poison row or ambiguous commit
    # cannot invalidate the transaction used by later rows or starve the batch.
    for document_id in pending_ids:
        if heartbeat is not None:
            heartbeat()
        async with sessionmaker() as session:
            repository = DocumentsRepository(session)
            document = await repository.get_pending_deletion(document_id)
            if document is None:
                continue
            artifact_key = document.artifact_key
            try:
                await purge_document_tombstone(document, repository, resolver)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "documents.deletion_reconcile_failed",
                    document_id=document_id,
                    artifact_key=artifact_key,
                    error_type=safe_type_name(exc),
                )
                await session.rollback()
                try:
                    await repository.defer_cleanup(
                        document_id,
                        error=safe_type_name(exc),
                    )
                except Exception as defer_exc:
                    logger.warning(
                        "documents.deletion_retry_schedule_failed",
                        document_id=document_id,
                        error_type=safe_type_name(defer_exc),
                    )
                    await session.rollback()

    for document_id in stale_ids:
        if heartbeat is not None:
            heartbeat()
        async with sessionmaker() as session:
            repository = DocumentsRepository(session)
            document = await repository.get_pending_upload(document_id)
            if document is None:
                continue
            artifact_key = document.artifact_key
            try:
                await purge_stale_upload_intent(document, repository, resolver)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "documents.upload_intent_reconcile_failed",
                    document_id=document_id,
                    artifact_key=artifact_key,
                    error_type=safe_type_name(exc),
                )
                await session.rollback()
                try:
                    await repository.defer_cleanup(
                        document_id,
                        error=safe_type_name(exc),
                    )
                except Exception as defer_exc:
                    logger.warning(
                        "documents.upload_retry_schedule_failed",
                        document_id=document_id,
                        error_type=safe_type_name(defer_exc),
                    )
                    await session.rollback()


async def run_document_deletion_reconciler(
    stop: asyncio.Event,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    """Periodically finish logical deletes interrupted between the two stores."""

    while not stop.is_set():
        try:
            await reconcile_pending_document_deletions_once(heartbeat=heartbeat)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "documents.deletion_reconciler_failed",
                error_type=safe_type_name(exc),
            )
        if heartbeat is not None:
            heartbeat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=DOCUMENT_DELETION_RETRY_INTERVAL_S)
        except TimeoutError:
            pass


# ── FastAPI dependency providers (shared by /documents and /artifacts) ──────


async def get_artifact_store() -> AsyncIterator[ArtifactStorePort]:
    """Default artifact store via the connection resolver; override in tests."""
    store = await get_connection_resolver().resolve(PortKind.ARTIFACT_STORE)
    try:
        yield store
    finally:
        await close_adapter(store)


def get_documents_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentsRepository:
    return DocumentsRepository(session)
