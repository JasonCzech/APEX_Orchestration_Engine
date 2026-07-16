"""/artifacts proxy: ownership, store affinity, streaming, and 404 behavior."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from langgraph_sdk.errors import NotFoundError
from starlette.requests import ClientDisconnect

from apex.adapters.registry import PortKind
from apex.adapters.stubs.artifact_store import MemoryArtifactStore
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.pipeline import Phase
from apex.graphs.pipeline import phase_subgraph
from apex.persistence.models import ArtifactReference, Document, EngineRun
from apex.ports.artifact_store import ArtifactStoreBusyError, engine_artifact_namespace
from apex.routers import artifacts as artifacts_router
from apex.routers.artifacts import (
    ArtifactAuthorizationRepositories,
    get_artifact_authorization_repository_factory,
    router,
)
from apex.services.connections import ConnectionResolver, get_connection_resolver


class FakeDocumentsRepository:
    def __init__(self) -> None:
        self.rows: dict[str, Document] = {}

    async def get_by_artifact_key(self, artifact_key: str) -> Document | None:
        return next(
            (row for row in self.rows.values() if row.artifact_key == artifact_key),
            None,
        )


class FakeEngineRunsRepository:
    def __init__(self) -> None:
        self.rows: list[EngineRun] = []

    async def get_by_artifact_namespace(
        self,
        artifact_namespace: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None:
        for row in self.rows:
            if row.artifact_namespace != artifact_namespace:
                continue
            if allowed_project_ids is not None and row.project_id not in allowed_project_ids:
                continue
            if allowed_scopes is not None and not _scope_allows_row(allowed_scopes, row):
                continue
            return row
        return None


class FakeArtifactReferencesRepository:
    def __init__(self) -> None:
        self.rows: dict[str, ArtifactReference] = {}

    async def get_exact(self, artifact_key: str) -> ArtifactReference | None:
        return self.rows.get(artifact_key)


def _scope_allows_row(scopes: Sequence[ScopeRef], row: EngineRun) -> bool:
    return any(
        scope.project_id == row.project_id
        and (
            scope.app_id is None
            or scope.app_id == row.app_id
            or (
                row.app_id is None
                and row.ownership_known is True
                and row.scope_ownership_known is True
            )
        )
        for scope in scopes
    )


def _not_found() -> NotFoundError:
    request = httpx.Request("GET", "http://loopback/threads/x")
    return NotFoundError("not found", response=httpx.Response(404, request=request), body=None)


class FakeThreads:
    def __init__(
        self,
        *,
        threads: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.threads = threads or {}
        self.states = states or {}
        self.state_calls: list[str] = []

    async def get(self, thread_id: str) -> dict[str, Any]:
        try:
            return self.threads[thread_id]
        except KeyError:
            raise _not_found() from None

    async def get_state(self, thread_id: str) -> dict[str, Any]:
        self.state_calls.append(thread_id)
        try:
            return self.states[thread_id]
        except KeyError:
            raise _not_found() from None


class FakeLoopbackClient:
    def __init__(self, threads: FakeThreads | None = None) -> None:
        self.threads = threads or FakeThreads()


class IsolatedArtifactStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        async def _chunks() -> AsyncIterator[bytes]:
            try:
                payload = self.objects[key]
            except KeyError:
                raise KeyError(key) from None
            for offset in range(0, len(payload), chunk_size):
                yield payload[offset : offset + chunk_size]

        return _chunks()


class FakeConnectionResolver:
    def __init__(self, default: Any | None = None) -> None:
        self.default = default or MemoryArtifactStore()
        self.stores: dict[str, Any] = {}
        self.calls: list[tuple[PortKind, str | None, str | None]] = []

    async def resolve(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> Any:
        store, _resolved_connection_id = await self.resolve_with_connection_id(
            kind,
            connection_id=connection_id,
            project_id=project_id,
        )
        return store

    async def resolve_with_connection_id(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[Any, str]:
        self.calls.append((kind, connection_id, project_id))
        if connection_id is not None and connection_id in self.stores:
            return self.stores[connection_id], connection_id
        return self.default, connection_id or "dev-artifact-store-memory"


@pytest.fixture(autouse=True)
def clean_artifact_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    MemoryArtifactStore.clear()
    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(),
    )
    yield
    MemoryArtifactStore.clear()


def identity(scopes: list[ScopeRef] | None = None, role: Role = Role.VIEWER) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="viewer",
        consumer_type=ConsumerType.DASHBOARD,
        role=role,
        scopes=scopes or [],
    )


def make_app(
    repo: FakeDocumentsRepository,
    who: ConsumerIdentity | None,
    engine_repo: FakeEngineRunsRepository | None = None,
    resolver: FakeConnectionResolver | None = None,
    artifact_references: FakeArtifactReferencesRepository | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    resolved_engine_repo = engine_repo or FakeEngineRunsRepository()
    resolved_references = artifact_references or FakeArtifactReferencesRepository()

    @asynccontextmanager
    async def open_authorization_repositories() -> AsyncIterator[ArtifactAuthorizationRepositories]:
        yield ArtifactAuthorizationRepositories(
            documents=repo,
            engine_runs=resolved_engine_repo,
            artifact_references=resolved_references,
        )

    app.dependency_overrides[get_artifact_authorization_repository_factory] = lambda: (
        open_authorization_repositories
    )
    app.dependency_overrides[get_connection_resolver] = lambda: resolver or FakeConnectionResolver()
    if who is not None:
        app.dependency_overrides[get_current_identity] = lambda: who
    return app


def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    asyncio.run(MemoryArtifactStore().put(key, data, content_type=content_type))


def document_row(
    doc_id: str,
    key: str,
    media_type: str,
    project_id: str | None,
    *,
    app_id: str | None = None,
    artifact_connection_id: str | None = None,
    size_bytes: int = 1,
) -> Document:
    return Document(
        id=doc_id,
        name=key.rsplit("/", 1)[-1],
        media_type=media_type,
        size_bytes=size_bytes,
        artifact_key=key,
        artifact_connection_id=artifact_connection_id,
        project_id=project_id,
        app_id=app_id,
        created_at=datetime.now(UTC),
    )


def engine_run_row(
    thread_id: str,
    idempotency_key: str,
    project_id: str | None,
    *,
    app_id: str | None = None,
    ownership_known: bool = True,
    artifact_connection_id: str | None = None,
    external_run_id: str = "shared-external-id",
) -> EngineRun:
    return EngineRun(
        id=f"{thread_id}-1",
        thread_id=thread_id,
        project_id=project_id,
        app_id=app_id,
        ownership_known=ownership_known,
        scope_ownership_known=ownership_known,
        attempt=1,
        engine="sim",
        external_run_id=external_run_id,
        artifact_namespace=engine_artifact_namespace(idempotency_key),
        artifact_connection_id=artifact_connection_id,
        handle={},
        status="completed",
        started_at=datetime.now(UTC),
        ended_at=None,
        summary=None,
    )


def artifact_reference_row(
    key: str,
    *,
    connection_id: str,
    kind: str,
    thread_id: str,
    project_id: str | None,
    app_id: str | None = None,
    ownership_known: bool = True,
    size_bytes: int | None = None,
    content_sha256: str | None = None,
) -> ArtifactReference:
    return ArtifactReference(
        id=f"ref-{len(key)}",
        artifact_key=key,
        connection_id=connection_id,
        kind=kind,
        thread_id=thread_id,
        project_id=project_id,
        app_id=app_id,
        ownership_known=ownership_known,
        size_bytes=size_bytes,
        content_sha256=content_sha256,
    )


def test_get_artifact_streams_document_bytes_with_row_media_type() -> None:
    key = "documents/d1/report.html"
    put(key, b"<html>report</html>", content_type="text/html")
    repo = FakeDocumentsRepository()
    repo.rows["d1"] = document_row(
        "d1", key, "text/html", project_id=None, size_bytes=len(b"<html>report</html>")
    )
    app = make_app(repo, identity())
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 200
    assert response.content == b"<html>report</html>"
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    "media_type",
    ["text/html\r\nx-injected: yes", "not-a-media-type", " text/plain ; charset=utf-8 "],
)
def test_get_artifact_sanitizes_legacy_document_media_type(media_type: str) -> None:
    key = "documents/d1/legacy.bin"
    put(key, b"payload")
    repo = FakeDocumentsRepository()
    repo.rows["d1"] = document_row(
        "d1", key, media_type, project_id=None, size_bytes=len(b"payload")
    )
    app = make_app(repo, identity())

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 200
    assert response.content == b"payload"
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert "x-injected" not in repr(response.headers).lower()


def test_get_artifact_uses_document_persisted_store_affinity() -> None:
    key = "documents/d1/report.json"
    selected = IsolatedArtifactStore()
    selected.objects[key] = b'{"source":"selected"}'
    fallback = IsolatedArtifactStore()
    fallback.objects[key] = b'{"source":"wrong"}'
    resolver = FakeConnectionResolver(fallback)
    resolver.stores["artifacts-p1"] = selected
    repo = FakeDocumentsRepository()
    repo.rows["d1"] = document_row(
        "d1",
        key,
        "application/json",
        "p1",
        artifact_connection_id="artifacts-p1",
        size_bytes=len(b'{"source":"selected"}'),
    )
    app = make_app(repo, identity([ScopeRef(project_id="p1")]), resolver=resolver)
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 200
    assert response.json() == {"source": "selected"}
    assert resolver.calls == [(PortKind.ARTIFACT_STORE, "artifacts-p1", "p1")]


def test_get_artifact_rejects_resolver_affinity_drift_before_streaming() -> None:
    key = "documents/d1/report.json"
    store = IsolatedArtifactStore()
    store.objects[key] = b'{"source":"wrong-store"}'

    class DriftingResolver(FakeConnectionResolver):
        async def resolve_with_connection_id(
            self,
            kind: PortKind,
            connection_id: str | None = None,
            project_id: str | None = None,
        ) -> tuple[Any, str]:
            self.calls.append((kind, connection_id, project_id))
            return store, "artifacts-p2"

    resolver = DriftingResolver()
    repo = FakeDocumentsRepository()
    repo.rows["d1"] = document_row(
        "d1",
        key,
        "application/json",
        "p1",
        artifact_connection_id="artifacts-p1",
    )

    with TestClient(
        make_app(repo, identity([ScopeRef(project_id="p1")]), resolver=resolver)
    ) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 503
    assert resolver.calls == [(PortKind.ARTIFACT_STORE, "artifacts-p1", "p1")]


def test_get_artifact_rejects_hostile_resolver_affinity_without_equality_hook() -> None:
    key = "documents/d1/report.json"
    store = IsolatedArtifactStore()
    store.objects[key] = b'{"source":"wrong-store"}'

    class HostileConnectionId(str):
        called = False

        def __eq__(self, other: object) -> bool:
            del other
            self.called = True
            return True

    hostile_id = HostileConnectionId("artifacts-p2")

    class DriftingResolver(FakeConnectionResolver):
        async def resolve_with_connection_id(
            self,
            kind: PortKind,
            connection_id: str | None = None,
            project_id: str | None = None,
        ) -> tuple[Any, str]:
            self.calls.append((kind, connection_id, project_id))
            return store, hostile_id

    resolver = DriftingResolver()
    repo = FakeDocumentsRepository()
    repo.rows["d1"] = document_row(
        "d1",
        key,
        "application/json",
        "p1",
        artifact_connection_id="artifacts-p1",
    )

    with TestClient(
        make_app(repo, identity([ScopeRef(project_id="p1")]), resolver=resolver)
    ) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 503
    assert hostile_id.called is False


def test_locked_runtime_fails_closed_for_legacy_document_without_store_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "documents/d1/legacy.txt"
    repo = FakeDocumentsRepository()
    repo.rows["d1"] = document_row("d1", key, "text/plain", project_id="p1")
    resolver = FakeConnectionResolver()
    monkeypatch.setattr(
        artifacts_router,
        "get_settings",
        lambda: type("LockedSettings", (), {"is_locked_down": True})(),
    )

    with TestClient(
        make_app(
            repo,
            identity([ScopeRef(project_id="p1")]),
            resolver=resolver,
        )
    ) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 503
    assert resolver.calls == []


def test_get_artifact_transcript_key_is_text_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "transcripts/t-1/execution/attempt-1.txt"
    threads = FakeThreads(
        threads={"t-1": {"thread_id": "t-1", "metadata": {"project_id": "p1"}}},
        states={
            "t-1": {
                "values": {
                    "phase_results": {
                        "execution": {
                            "transcript_ref": {
                                "kind": "transcript",
                                "key": key,
                                "media_type": "text/plain",
                                "artifact_connection_id": "artifacts-p1",
                            }
                        }
                    }
                }
            }
        },
    )
    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(threads),
    )
    selected = IsolatedArtifactStore()
    selected.objects[key] = b"phase log"
    resolver = FakeConnectionResolver(IsolatedArtifactStore())
    resolver.stores["artifacts-p1"] = selected
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        resolver=resolver,
    )
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 200
    assert response.content == b"phase log"
    assert response.headers["content-type"].startswith("text/plain")
    assert resolver.calls == [(PortKind.ARTIFACT_STORE, "artifacts-p1", "p1")]


def test_locked_runtime_rejects_legacy_transcript_without_store_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "transcripts/t-legacy/execution/attempt-1.txt"
    threads = FakeThreads(
        threads={"t-legacy": {"thread_id": "t-legacy", "metadata": {"project_id": "p1"}}},
        states={
            "t-legacy": {
                "values": {
                    "transcript_ref": {
                        "kind": "transcript",
                        "key": key,
                        "media_type": "text/plain",
                    }
                }
            }
        },
    )
    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(threads),
    )
    monkeypatch.setattr(
        artifacts_router,
        "get_settings",
        lambda: type("LockedSettings", (), {"is_locked_down": True})(),
    )
    resolver = FakeConnectionResolver()
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        resolver=resolver,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 503
    assert "operator repair" in response.json()["title"]
    assert resolver.calls == []
    assert threads.state_calls == []


def test_get_artifact_transcript_requires_exact_checkpoint_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "transcripts/t-1/execution/attempt-1.txt"
    put(key, b"unreferenced")
    threads = FakeThreads(
        threads={"t-1": {"thread_id": "t-1", "metadata": {}}},
        states={"t-1": {"values": {"phase_results": {}}}},
    )
    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(threads),
    )
    app = make_app(FakeDocumentsRepository(), identity())
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 404


def test_phase_finalize_transcript_can_be_read_through_authorized_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", lambda: ConnectionResolver())
    monkeypatch.setattr(
        phase_subgraph.usage_events, "record_phase_usage_sync", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        phase_subgraph.usage_events, "record_agent_event_sync", lambda *args, **kwargs: None
    )
    state: dict[str, Any] = {
        "phase_results": {
            "story_analysis": {
                "attempt": 1,
                "status": "succeeded",
                "summary": "reviewed checkout",
                "reasoning_digest": "bounded digest",
            }
        }
    }
    update = phase_subgraph._make_finalize(Phase.STORY_ANALYSIS)(  # type: ignore[attr-defined]
        state,  # type: ignore[arg-type]
        {"configurable": {"thread_id": "t-finalize", "project_id": "p1"}},
    )
    ref = update["phase_results"]["story_analysis"]["transcript_ref"]
    threads = FakeThreads(
        threads={
            "t-finalize": {
                "thread_id": "t-finalize",
                "metadata": {"project_id": "p1"},
            }
        },
        states={"t-finalize": {"values": update}},
    )
    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(threads),
    )
    resolver = FakeConnectionResolver()
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        resolver=resolver,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{ref['key']}")

    assert response.status_code == 200
    assert b'"phase": "story_analysis"' in response.content
    assert response.headers["content-type"].startswith("text/plain")
    assert resolver.calls == [(PortKind.ARTIFACT_STORE, "dev-artifact-store-memory", "p1")]


def test_get_artifact_unknown_key_is_404() -> None:
    put("blobs/raw.bin", b"\x00\x01\x02")
    app = make_app(FakeDocumentsRepository(), identity())
    with TestClient(app) as client:
        response = client.get("/v1/artifacts/blobs/raw.bin")
    assert response.status_code == 404


def test_get_artifact_404_does_not_reflect_arbitrary_path_material() -> None:
    canary = "Bearer artifact-path-secret-canary-6e1b"
    app = make_app(FakeDocumentsRepository(), identity())

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/blobs/{canary}")

    assert response.status_code == 404
    assert response.json()["title"] == "artifact not found"
    assert canary not in response.text


def test_get_artifact_detaches_arbitrary_resolver_failure() -> None:
    canary = "artifact-resolver-secret-canary"
    key = "documents/d1/report.json"
    repo = FakeDocumentsRepository()
    repo.rows["d1"] = document_row(
        "d1",
        key,
        "application/json",
        "p1",
        artifact_connection_id="artifacts-p1",
    )

    class FailingResolver(FakeConnectionResolver):
        async def resolve_with_connection_id(
            self,
            kind: PortKind,
            connection_id: str | None = None,
            project_id: str | None = None,
        ) -> tuple[Any, str]:
            raise RuntimeError(canary)

    with TestClient(
        make_app(
            repo,
            identity([ScopeRef(project_id="p1")]),
            resolver=FailingResolver(),
        )
    ) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 503
    assert response.json()["title"] == "artifact store unavailable"
    assert canary not in response.text


async def test_authorization_session_closes_before_queued_stream_preflight() -> None:
    key = "documents/d1/queued.bin"
    documents = FakeDocumentsRepository()
    documents.rows["d1"] = document_row(
        "d1",
        key,
        "application/octet-stream",
        project_id="p1",
        artifact_connection_id="artifacts-p1",
        size_bytes=len(b"payload"),
    )
    session_closed = asyncio.Event()
    stream_queued = asyncio.Event()
    stream_admission = asyncio.Semaphore(0)
    lifecycle: list[str] = []

    @asynccontextmanager
    async def open_repositories() -> AsyncIterator[ArtifactAuthorizationRepositories]:
        lifecycle.append("session-open")
        try:
            yield ArtifactAuthorizationRepositories(
                documents=documents,
                engine_runs=FakeEngineRunsRepository(),
                artifact_references=FakeArtifactReferencesRepository(),
            )
        finally:
            lifecycle.append("session-closed")
            session_closed.set()

    class QueuedIterator:
        def __init__(self) -> None:
            self.sent = False
            self.closed = False

        def __aiter__(self) -> "QueuedIterator":
            return self

        async def __anext__(self) -> bytes:
            if self.sent:
                raise StopAsyncIteration
            lifecycle.append("stream-queued")
            stream_queued.set()
            await stream_admission.acquire()
            self.sent = True
            return b"payload"

        async def aclose(self) -> None:
            self.closed = True

    iterator = QueuedIterator()

    class Store:
        def iter_bytes(self, _key: str) -> QueuedIterator:
            return iterator

    class Resolver:
        async def resolve_with_connection_id(
            self,
            kind: PortKind,
            connection_id: str | None = None,
            project_id: str | None = None,
        ) -> tuple[Store, str]:
            assert kind is PortKind.ARTIFACT_STORE
            assert connection_id == "artifacts-p1"
            assert project_id == "p1"
            assert session_closed.is_set()
            lifecycle.append("resolved")
            return Store(), "artifacts-p1"

    task = asyncio.create_task(
        artifacts_router.get_artifact(
            key=key,
            identity=identity([ScopeRef(project_id="p1")]),
            resolver=Resolver(),  # type: ignore[arg-type]
            authorization_repositories=open_repositories,
            request=Request({"type": "http", "headers": []}),
        )
    )
    await asyncio.wait_for(stream_queued.wait(), timeout=1)

    assert task.done() is False
    assert lifecycle == ["session-open", "session-closed", "resolved", "stream-queued"]

    stream_admission.release()
    response = await asyncio.wait_for(task, timeout=1)
    assert isinstance(response, artifacts_router._OwnedStreamingResponse)
    await response._owned_stream.aclose()
    assert iterator.closed is True


async def test_artifact_stream_preflight_cancellation_closes_provider_iterator() -> None:
    class CancellingIterator:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self) -> "CancellingIterator":
            return self

        async def __anext__(self) -> bytes:
            raise asyncio.CancelledError

        async def aclose(self) -> None:
            self.closed = True

    iterator = CancellingIterator()

    class Store:
        def iter_bytes(self, _key: str) -> CancellingIterator:
            return iterator

    with pytest.raises(asyncio.CancelledError):
        await artifacts_router._open_stream(Store(), "documents/d1/file.bin")

    assert iterator.closed is True


async def test_artifact_stream_busy_fails_fast_and_closes_provider_iterator() -> None:
    class BusyIterator:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self) -> "BusyIterator":
            return self

        async def __anext__(self) -> bytes:
            raise ArtifactStoreBusyError("busy")

        async def aclose(self) -> None:
            self.closed = True

    iterator = BusyIterator()

    class Store:
        def iter_bytes(self, _key: str) -> BusyIterator:
            return iterator

    with pytest.raises(HTTPException) as excinfo:
        await asyncio.wait_for(
            artifacts_router._open_stream(Store(), "documents/d1/file.bin"),
            timeout=0.1,
        )

    assert excinfo.value.status_code == 503
    assert excinfo.value.headers == {"Retry-After": "1"}
    assert iterator.closed is True


async def test_artifact_stream_preflight_detaches_arbitrary_provider_failure() -> None:
    canary = "artifact-provider-preflight-secret-canary"

    class FailingIterator:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self) -> "FailingIterator":
            return self

        async def __anext__(self) -> bytes:
            raise RuntimeError(canary)

        async def aclose(self) -> None:
            self.closed = True

    iterator = FailingIterator()

    class Store:
        def iter_bytes(self, _key: str) -> FailingIterator:
            return iterator

    with pytest.raises(HTTPException) as caught:
        await artifacts_router._open_stream(Store(), "documents/d1/file.bin")

    assert caught.value.status_code == 502
    assert caught.value.detail == "artifact store read failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert canary not in str(caught.value)
    assert iterator.closed is True


async def test_artifact_stream_midread_detaches_arbitrary_provider_failure() -> None:
    canary = "artifact-provider-midstream-secret-canary"

    class FailingIterator:
        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        def __aiter__(self) -> "FailingIterator":
            return self

        async def __anext__(self) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"prefetched"
            raise RuntimeError(canary)

        async def aclose(self) -> None:
            self.closed = True

    iterator = FailingIterator()

    class Store:
        def iter_bytes(self, _key: str) -> FailingIterator:
            return iterator

    stream = await artifacts_router._open_stream(Store(), "documents/d1/file.bin")
    assert await anext(stream) == b"prefetched"

    with pytest.raises(artifacts_router._ArtifactStreamReadError) as caught:
        await anext(stream)

    assert str(caught.value) == "artifact stream read failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert canary not in str(caught.value)
    assert iterator.closed is True


async def test_artifact_stream_preflight_rejects_non_bytes_without_coercion() -> None:
    class HostileChunk:
        len_called = False

        def __len__(self) -> int:
            self.len_called = True
            raise AssertionError("malformed chunks must not be coerced")

    hostile = HostileChunk()

    class MalformedIterator:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self) -> "MalformedIterator":
            return self

        async def __anext__(self) -> Any:
            return hostile

        async def aclose(self) -> None:
            self.closed = True

    iterator = MalformedIterator()

    class Store:
        def iter_bytes(self, _key: str) -> MalformedIterator:
            return iterator

    with pytest.raises(HTTPException) as exc_info:
        await artifacts_router._open_stream(Store(), "documents/d1/file.bin")

    assert exc_info.value.status_code == 502
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert hostile.len_called is False
    assert iterator.closed is True


async def test_artifact_stream_midread_rejects_oversized_chunk_and_closes() -> None:
    class OversizedIterator:
        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        def __aiter__(self) -> "OversizedIterator":
            return self

        async def __anext__(self) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"first"
            return b"x" * (artifacts_router._MAX_ARTIFACT_STREAM_CHUNK_BYTES + 1)

        async def aclose(self) -> None:
            self.closed = True

    iterator = OversizedIterator()

    class Store:
        def iter_bytes(self, _key: str) -> OversizedIterator:
            return iterator

    stream = await artifacts_router._open_stream(Store(), "documents/d1/file.bin")
    assert await anext(stream) == b"first"
    with pytest.raises(artifacts_router._ArtifactStreamReadError) as exc_info:
        await anext(stream)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert iterator.closed is True


@pytest.mark.parametrize(
    ("expected_size", "expected_digest"),
    [
        (4, None),
        (2, sha256(b"different").hexdigest()),
    ],
)
async def test_artifact_stream_requires_exact_durable_content_identity(
    expected_size: int,
    expected_digest: str | None,
) -> None:
    class TruncatedOrReplacedIterator:
        def __init__(self) -> None:
            self.sent = False
            self.closed = False

        def __aiter__(self) -> "TruncatedOrReplacedIterator":
            return self

        async def __anext__(self) -> bytes:
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return b"ok"

        async def aclose(self) -> None:
            self.closed = True

    iterator = TruncatedOrReplacedIterator()

    class Store:
        def iter_bytes(self, _key: str) -> TruncatedOrReplacedIterator:
            return iterator

    stream = await artifacts_router._open_stream(
        Store(),
        "engine-runs/key/result.bin",
        expected_size=expected_size,
        expected_sha256=expected_digest,
    )
    assert await anext(stream) == b"ok"
    with pytest.raises(artifacts_router._ArtifactStreamReadError):
        await anext(stream)

    assert iterator.closed is True


async def test_legacy_artifact_stream_has_hard_total_byte_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifacts_router, "_MAX_LEGACY_ARTIFACT_STREAM_BYTES", 3)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"ab"
        yield b"cd"

    iterator = chunks()
    stream = artifacts_router._OwnedStream(iterator, await anext(iterator))
    assert await anext(stream) == b"ab"
    with pytest.raises(artifacts_router._ArtifactStreamReadError):
        await anext(stream)


async def test_prefetched_artifact_stream_can_close_before_first_body_iteration() -> None:
    class TrackingIterator:
        def __init__(self) -> None:
            self.closed = False
            self.sent = False

        def __aiter__(self) -> "TrackingIterator":
            return self

        async def __anext__(self) -> bytes:
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return b"prefetched"

        async def aclose(self) -> None:
            self.closed = True

    iterator = TrackingIterator()

    class Store:
        def iter_bytes(self, _key: str) -> TrackingIterator:
            return iterator

    stream = await artifacts_router._open_stream(Store(), "documents/d1/file.bin")
    await stream.aclose()

    assert iterator.closed is True
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_artifact_stream_close_waits_for_inflight_read() -> None:
    read_started = asyncio.Event()
    release_read = asyncio.Event()

    class TrackingIterator:
        def __init__(self) -> None:
            self.reading = False
            self.closed = False
            self.close_raced = False

        def __aiter__(self) -> "TrackingIterator":
            return self

        async def __anext__(self) -> bytes:
            self.reading = True
            read_started.set()
            try:
                await release_read.wait()
                return b"payload"
            finally:
                self.reading = False

        async def aclose(self) -> None:
            self.close_raced = self.reading
            self.closed = True

    iterator = TrackingIterator()
    stream = artifacts_router._OwnedStream(iterator, None)
    read_task = asyncio.create_task(anext(stream))
    await read_started.wait()
    close_task = asyncio.create_task(stream.aclose())
    await asyncio.sleep(0)

    assert close_task.done() is False
    release_read.set()
    assert await read_task == b"payload"
    await close_task
    assert iterator.closed is True
    assert iterator.close_raced is False


async def test_artifact_response_send_failure_closes_provider_iterator() -> None:
    class TrackingIterator:
        def __init__(self) -> None:
            self.closed = False
            self.sent = False

        def __aiter__(self) -> "TrackingIterator":
            return self

        async def __anext__(self) -> bytes:
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return b"payload"

        async def aclose(self) -> None:
            self.closed = True

    iterator = TrackingIterator()

    class Store:
        def iter_bytes(self, _key: str) -> TrackingIterator:
            return iterator

    stream = await artifacts_router._open_stream(Store(), "documents/d1/file.bin")
    response = artifacts_router._OwnedStreamingResponse(
        stream,
        media_type="application/octet-stream",
    )

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    with pytest.raises(ClientDisconnect):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            receive,
            send,
        )

    assert iterator.closed is True


@pytest.mark.parametrize("initial_outcome", ["cancelled", "error"])
async def test_artifact_response_close_survives_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    initial_outcome: str,
) -> None:
    response_entered = asyncio.Event()
    close_entered = asyncio.Event()
    allow_close = asyncio.Event()

    class StreamError(RuntimeError):
        pass

    class BlockingCloseIterator:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self) -> "BlockingCloseIterator":
            return self

        async def __anext__(self) -> bytes:
            raise AssertionError("the response body is replaced by this focused test")

        async def aclose(self) -> None:
            close_entered.set()
            await allow_close.wait()
            self.closed = True

    async def response_call(
        _response: Any,
        _scope: Any,
        _receive: Any,
        _send: Any,
    ) -> None:
        response_entered.set()
        if initial_outcome == "error":
            raise StreamError("send failed")
        await asyncio.Event().wait()

    monkeypatch.setattr(artifacts_router.StreamingResponse, "__call__", response_call)
    iterator = BlockingCloseIterator()
    response = artifacts_router._OwnedStreamingResponse(
        artifacts_router._OwnedStream(iterator, b"prefetched"),
        media_type="application/octet-stream",
    )
    response_task = asyncio.create_task(response({}, None, None))
    await response_entered.wait()
    if initial_outcome == "cancelled":
        response_task.cancel()
    await close_entered.wait()

    # Deliver cancellation while the original cancellation/error is unwinding
    # through provider cleanup, as can happen during disconnect plus shutdown.
    response_task.cancel()
    await asyncio.sleep(0)
    response_task.cancel()
    await asyncio.sleep(0)
    assert response_task.done() is False
    allow_close.set()

    expected = asyncio.CancelledError if initial_outcome == "cancelled" else StreamError
    with pytest.raises(expected):
        await response_task
    assert iterator.closed is True


def test_get_artifact_engine_namespace_requires_project_and_app_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = engine_artifact_namespace("run-p1-a1")
    key = f"{namespace}/results.json"
    put(key, b"{}", content_type="application/json")
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(engine_run_row("t-1", "run-p1-a1", "p1", app_id="app-a"))
    threads = FakeThreads(
        threads={"t-1": {"thread_id": "t-1", "metadata": {"project_id": "p1"}}},
        states={
            "t-1": {
                "values": {
                    "artifacts": [
                        {"kind": "engine_results", "key": key, "media_type": "application/json"}
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(threads),
    )

    allowed_app = make_app(
        FakeDocumentsRepository(),
        identity(scopes=[ScopeRef(project_id="p1", app_id="app-a")]),
        engine_repo,
    )
    with TestClient(allowed_app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 200

    other_app = make_app(
        FakeDocumentsRepository(),
        identity(scopes=[ScopeRef(project_id="p1", app_id="app-b")]),
        engine_repo,
    )
    with TestClient(other_app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 404


def test_get_artifact_app_scope_hides_legacy_run_with_unknown_owner() -> None:
    namespace = engine_artifact_namespace("legacy-run")
    key = f"{namespace}/results.json"
    put(key, b"{}", content_type="application/json")
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(
        engine_run_row(
            "t-legacy",
            "legacy-run",
            "p1",
            app_id=None,
            ownership_known=False,
        )
    )

    app = make_app(
        FakeDocumentsRepository(),
        identity(scopes=[ScopeRef(project_id="p1", app_id="app-a")]),
        engine_repo,
    )
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 404


def test_get_artifact_engine_key_uses_namespace_not_colliding_external_id() -> None:
    p1 = engine_run_row("t-1", "private-p1", "p1")
    p2 = engine_run_row("t-2", "private-p2", "p2")
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.extend([p1, p2])
    key = f"{p1.artifact_namespace}/results.json"
    put(key, b"p1")
    app = make_app(
        FakeDocumentsRepository(),
        identity(scopes=[ScopeRef(project_id="p2")]),
        engine_repo,
    )
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 404


def test_get_artifact_engine_key_uses_persisted_store_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = engine_run_row(
        "t-1",
        "run-p1-a1",
        "p1",
        artifact_connection_id="artifacts-p1",
    )
    key = f"{run.artifact_namespace}/results.json"
    selected = IsolatedArtifactStore()
    selected.objects[key] = b"selected"
    resolver = FakeConnectionResolver(IsolatedArtifactStore())
    resolver.stores["artifacts-p1"] = selected
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    threads = FakeThreads(
        threads={"t-1": {"thread_id": "t-1", "metadata": {"project_id": "p1"}}},
        states={
            "t-1": {
                "values": {
                    "artifacts": [
                        {
                            "kind": "engine_results",
                            "key": key,
                            "media_type": "application/octet-stream",
                            "artifact_connection_id": "artifacts-p1",
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(threads),
    )
    app = make_app(
        FakeDocumentsRepository(),
        identity(scopes=[ScopeRef(project_id="p1")]),
        engine_repo,
        resolver,
    )
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 200
    assert response.content == b"selected"
    assert resolver.calls == [(PortKind.ARTIFACT_STORE, "artifacts-p1", "p1")]


def test_get_engine_artifact_does_not_treat_missing_owned_thread_as_legacy() -> None:
    run = engine_run_row("t-known", "run-known", "p1", ownership_known=True)
    key = f"{run.artifact_namespace}/results.json"
    put(key, b"must-not-leak")
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        engine_repo,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 404


def test_get_engine_artifact_keeps_projection_fallback_for_unknown_legacy_owner() -> None:
    run = engine_run_row("t-legacy", "run-legacy", "p1", ownership_known=False)
    key = f"{run.artifact_namespace}/results.json"
    put(key, b"legacy")
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        engine_repo,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 200
    assert response.content == b"legacy"


def test_locked_runtime_rejects_legacy_engine_projection_without_store_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = engine_run_row("t-legacy", "run-legacy", "p1", ownership_known=False)
    key = f"{run.artifact_namespace}/results.json"
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    resolver = FakeConnectionResolver()
    monkeypatch.setattr(
        artifacts_router,
        "get_settings",
        lambda: type("LockedSettings", (), {"is_locked_down": True})(),
    )
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        engine_repo,
        resolver,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 503
    assert "operator repair" in response.json()["title"]
    assert resolver.calls == []


def test_locked_runtime_rejects_legacy_engine_checkpoint_without_store_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = engine_run_row("t-known", "run-known", "p1", ownership_known=True)
    key = f"{run.artifact_namespace}/results.json"
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    threads = FakeThreads(
        threads={"t-known": {"thread_id": "t-known", "metadata": {"project_id": "p1"}}},
        states={
            "t-known": {
                "values": {
                    "artifacts": [
                        {
                            "kind": "engine_results",
                            "key": key,
                            "media_type": "application/json",
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(threads),
    )
    monkeypatch.setattr(
        artifacts_router,
        "get_settings",
        lambda: type("LockedSettings", (), {"is_locked_down": True})(),
    )
    resolver = FakeConnectionResolver()
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        engine_repo,
        resolver,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 503
    assert "operator repair" in response.json()["title"]
    assert resolver.calls == []
    assert threads.state_calls == []


def test_get_engine_artifact_uses_exact_durable_reference_after_thread_retention() -> None:
    run = engine_run_row(
        "t-retained",
        "run-retained",
        "p1",
        app_id="app-a",
        artifact_connection_id="artifacts-p1",
    )
    key = f"{run.artifact_namespace}/results.json"
    selected = IsolatedArtifactStore()
    selected.objects[key] = b"durable"
    resolver = FakeConnectionResolver(IsolatedArtifactStore())
    resolver.stores["artifacts-p1"] = selected
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    references = FakeArtifactReferencesRepository()
    references.rows[key] = artifact_reference_row(
        key,
        connection_id="artifacts-p1",
        kind="engine_results",
        thread_id="t-retained",
        project_id="p1",
        app_id="app-a",
        size_bytes=len(b"durable"),
        content_sha256=sha256(b"durable").hexdigest(),
    )
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1", app_id="app-a")]),
        engine_repo,
        resolver,
        references,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 200
    assert response.content == b"durable"
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert resolver.calls == [(PortKind.ARTIFACT_STORE, "artifacts-p1", "p1")]


def test_ambiguous_durable_reference_is_hidden_from_app_only_scope() -> None:
    key = "transcripts/t-legacy/execution/attempt-1.txt"
    put(key, b"legacy")
    references = FakeArtifactReferencesRepository()
    references.rows[key] = artifact_reference_row(
        key,
        connection_id="artifacts-p1",
        kind="transcript",
        thread_id="t-legacy",
        project_id="p1",
        app_id=None,
        ownership_known=False,
    )

    app_only = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1", app_id="app-a")]),
        artifact_references=references,
    )
    with TestClient(app_only) as client:
        denied = client.get(f"/v1/artifacts/{key}")

    project_wide = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        artifact_references=references,
    )
    with TestClient(project_wide) as client:
        allowed = client.get(f"/v1/artifacts/{key}")

    assert denied.status_code == 404
    assert allowed.status_code == 200
    assert allowed.content == b"legacy"


def test_durable_engine_reference_never_authorizes_namespace_wildcard() -> None:
    run = engine_run_row(
        "t-retained",
        "run-retained",
        "p1",
        artifact_connection_id="artifacts-p1",
    )
    indexed_key = f"{run.artifact_namespace}/results.json"
    unindexed_key = f"{run.artifact_namespace}/private.log"
    put(unindexed_key, b"private")
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    references = FakeArtifactReferencesRepository()
    references.rows[indexed_key] = artifact_reference_row(
        indexed_key,
        connection_id="artifacts-p1",
        kind="engine_results",
        thread_id="t-retained",
        project_id="p1",
    )
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        engine_repo,
        artifact_references=references,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{unindexed_key}")

    assert response.status_code == 404


def test_durable_engine_reference_rejects_affinity_or_scope_mismatch() -> None:
    run = engine_run_row(
        "t-retained",
        "run-retained",
        "p1",
        app_id="app-a",
        artifact_connection_id="artifacts-p1",
    )
    key = f"{run.artifact_namespace}/results.json"
    put(key, b"wrong")
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    references = FakeArtifactReferencesRepository()
    references.rows[key] = artifact_reference_row(
        key,
        connection_id="artifacts-p2",
        kind="engine_results",
        thread_id="t-retained",
        project_id="p1",
        app_id="app-a",
    )
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1", app_id="app-a")]),
        engine_repo,
        artifact_references=references,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 404


def test_get_engine_artifact_rejects_checkpoint_store_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = engine_run_row(
        "t-1",
        "run-p1-a1",
        "p1",
        artifact_connection_id="artifacts-p1",
    )
    key = f"{run.artifact_namespace}/results.json"
    threads = FakeThreads(
        threads={"t-1": {"thread_id": "t-1", "metadata": {"project_id": "p1"}}},
        states={
            "t-1": {
                "values": {
                    "artifacts": [
                        {
                            "kind": "engine_results",
                            "key": key,
                            "media_type": "application/json",
                            "artifact_connection_id": "artifacts-p2",
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(threads),
    )
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    resolver = FakeConnectionResolver()
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        engine_repo,
        resolver,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 404
    assert resolver.calls == []


def test_get_engine_artifact_sanitizes_checkpoint_media_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = engine_run_row("t-1", "run-p1-a1", "p1")
    key = f"{run.artifact_namespace}/results.json"
    put(key, b"payload")
    threads = FakeThreads(
        threads={"t-1": {"thread_id": "t-1", "metadata": {"project_id": "p1"}}},
        states={
            "t-1": {
                "values": {
                    "artifacts": [
                        {
                            "kind": "engine_results",
                            "key": key,
                            "media_type": "text/html\r\nx-injected: yes",
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(threads),
    )
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    app = make_app(FakeDocumentsRepository(), identity([ScopeRef(project_id="p1")]), engine_repo)

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")


def test_get_engine_artifact_detaches_arbitrary_loopback_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "artifact-loopback-secret-canary"
    run = engine_run_row("t-1", "run-p1-a1", "p1")
    key = f"{run.artifact_namespace}/results.json"

    class FailingThreads(FakeThreads):
        async def get(self, thread_id: str) -> dict[str, Any]:
            assert thread_id == "t-1"
            raise RuntimeError(canary)

    monkeypatch.setattr(
        artifacts_router,
        "loopback_client",
        lambda _api_key: FakeLoopbackClient(FailingThreads()),
    )
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(run)
    resolver = FakeConnectionResolver()
    app = make_app(
        FakeDocumentsRepository(),
        identity([ScopeRef(project_id="p1")]),
        engine_repo,
        resolver,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")

    assert response.status_code == 502
    assert response.json()["title"] == "pipeline state unavailable"
    assert canary not in response.text
    assert resolver.calls == []


def test_get_artifact_missing_is_404_problem() -> None:
    repo = FakeDocumentsRepository()
    key = "documents/d1/missing.txt"
    repo.rows["d1"] = document_row("d1", key, "text/plain", project_id=None)
    app = make_app(repo, identity())
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_get_artifact_out_of_scope_document_is_404() -> None:
    key = "documents/d2/secret.txt"
    put(key, b"secret", content_type="text/plain")
    repo = FakeDocumentsRepository()
    repo.rows["d2"] = document_row("d2", key, "text/plain", project_id="p2")
    app = make_app(repo, identity(scopes=[ScopeRef(project_id="p1")], role=Role.OPERATOR))
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 404


def test_get_artifact_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", "k")
    app = make_app(FakeDocumentsRepository(), who=None)
    with TestClient(app) as client:
        response = client.get("/v1/artifacts/blobs/raw.bin")
    assert response.status_code == 401


def test_checkpoint_artifact_scan_is_depth_and_cycle_bounded() -> None:
    state: dict[str, Any] = {}
    current = state
    for _ in range(1_000):
        nested: dict[str, Any] = {}
        current["next"] = nested
        current = nested
    current["next"] = state
    current["artifact"] = {"kind": "transcript", "key": "too-deep"}

    assert artifacts_router._find_artifact_ref(state, "too-deep") is None


def test_checkpoint_artifact_scan_has_a_hard_fanout_budget() -> None:
    key = "transcripts/t-1/execution/attempt-1.txt"
    values: list[dict[str, str]] = [
        {"kind": "other", "key": f"unrelated-{index}"}
        for index in range(artifacts_router._MAX_CHECKPOINT_SCAN_NODES)
    ]
    values[-1] = {"kind": "transcript", "key": key}

    assert artifacts_router._find_artifact_ref(values, key) is None


def test_checkpoint_artifact_scan_rejects_hostile_scalar_subclasses_without_hooks() -> None:
    class HostileString(str):
        compared = False

        def __eq__(self, other: object) -> bool:
            self.compared = True
            raise AssertionError("provider scalar comparison must not run")

    candidate = HostileString("transcripts/t-1/execution/attempt-1.txt")
    state = {"kind": "transcript", "key": candidate}

    assert artifacts_router._find_artifact_ref(state, str(candidate)) is None
    assert candidate.compared is False


def test_checkpoint_artifact_affinity_rejects_hostile_scalar_without_hooks() -> None:
    class HostileConnectionId(str):
        called = False

        def strip(self, *_args: Any, **_kwargs: Any) -> str:
            self.called = True
            raise AssertionError("checkpoint scalar hooks must not run")

    candidate = HostileConnectionId("artifacts-p1")

    assert artifacts_router._connection_id(candidate) is None
    assert candidate.called is False
