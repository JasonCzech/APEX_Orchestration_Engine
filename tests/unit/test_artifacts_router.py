"""/artifacts proxy: ownership, store affinity, streaming, and 404 behavior."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph_sdk.errors import NotFoundError

from apex.adapters.registry import PortKind
from apex.adapters.stubs.artifact_store import MemoryArtifactStore
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.pipeline import Phase
from apex.graphs.pipeline import phase_subgraph
from apex.persistence.models import Document, EngineRun
from apex.ports.artifact_store import engine_artifact_namespace
from apex.routers import artifacts as artifacts_router
from apex.routers.artifacts import get_engine_runs_repository, router
from apex.services.connections import ConnectionResolver, get_connection_resolver
from apex.services.documents import get_documents_repository


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


def _scope_allows_row(scopes: Sequence[ScopeRef], row: EngineRun) -> bool:
    return any(
        scope.project_id == row.project_id
        and (
            scope.app_id is None
            or scope.app_id == row.app_id
            or (row.app_id is None and row.ownership_known is True)
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

    async def get(self, thread_id: str) -> dict[str, Any]:
        try:
            return self.threads[thread_id]
        except KeyError:
            raise _not_found() from None

    async def get_state(self, thread_id: str) -> dict[str, Any]:
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
        self.calls.append((kind, connection_id, project_id))
        if connection_id is not None and connection_id in self.stores:
            return self.stores[connection_id]
        return self.default


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
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_documents_repository] = lambda: repo
    app.dependency_overrides[get_engine_runs_repository] = lambda: (
        engine_repo or FakeEngineRunsRepository()
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
) -> Document:
    return Document(
        id=doc_id,
        name=key.rsplit("/", 1)[-1],
        media_type=media_type,
        size_bytes=1,
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


def test_get_artifact_streams_document_bytes_with_row_media_type() -> None:
    key = "documents/d1/report.html"
    put(key, b"<html>report</html>", content_type="text/html")
    repo = FakeDocumentsRepository()
    repo.rows["d1"] = document_row("d1", key, "text/html", project_id=None)
    app = make_app(repo, identity())
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 200
    assert response.content == b"<html>report</html>"
    assert response.headers["content-type"].startswith("text/html")


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
    )
    app = make_app(repo, identity([ScopeRef(project_id="p1")]), resolver=resolver)
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 200
    assert response.json() == {"source": "selected"}
    assert resolver.calls == [(PortKind.ARTIFACT_STORE, "artifacts-p1", "p1")]


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


def test_get_artifact_engine_namespace_requires_project_and_app_scope() -> None:
    namespace = engine_artifact_namespace("run-p1-a1")
    key = f"{namespace}/results.json"
    put(key, b"{}", content_type="application/json")
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(engine_run_row("t-1", "run-p1-a1", "p1", app_id="app-a"))

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


def test_get_artifact_engine_key_uses_persisted_store_affinity() -> None:
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
