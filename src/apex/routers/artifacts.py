"""Authenticated, store-affine byte proxy for documents and run artifacts."""

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from itertools import islice
from typing import Annotated, Any, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from langgraph_sdk.errors import NotFoundError

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity
from apex.auth.identity import ConsumerIdentity, ScopeRef
from apex.auth.service import extract_api_key
from apex.domain.input_limits import NoNulStr
from apex.persistence.db import get_sessionmaker
from apex.persistence.models import ArtifactReference, Document, EngineRun
from apex.persistence.repositories.artifact_references import ArtifactReferencesRepository
from apex.persistence.repositories.documents import DocumentsRepository
from apex.persistence.repositories.engine_runs import EngineRunsRepository
from apex.ports.artifact_store import ArtifactStoreBusyError
from apex.services.connections import ConnectionResolver, get_connection_resolver
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


def _artifact_not_found() -> HTTPException:
    """Return a stable 404 without reflecting an arbitrary artifact path."""

    return HTTPException(status_code=404, detail="artifact not found")


@dataclass(frozen=True)
class AuthorizedArtifact:
    media_type: str
    project_id: str | None
    connection_id: str | None


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


@dataclass(frozen=True)
class _ArtifactAuthorizationRows:
    document: _DocumentAuthorizationRow | None = None
    engine_run: _EngineRunAuthorizationRow | None = None
    artifact_reference: _ArtifactReferenceAuthorizationRow | None = None


class _OwnedStream:
    """Close-once owner for a provider iterator, including before first use."""

    def __init__(self, iterator: Any, first: bytes | None, *, exhausted: bool = False) -> None:
        self._iterator = iterator
        self._first = first
        self._has_first = first is not None
        self._closed = exhausted
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
                return first
            try:
                return await anext(self._iterator)
            except StopAsyncIteration:
                self._closed = True
                await _close_stream_iterator(self._iterator)
                raise
            except BaseException:
                self._closed = True
                await _close_stream_iterator(self._iterator)
                raise

    async def aclose(self) -> None:
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
        finally:
            # Starlette does not close an arbitrary async iterator when send()
            # fails or the request is cancelled between handler return and the
            # first body pull. Tie the provider lease to the ASGI call instead.
            await self._owned_stream.aclose()


async def _open_stream(store: Any, key: str) -> _OwnedStream:
    """Open and preflight a stream so a missing object is still a clean 404."""

    iterator = store.iter_bytes(key).__aiter__()
    first: bytes | None
    try:
        first = await anext(iterator)
    except StopAsyncIteration:
        await _close_stream_iterator(iterator)
        return _OwnedStream(iterator, None, exhausted=True)
    except (KeyError, FileNotFoundError):
        await _close_stream_iterator(iterator)
        raise _artifact_not_found() from None
    except ArtifactStoreBusyError:
        await _close_stream_iterator(iterator)
        raise HTTPException(
            status_code=503,
            detail="artifact streaming capacity is busy",
            headers={"Retry-After": "1"},
        ) from None
    except BaseException:
        # The wrapper below does not own the iterator until preflight completes.
        # Close explicitly on cancellation or an arbitrary provider failure so a
        # partially-open response cannot strand a socket/object-store lease.
        await _close_stream_iterator(iterator)
        raise

    return _OwnedStream(iterator, first)


async def _close_stream_iterator(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except BaseException:  # preserve the original stream/cancellation outcome
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
    try:
        store = await resolver.resolve(
            PortKind.ARTIFACT_STORE,
            connection_id=authorized.connection_id,
            project_id=authorized.project_id,
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="artifact store unavailable") from exc

    stream = await _open_stream(store, key)
    try:
        return _OwnedStreamingResponse(stream, media_type=authorized.media_type)
    except BaseException:
        # Construction is synchronous today, but keep ownership correct if a
        # response extension rejects the stream before Starlette starts iterating.
        await _close_stream_iterator(stream)
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
            )
        state_lookup = await _thread_artifact_lookup(request, thread_id, key)
        if state_lookup is None:
            raise _artifact_not_found() from None
        thread, ref = state_lookup
        if ref is None or ref.get("kind") != "transcript":
            raise _artifact_not_found()
        metadata = thread.get("metadata") if isinstance(thread, dict) else None
        project_id = metadata.get("project_id") if isinstance(metadata, dict) else None
        connection_id = _connection_id(ref.get("artifact_connection_id"))
        _require_artifact_affinity(connection_id, kind="transcript")
        return AuthorizedArtifact(
            media_type=_safe_media_type(ref.get("media_type"), "text/plain"),
            project_id=str(project_id) if project_id else None,
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


def _materialize_document(document: Document) -> _DocumentAuthorizationRow:
    return _DocumentAuthorizationRow(
        media_type=document.media_type,
        project_id=document.project_id,
        app_id=document.app_id,
        artifact_connection_id=document.artifact_connection_id,
    )


def _materialize_engine_run(run: EngineRun) -> _EngineRunAuthorizationRow:
    return _EngineRunAuthorizationRow(
        thread_id=run.thread_id,
        project_id=run.project_id,
        app_id=run.app_id,
        ownership_known=(
            run.ownership_known
            and (run.app_id is not None or run.scope_ownership_known)
        ),
        artifact_connection_id=run.artifact_connection_id,
    )


def _materialize_artifact_reference(
    reference: ArtifactReference,
) -> _ArtifactReferenceAuthorizationRow:
    return _ArtifactReferenceAuthorizationRow(
        artifact_key=reference.artifact_key,
        connection_id=reference.connection_id,
        kind=reference.kind,
        thread_id=reference.thread_id,
        project_id=reference.project_id,
        app_id=reference.app_id,
        ownership_known=reference.ownership_known,
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
    try:
        thread = await client.threads.get(thread_id)
        state = await client.threads.get_state(thread_id)
    except NotFoundError:
        return None
    normalized_thread = cast(dict[str, Any], dict(thread)) if isinstance(thread, dict) else {}
    return normalized_thread, _find_artifact_ref(state, key)


def _find_artifact_ref(value: Any, key: str) -> dict[str, Any] | None:
    """Find an exact state reference without recursive or fan-out amplification."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    visited: set[int] = set()
    scanned = 0
    while stack and scanned < _MAX_CHECKPOINT_SCAN_NODES:
        current, depth = stack.pop()
        scanned += 1
        if isinstance(current, dict):
            if current.get("key") == key and isinstance(current.get("kind"), str):
                return current
            if depth >= _MAX_CHECKPOINT_SCAN_DEPTH or id(current) in visited:
                continue
            visited.add(id(current))
            budget = _MAX_CHECKPOINT_SCAN_NODES - scanned - len(stack)
            if budget > 0:
                stack.extend((nested, depth + 1) for nested in islice(current.values(), budget))
        elif isinstance(current, list):
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
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _safe_media_type(value: Any, fallback: str) -> str:
    if isinstance(value, str) and _MEDIA_TYPE_RE.fullmatch(value.strip()):
        return value.strip().lower()
    return fallback
