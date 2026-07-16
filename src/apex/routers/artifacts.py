"""Authenticated, store-affine byte proxy for documents and run artifacts."""

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from itertools import islice
from typing import Annotated, Any, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from langgraph_sdk.errors import NotFoundError

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity
from apex.auth.identity import ConsumerIdentity, ScopeRef
from apex.auth.service import extract_api_key
from apex.domain.input_limits import MAX_RECORD_ID_CHARS, NoNulStr
from apex.persistence.db import get_sessionmaker
from apex.persistence.models import ArtifactReference, Document, EngineRun
from apex.persistence.repositories.artifact_references import ArtifactReferencesRepository
from apex.persistence.repositories.documents import DocumentsRepository
from apex.persistence.repositories.engine_runs import EngineRunsRepository
from apex.ports.artifact_store import ArtifactStoreBusyError
from apex.services.connections import ConnectionResolver, close_adapter, get_connection_resolver
from apex.services.langgraph_client import loopback_client
from apex.settings import get_settings

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

ConnectionResolverDep = Annotated[ConnectionResolver, Depends(get_connection_resolver)]
ArtifactKey = Annotated[NoNulStr, Path(min_length=1, max_length=1024)]

FALLBACK_MEDIA_TYPE = "application/octet-stream"
TRANSCRIPT_PREFIX = "transcripts/"
ENGINE_RUN_PREFIX = "engine-runs/"
_ENGINE_NAMESPACE_RE = re.compile(r"^engine-runs/[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")
_MAX_CHECKPOINT_SCAN_DEPTH = 64
_MAX_CHECKPOINT_SCAN_NODES = 10_000
_MAX_ARTIFACT_STREAM_CHUNK_BYTES = 64 * 1024
_MAX_LEGACY_ARTIFACT_STREAM_BYTES = 512 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _ArtifactStreamReadError(RuntimeError):
    """A response already started before its object-store read failed."""


def _artifact_store_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="artifact store unavailable")


def _artifact_read_failed() -> HTTPException:
    return HTTPException(status_code=502, detail="artifact store read failed")


async def _await_task_definitively(task: asyncio.Task[None]) -> None:
    """Settle owned cleanup despite repeated cancellation of its caller."""

    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except BaseException:
            break
    task.result()
    if interrupted:
        raise asyncio.CancelledError from None


def _artifact_not_found() -> HTTPException:
    """Return a stable 404 without reflecting an arbitrary artifact path."""

    return HTTPException(status_code=404, detail="artifact not found")


@dataclass(frozen=True)
class AuthorizedArtifact:
    media_type: str
    project_id: str | None
    connection_id: str | None
    expected_size: int | None = None
    content_sha256: str | None = None


class _ArtifactDocumentsReader(Protocol):
    async def get_by_artifact_key(self, artifact_key: str) -> Document | None: ...


class _ArtifactEngineRunsReader(Protocol):
    async def get_by_artifact_namespace(
        self,
        artifact_namespace: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None: ...


class _ArtifactReferencesReader(Protocol):
    async def get_exact(self, artifact_key: str) -> ArtifactReference | None: ...


@dataclass(frozen=True)
class ArtifactAuthorizationRepositories:
    """Repositories whose session is owned by one authorization lookup."""

    documents: _ArtifactDocumentsReader
    engine_runs: _ArtifactEngineRunsReader
    artifact_references: _ArtifactReferencesReader


ArtifactAuthorizationRepositoryFactory = Callable[
    [], AbstractAsyncContextManager[ArtifactAuthorizationRepositories]
]


@asynccontextmanager
async def _open_artifact_authorization_repositories() -> AsyncIterator[
    ArtifactAuthorizationRepositories
]:
    async with get_sessionmaker()() as session:
        yield ArtifactAuthorizationRepositories(
            documents=DocumentsRepository(session),
            engine_runs=EngineRunsRepository(session),
            artifact_references=ArtifactReferencesRepository(session),
        )


def get_artifact_authorization_repository_factory() -> ArtifactAuthorizationRepositoryFactory:
    """Return an opener so the endpoint, not FastAPI, controls session closure."""

    return _open_artifact_authorization_repositories


ArtifactAuthorizationRepositoriesDep = Annotated[
    ArtifactAuthorizationRepositoryFactory,
    Depends(get_artifact_authorization_repository_factory),
]


@dataclass(frozen=True)
class _DocumentAuthorizationRow:
    media_type: str
    project_id: str | None
    app_id: str | None
    artifact_connection_id: str | None
    size_bytes: int


@dataclass(frozen=True)
class _EngineRunAuthorizationRow:
    thread_id: str
    project_id: str | None
    app_id: str | None
    ownership_known: bool
    artifact_connection_id: str | None


@dataclass(frozen=True)
class _ArtifactReferenceAuthorizationRow:
    artifact_key: str
    connection_id: str
    kind: str
    thread_id: str
    project_id: str | None
    app_id: str | None
    ownership_known: bool
    size_bytes: int | None
    content_sha256: str | None


@dataclass(frozen=True)
class _ArtifactAuthorizationRows:
    document: _DocumentAuthorizationRow | None = None
    engine_run: _EngineRunAuthorizationRow | None = None
    artifact_reference: _ArtifactReferenceAuthorizationRow | None = None


class _OwnedStream:
    """Close-once owner for a provider iterator, including before first use."""

    def __init__(
        self,
        iterator: Any,
        first: bytes | None,
        *,
        exhausted: bool = False,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> None:
        if expected_size is not None and (
            type(expected_size) is not int
            or not 0 <= expected_size <= _MAX_LEGACY_ARTIFACT_STREAM_BYTES
        ):
            raise ValueError("invalid artifact stream size")
        if expected_sha256 is not None and (
            type(expected_sha256) is not str or _SHA256_RE.fullmatch(expected_sha256) is None
        ):
            raise ValueError("invalid artifact stream digest")
        self._iterator = iterator
        self._first = first
        self._has_first = first is not None
        self._closed = exhausted
        self._expected_size = expected_size
        self._max_bytes = (
            expected_size if expected_size is not None else _MAX_LEGACY_ARTIFACT_STREAM_BYTES
        )
        self._consumed_bytes = 0
        self._expected_sha256 = expected_sha256
        self._hasher = sha256() if expected_sha256 is not None else None
        self._operation_lock = asyncio.Lock()

    def __aiter__(self) -> "_OwnedStream":
        return self

    async def __anext__(self) -> bytes:
        async with self._operation_lock:
            if self._closed:
                raise StopAsyncIteration
            if self._has_first:
                self._has_first = False
                first = self._first
                self._first = None
                assert first is not None
                if self._accept_chunk(first):
                    return first
                self._closed = True
                await _close_stream_iterator_preserving_error(self._iterator)
                raise _ArtifactStreamReadError("artifact stream read failed")
            read_failed = False
            try:
                chunk = await anext(self._iterator)
                if self._accept_chunk(chunk):
                    return chunk
                self._closed = True
                await _close_stream_iterator_preserving_error(self._iterator)
                read_failed = True
            except StopAsyncIteration:
                self._closed = True
                await _close_stream_iterator_preserving_error(self._iterator)
                if self._stream_identity_matches():
                    raise
                read_failed = True
            except asyncio.CancelledError:
                self._closed = True
                await _close_stream_iterator_preserving_error(self._iterator)
                raise
            except Exception:
                self._closed = True
                await _close_stream_iterator_preserving_error(self._iterator)
                read_failed = True
            except BaseException:
                self._closed = True
                await _close_stream_iterator_preserving_error(self._iterator)
                raise
            if read_failed:
                # Response headers may already be committed. Propagate only a
                # stable sentinel so the ASGI server cannot log an object-store
                # response body, credential, or hostile exception context.
                raise _ArtifactStreamReadError("artifact stream read failed")
            raise AssertionError("artifact stream read ended without an outcome")

    def _accept_chunk(self, chunk: Any) -> bool:
        if (
            type(chunk) is not bytes
            or not chunk
            or len(chunk) > _MAX_ARTIFACT_STREAM_CHUNK_BYTES
            or len(chunk) > self._max_bytes - self._consumed_bytes
        ):
            return False
        self._consumed_bytes += len(chunk)
        if self._hasher is not None:
            self._hasher.update(chunk)
        return True

    def _stream_identity_matches(self) -> bool:
        if self._expected_size is not None and self._consumed_bytes != self._expected_size:
            return False
        return self._expected_sha256 is None or (
            self._hasher is not None and self._hasher.hexdigest() == self._expected_sha256
        )

    async def aclose(self) -> None:
        close_task = asyncio.create_task(self._close_once())
        await _await_task_definitively(close_task)

    async def _close_once(self) -> None:
        # A concurrent response-finalizer waits for an active provider read to
        # settle; it never calls aclose() against a running async generator.
        async with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            await _close_stream_iterator(self._iterator)


class _OwnedStreamingResponse(StreamingResponse):
    """Make the ASGI response—not generator GC—own provider cleanup."""

    def __init__(self, stream: _OwnedStream, *, media_type: str) -> None:
        self._owned_stream = stream
        super().__init__(
            stream,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store"},
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            # Starlette does not close an arbitrary async iterator when send()
            # fails or the request is cancelled between handler return and the
            # first body pull. Repeated cancellation during cleanup must not
            # replace the original stream/send outcome.
            try:
                await self._owned_stream.aclose()
            except asyncio.CancelledError:
                pass
            raise
        else:
            await self._owned_stream.aclose()


async def _open_stream(
    store: Any,
    key: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> _OwnedStream:
    """Open and preflight a stream so a missing object is still a clean 404."""

    if (
        expected_size is not None
        and (
            type(expected_size) is not int
            or not 0 <= expected_size <= _MAX_LEGACY_ARTIFACT_STREAM_BYTES
        )
    ) or (
        expected_sha256 is not None
        and (type(expected_sha256) is not str or _SHA256_RE.fullmatch(expected_sha256) is None)
    ):
        raise _artifact_read_failed()

    source: Any | None = None
    iterator: Any | None = None
    open_error: HTTPException | None = None
    try:
        source = store.iter_bytes(key)
        if source is None:
            raise TypeError("artifact store returned no stream")
        iterator = source.__aiter__()
    except (KeyError, FileNotFoundError):
        open_error = _artifact_not_found()
    except ArtifactStoreBusyError:
        open_error = HTTPException(
            status_code=503,
            detail="artifact streaming capacity is busy",
            headers={"Retry-After": "1"},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        open_error = _artifact_read_failed()
    if open_error is not None:
        if source is not None:
            await _close_stream_iterator_preserving_error(source)
        raise open_error
    assert iterator is not None

    first: bytes | None = None
    stream_error: HTTPException | None = None
    exhausted = False
    try:
        first = await anext(iterator)
    except StopAsyncIteration:
        await _close_stream_iterator_preserving_error(iterator)
        exhausted = True
    except (KeyError, FileNotFoundError):
        await _close_stream_iterator_preserving_error(iterator)
        stream_error = _artifact_not_found()
    except ArtifactStoreBusyError:
        await _close_stream_iterator_preserving_error(iterator)
        stream_error = HTTPException(
            status_code=503,
            detail="artifact streaming capacity is busy",
            headers={"Retry-After": "1"},
        )
    except asyncio.CancelledError:
        await _close_stream_iterator_preserving_error(iterator)
        raise
    except Exception:
        await _close_stream_iterator_preserving_error(iterator)
        stream_error = _artifact_read_failed()
    except BaseException:
        # The wrapper below does not own the iterator until preflight completes.
        # Close explicitly on cancellation or an arbitrary provider failure so a
        # partially-open response cannot strand a socket/object-store lease.
        await _close_stream_iterator_preserving_error(iterator)
        raise

    if exhausted:
        empty_digest = sha256(b"").hexdigest()
        if (expected_size not in (None, 0)) or (
            expected_sha256 is not None and expected_sha256 != empty_digest
        ):
            stream_error = _artifact_read_failed()
        else:
            return _OwnedStream(
                iterator,
                None,
                exhausted=True,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
    elif first is not None:
        maximum = expected_size if expected_size is not None else _MAX_LEGACY_ARTIFACT_STREAM_BYTES
        if (
            type(first) is not bytes
            or not first
            or len(first) > _MAX_ARTIFACT_STREAM_CHUNK_BYTES
            or len(first) > maximum
        ):
            await _close_stream_iterator_preserving_error(iterator)
            stream_error = _artifact_read_failed()

    if stream_error is not None:
        raise stream_error
    return _OwnedStream(
        iterator,
        first,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )


async def _close_stream_iterator(iterator: Any) -> None:
    try:
        close = getattr(iterator, "aclose", None)
    except BaseException:
        return
    if not callable(close):
        return

    async def close_owned() -> None:
        try:
            await cast(Awaitable[Any], close())
        except BaseException:
            # Provider cleanup is best effort, but its owned task must settle.
            # A caller cancellation is delivered only to the shielded waiter.
            pass

    close_task = asyncio.create_task(close_owned())
    await _await_task_definitively(close_task)


async def _close_stream_iterator_preserving_error(iterator: Any) -> None:
    """Close definitively without replacing an exception already being handled."""

    try:
        await _close_stream_iterator(iterator)
    except asyncio.CancelledError:
        pass


@router.get("/{key:path}", operation_id="getArtifact")
async def get_artifact(
    key: ArtifactKey,
    identity: CurrentIdentity,
    resolver: ConnectionResolverDep,
    authorization_repositories: ArtifactAuthorizationRepositoriesDep,
    request: Request,
) -> StreamingResponse:
    authorized = await _authorize_artifact_key(
        key,
        identity,
        authorization_repositories,
        request,
    )
    store: Any | None = None
    resolver_error: HTTPException | None = None
    try:
        store, resolved_connection_id = await resolver.resolve_with_connection_id(
            PortKind.ARTIFACT_STORE,
            connection_id=authorized.connection_id,
            project_id=authorized.project_id,
        )
        if (
            type(resolved_connection_id) is not str
            or not 1 <= len(resolved_connection_id) <= MAX_RECORD_ID_CHARS
            or "\x00" in resolved_connection_id
            or (
                authorized.connection_id is not None
                and resolved_connection_id != authorized.connection_id
            )
        ):
            raise RuntimeError("artifact-store resolver did not honor durable affinity")
    except Exception:
        if store is not None:
            try:
                await close_adapter(store)
            except Exception:
                # Preserve the fixed boundary translation if provider cleanup
                # also fails; request cancellation is still allowed through.
                pass
        store = None
        resolver_error = _artifact_store_unavailable()
    if resolver_error is not None:
        raise resolver_error

    try:
        stream = await _open_stream(
            store,
            key,
            expected_size=authorized.expected_size,
            expected_sha256=authorized.content_sha256,
        )
    except BaseException:
        try:
            await close_adapter(store)
        except BaseException:
            pass
        raise
    # iter_bytes() returned a leased iterator. Release the parent resolver
    # checkout before the response starts; the iterator retains the generation
    # through exhaustion/disconnect and owns its own definitive cleanup.
    close_failed = False
    try:
        await close_adapter(store)
    except asyncio.CancelledError:
        await _close_stream_iterator_preserving_error(stream)
        raise
    except Exception:
        await _close_stream_iterator_preserving_error(stream)
        close_failed = True
    except BaseException:
        await _close_stream_iterator_preserving_error(stream)
        raise
    if close_failed:
        raise _artifact_store_unavailable()
    try:
        return _OwnedStreamingResponse(stream, media_type=authorized.media_type)
    except BaseException:
        # Construction is synchronous today, but keep ownership correct if a
        # response extension rejects the stream before Starlette starts iterating.
        await _close_stream_iterator_preserving_error(stream)
        raise


async def _authorize_artifact_key(
    key: str,
    identity: ConsumerIdentity,
    repository_factory: ArtifactAuthorizationRepositoryFactory,
    request: Request,
) -> AuthorizedArtifact:
    # The context exits before any loopback request, connection resolution, or
    # object-store admission. Only immutable scalar snapshots leave this block.
    async with repository_factory() as repositories:
        rows = await _load_artifact_authorization_rows(key, identity, repositories)

    return await _authorize_materialized_artifact(key, identity, rows, request)


async def _load_artifact_authorization_rows(
    key: str,
    identity: ConsumerIdentity,
    repositories: ArtifactAuthorizationRepositories,
) -> _ArtifactAuthorizationRows:
    document = await repositories.documents.get_by_artifact_key(key)
    if document is not None:
        return _ArtifactAuthorizationRows(document=_materialize_document(document))

    parsed_engine_key = _parse_engine_key(key)
    if parsed_engine_key is not None:
        namespace, _relative_key = parsed_engine_key
        allowed = None if identity.is_unscoped else identity.scopes
        run = await repositories.engine_runs.get_by_artifact_namespace(
            namespace,
            allowed_scopes=allowed,
        )
        if run is None:
            return _ArtifactAuthorizationRows()
        reference = await repositories.artifact_references.get_exact(key)
        return _ArtifactAuthorizationRows(
            engine_run=_materialize_engine_run(run),
            artifact_reference=(
                _materialize_artifact_reference(reference) if reference is not None else None
            ),
        )

    transcript_thread_id = _transcript_thread_id(key)
    if transcript_thread_id is not None:
        reference = await repositories.artifact_references.get_exact(key)
        return _ArtifactAuthorizationRows(
            artifact_reference=(
                _materialize_artifact_reference(reference) if reference is not None else None
            )
        )

    return _ArtifactAuthorizationRows()


async def _authorize_materialized_artifact(
    key: str,
    identity: ConsumerIdentity,
    rows: _ArtifactAuthorizationRows,
    request: Request,
) -> AuthorizedArtifact:
    document = rows.document
    if document is not None:
        if document.project_id is None or identity.allows_scope(
            project_id=document.project_id, app_id=document.app_id
        ):
            if document.artifact_connection_id is None and get_settings().is_locked_down:
                raise HTTPException(
                    status_code=503,
                    detail="document artifact-store affinity requires operator repair",
                )
            return AuthorizedArtifact(
                media_type=_safe_media_type(document.media_type, FALLBACK_MEDIA_TYPE),
                project_id=document.project_id,
                connection_id=document.artifact_connection_id,
                expected_size=document.size_bytes,
            )
        raise _artifact_not_found()

    if key.startswith(ENGINE_RUN_PREFIX):
        if _parse_engine_key(key) is None:
            raise _artifact_not_found()
        row = rows.engine_run
        if row is not None:
            durable_ref = rows.artifact_reference
            if durable_ref is not None:
                return _authorize_durable_engine_reference(key, identity, row, durable_ref)
            if get_settings().is_locked_down:
                # Production checkpoints can be arbitrarily large legacy blobs and
                # the native SDK materializes the complete response before our
                # bounded tree scan can run. Never deserialize one on a public
                # artifact request. Pre-ownership rows retain their deliberately
                # narrow projection-only compatibility path; known owners require
                # the durable exact-key index written by current executions.
                if row.ownership_known:
                    raise HTTPException(
                        status_code=503,
                        detail="engine artifact durable reference requires operator repair",
                    )
                connection_id = _connection_id(row.artifact_connection_id)
                _require_artifact_affinity(connection_id, kind="engine")
                return AuthorizedArtifact(
                    media_type=FALLBACK_MEDIA_TYPE,
                    project_id=row.project_id,
                    connection_id=connection_id,
                )
            state_lookup = await _thread_artifact_lookup(request, row.thread_id, key)
            if state_lookup is not None:
                _thread, ref = state_lookup
                if ref is None:
                    raise _artifact_not_found()
                media_type = _safe_media_type(ref.get("media_type"), FALLBACK_MEDIA_TYPE)
                row_connection_id = _connection_id(row.artifact_connection_id)
                ref_connection_id = _connection_id(ref.get("artifact_connection_id"))
                if (
                    row_connection_id is not None
                    and ref_connection_id is not None
                    and row_connection_id != ref_connection_id
                ):
                    raise _artifact_not_found()
                connection_id = row_connection_id or ref_connection_id
            else:
                # Only rows explicitly marked as pre-ownership may use the
                # projection-only compatibility path.  For an ownership-known
                # row, a filtered/missing thread is an authorization failure,
                # not evidence that the row is legacy.
                if row.ownership_known:
                    raise _artifact_not_found()
                media_type = FALLBACK_MEDIA_TYPE
                connection_id = _connection_id(row.artifact_connection_id)
            _require_artifact_affinity(connection_id, kind="engine")
            return AuthorizedArtifact(
                media_type=media_type,
                project_id=row.project_id,
                connection_id=connection_id,
            )
        raise _artifact_not_found()

    if key.startswith(TRANSCRIPT_PREFIX):
        thread_id = _transcript_thread_id(key)
        if thread_id is None:
            raise _artifact_not_found()
        durable_ref = rows.artifact_reference
        if durable_ref is not None:
            if (
                durable_ref.kind != "transcript"
                or durable_ref.artifact_key != key
                or durable_ref.thread_id != thread_id
                or not _reference_visible(identity, durable_ref)
            ):
                raise _artifact_not_found()
            return AuthorizedArtifact(
                media_type="text/plain",
                project_id=durable_ref.project_id,
                connection_id=durable_ref.connection_id,
                expected_size=durable_ref.size_bytes,
                content_sha256=durable_ref.content_sha256,
            )
        if get_settings().is_locked_down:
            raise HTTPException(
                status_code=503,
                detail="transcript durable reference requires operator repair",
            )
        state_lookup = await _thread_artifact_lookup(request, thread_id, key)
        if state_lookup is None:
            raise _artifact_not_found() from None
        thread, ref = state_lookup
        if ref is None or ref.get("kind") != "transcript":
            raise _artifact_not_found()
        metadata = thread.get("metadata") if type(thread) is dict else None
        raw_project_id = metadata.get("project_id") if type(metadata) is dict else None
        project_id = raw_project_id if type(raw_project_id) is str and raw_project_id else None
        connection_id = _connection_id(ref.get("artifact_connection_id"))
        _require_artifact_affinity(connection_id, kind="transcript")
        return AuthorizedArtifact(
            media_type=_safe_media_type(ref.get("media_type"), "text/plain"),
            project_id=project_id,
            connection_id=connection_id,
        )

    raise _artifact_not_found()


def _authorize_durable_engine_reference(
    key: str,
    identity: ConsumerIdentity,
    run: _EngineRunAuthorizationRow,
    reference: _ArtifactReferenceAuthorizationRow,
) -> AuthorizedArtifact:
    run_connection_id = _connection_id(run.artifact_connection_id)
    reference_connection_id = _connection_id(reference.connection_id)
    if (
        reference.artifact_key != key
        or not reference.kind.startswith("engine_")
        or reference.thread_id != run.thread_id
        or reference.project_id != run.project_id
        or reference.app_id != run.app_id
        or not _reference_visible(identity, reference)
        or reference_connection_id is None
        or (run_connection_id is not None and run_connection_id != reference_connection_id)
    ):
        raise _artifact_not_found()
    return AuthorizedArtifact(
        media_type=FALLBACK_MEDIA_TYPE,
        project_id=reference.project_id,
        connection_id=reference_connection_id,
        expected_size=reference.size_bytes,
        content_sha256=reference.content_sha256,
    )


def _reference_visible(
    identity: ConsumerIdentity,
    reference: _ArtifactReferenceAuthorizationRow,
) -> bool:
    if reference.project_id is None:
        return identity.is_unscoped
    if not reference.ownership_known and reference.app_id is None:
        # Rows created before application ownership was projected are ambiguous:
        # app-only grants must not reinterpret NULL as a deliberate project-level
        # audience. Project-wide operators retain access for remediation.
        return identity.is_unscoped or any(
            scope.project_id == reference.project_id and scope.app_id is None
            for scope in identity.scopes
        )
    return identity.allows_scope(project_id=reference.project_id, app_id=reference.app_id)


def _content_identity_repair_required() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="artifact content identity requires operator repair",
    )


def _validated_artifact_size(value: Any, *, nullable: bool) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or not 0 <= value <= _MAX_LEGACY_ARTIFACT_STREAM_BYTES:
        raise _content_identity_repair_required()
    return value


def _materialize_document(document: Document) -> _DocumentAuthorizationRow:
    size_bytes = _validated_artifact_size(document.size_bytes, nullable=False)
    assert size_bytes is not None
    return _DocumentAuthorizationRow(
        media_type=document.media_type,
        project_id=document.project_id,
        app_id=document.app_id,
        artifact_connection_id=document.artifact_connection_id,
        size_bytes=size_bytes,
    )


def _materialize_engine_run(run: EngineRun) -> _EngineRunAuthorizationRow:
    return _EngineRunAuthorizationRow(
        thread_id=run.thread_id,
        project_id=run.project_id,
        app_id=run.app_id,
        ownership_known=(
            run.ownership_known and (run.app_id is not None or run.scope_ownership_known)
        ),
        artifact_connection_id=run.artifact_connection_id,
    )


def _materialize_artifact_reference(
    reference: ArtifactReference,
) -> _ArtifactReferenceAuthorizationRow:
    size_bytes = _validated_artifact_size(reference.size_bytes, nullable=True)
    content_sha256 = reference.content_sha256
    if content_sha256 is not None and (
        type(content_sha256) is not str or _SHA256_RE.fullmatch(content_sha256) is None
    ):
        raise _content_identity_repair_required()
    if (size_bytes is None) != (content_sha256 is None):
        raise _content_identity_repair_required()
    return _ArtifactReferenceAuthorizationRow(
        artifact_key=reference.artifact_key,
        connection_id=reference.connection_id,
        kind=reference.kind,
        thread_id=reference.thread_id,
        project_id=reference.project_id,
        app_id=reference.app_id,
        ownership_known=reference.ownership_known,
        size_bytes=size_bytes,
        content_sha256=content_sha256,
    )


def _parse_engine_key(key: str) -> tuple[str, str] | None:
    if not key.startswith(ENGINE_RUN_PREFIX):
        return None
    parts = key.split("/", 2)
    namespace = "/".join(parts[:2]) if len(parts) == 3 else ""
    relative_key = parts[2] if len(parts) == 3 else ""
    if (
        _ENGINE_NAMESPACE_RE.fullmatch(namespace) is None
        or not relative_key
        or any(part in {"", ".", ".."} for part in relative_key.split("/"))
    ):
        return None
    return namespace, relative_key


def _transcript_thread_id(key: str) -> str | None:
    if not key.startswith(TRANSCRIPT_PREFIX):
        return None
    thread_id = key.removeprefix(TRANSCRIPT_PREFIX).split("/", 1)[0]
    return thread_id or None


async def _thread_artifact_lookup(
    request: Request, thread_id: str, key: str
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """Read an auth-filtered thread and find an exact checkpointed ArtifactRef."""

    client = loopback_client(extract_api_key(request.headers))
    lookup_failed = False
    thread: Any = None
    state: Any = None
    try:
        thread = await client.threads.get(thread_id)
        state = await client.threads.get_state(thread_id)
    except NotFoundError:
        return None
    except Exception:
        lookup_failed = True
    if lookup_failed:
        raise HTTPException(status_code=502, detail="pipeline state unavailable")
    normalized_thread = cast(dict[str, Any], dict(thread)) if type(thread) is dict else {}
    return normalized_thread, _find_artifact_ref(state, key)


def _find_artifact_ref(value: Any, key: str) -> dict[str, Any] | None:
    """Find an exact state reference without recursive or fan-out amplification."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    visited: set[int] = set()
    scanned = 0
    while stack and scanned < _MAX_CHECKPOINT_SCAN_NODES:
        current, depth = stack.pop()
        scanned += 1
        if type(current) is dict:
            candidate_key = current.get("key")
            candidate_kind = current.get("kind")
            if type(candidate_key) is str and candidate_key == key and type(candidate_kind) is str:
                return current
            if depth >= _MAX_CHECKPOINT_SCAN_DEPTH or id(current) in visited:
                continue
            visited.add(id(current))
            budget = _MAX_CHECKPOINT_SCAN_NODES - scanned - len(stack)
            if budget > 0:
                stack.extend((nested, depth + 1) for nested in islice(current.values(), budget))
        elif type(current) is list:
            if depth >= _MAX_CHECKPOINT_SCAN_DEPTH or id(current) in visited:
                continue
            visited.add(id(current))
            budget = _MAX_CHECKPOINT_SCAN_NODES - scanned - len(stack)
            if budget > 0:
                stack.extend((nested, depth + 1) for nested in islice(current, budget))
    return None


def _require_artifact_affinity(connection_id: str | None, *, kind: str) -> None:
    if connection_id is None and get_settings().is_locked_down:
        raise HTTPException(
            status_code=503,
            detail=f"{kind} artifact-store affinity requires operator repair",
        )


def _connection_id(value: Any) -> str | None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAX_RECORD_ID_CHARS
        or value != value.strip()
        or "\x00" in value
    ):
        return None
    return value


def _safe_media_type(value: Any, fallback: str) -> str:
    if (
        type(value) is str
        and 1 <= len(value) <= 255
        and value == value.strip()
        and _MEDIA_TYPE_RE.fullmatch(value)
    ):
        return value.lower()
    return fallback
