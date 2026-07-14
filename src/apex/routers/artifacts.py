"""Authenticated, store-affine byte proxy for documents and run artifacts."""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph_sdk.errors import NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity
from apex.auth.identity import ConsumerIdentity
from apex.auth.service import extract_api_key
from apex.persistence.db import get_session
from apex.persistence.repositories.documents import DocumentsRepository
from apex.persistence.repositories.engine_runs import EngineRunsRepository
from apex.services.connections import ConnectionResolver, get_connection_resolver
from apex.services.documents import get_documents_repository
from apex.services.langgraph_client import loopback_client

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

RepositoryDep = Annotated[DocumentsRepository, Depends(get_documents_repository)]
ConnectionResolverDep = Annotated[ConnectionResolver, Depends(get_connection_resolver)]

FALLBACK_MEDIA_TYPE = "application/octet-stream"
TRANSCRIPT_PREFIX = "transcripts/"
ENGINE_RUN_PREFIX = "engine-runs/"
_ENGINE_NAMESPACE_RE = re.compile(r"^engine-runs/[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")


@dataclass(frozen=True)
class AuthorizedArtifact:
    media_type: str
    project_id: str | None
    connection_id: str | None


def get_engine_runs_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EngineRunsRepository:
    return EngineRunsRepository(session)


EngineRunsRepoDep = Annotated[EngineRunsRepository, Depends(get_engine_runs_repository)]


async def _open_stream(store: Any, key: str) -> AsyncIterator[bytes]:
    """Open and preflight a stream so a missing object is still a clean 404."""

    iterator = store.iter_bytes(key).__aiter__()
    first: bytes | None
    try:
        first = await anext(iterator)
    except StopAsyncIteration:
        first = None
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail=f"artifact {key!r} not found") from None

    async def _chunks() -> AsyncIterator[bytes]:
        try:
            if first is not None:
                yield first
            async for chunk in iterator:
                yield chunk
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    return _chunks()


@router.get("/{key:path}", operation_id="getArtifact")
async def get_artifact(
    key: str,
    identity: CurrentIdentity,
    resolver: ConnectionResolverDep,
    repository: RepositoryDep,
    engine_runs: EngineRunsRepoDep,
    request: Request,
) -> StreamingResponse:
    authorized = await _authorize_artifact_key(key, identity, repository, engine_runs, request)
    try:
        store = await resolver.resolve(
            PortKind.ARTIFACT_STORE,
            connection_id=authorized.connection_id,
            project_id=authorized.project_id,
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="artifact store unavailable") from exc

    stream = await _open_stream(store, key)
    return StreamingResponse(stream, media_type=authorized.media_type)


async def _authorize_artifact_key(
    key: str,
    identity: ConsumerIdentity,
    documents: DocumentsRepository,
    engine_runs: EngineRunsRepository,
    request: Request,
) -> AuthorizedArtifact:
    document = await documents.get_by_artifact_key(key)
    if document is not None:
        if document.project_id is None or identity.allows_scope(
            project_id=document.project_id, app_id=document.app_id
        ):
            return AuthorizedArtifact(
                media_type=document.media_type,
                project_id=document.project_id,
                connection_id=document.artifact_connection_id,
            )
        raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")

    if key.startswith(ENGINE_RUN_PREFIX):
        parts = key.split("/", 2)
        namespace = "/".join(parts[:2]) if len(parts) == 3 else ""
        relative_key = parts[2] if len(parts) == 3 else ""
        if (
            _ENGINE_NAMESPACE_RE.fullmatch(namespace) is None
            or not relative_key
            or any(part in {"", ".", ".."} for part in relative_key.split("/"))
        ):
            raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")
        allowed = None if identity.is_unscoped else identity.scopes
        row = await engine_runs.get_by_artifact_namespace(namespace, allowed_scopes=allowed)
        if row is not None:
            state_lookup = await _thread_artifact_lookup(request, row.thread_id, key)
            if state_lookup is not None:
                _thread, ref = state_lookup
                if ref is None:
                    raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")
                media_type = _safe_media_type(ref.get("media_type"), FALLBACK_MEDIA_TYPE)
                row_connection_id = _connection_id(row.artifact_connection_id)
                ref_connection_id = _connection_id(ref.get("artifact_connection_id"))
                if (
                    row_connection_id is not None
                    and ref_connection_id is not None
                    and row_connection_id != ref_connection_id
                ):
                    raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")
                connection_id = row_connection_id or ref_connection_id
            else:
                # Projection-only legacy rows predate exact ArtifactRefs.
                media_type = FALLBACK_MEDIA_TYPE
                connection_id = row.artifact_connection_id
            return AuthorizedArtifact(
                media_type=media_type,
                project_id=row.project_id,
                connection_id=connection_id,
            )
        raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")

    if key.startswith(TRANSCRIPT_PREFIX):
        thread_id = key.removeprefix(TRANSCRIPT_PREFIX).split("/", 1)[0]
        if not thread_id:
            raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")
        state_lookup = await _thread_artifact_lookup(request, thread_id, key)
        if state_lookup is None:
            raise HTTPException(status_code=404, detail=f"artifact {key!r} not found") from None
        thread, ref = state_lookup
        if ref is None or ref.get("kind") != "transcript":
            raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")
        metadata = thread.get("metadata") if isinstance(thread, dict) else None
        project_id = metadata.get("project_id") if isinstance(metadata, dict) else None
        connection_id = _connection_id(ref.get("artifact_connection_id"))
        return AuthorizedArtifact(
            media_type=_safe_media_type(ref.get("media_type"), "text/plain"),
            project_id=str(project_id) if project_id else None,
            connection_id=connection_id,
        )

    raise HTTPException(status_code=404, detail=f"artifact {key!r} not found")


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
    if isinstance(value, dict):
        if value.get("key") == key and isinstance(value.get("kind"), str):
            return value
        for nested in value.values():
            found = _find_artifact_ref(nested, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_artifact_ref(nested, key)
            if found is not None:
                return found
    return None


def _connection_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _safe_media_type(value: Any, fallback: str) -> str:
    if isinstance(value, str) and _MEDIA_TYPE_RE.fullmatch(value.strip()):
        return value.strip().lower()
    return fallback
