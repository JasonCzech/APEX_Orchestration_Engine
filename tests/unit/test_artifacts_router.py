"""/artifacts proxy: streams bytes with the right content type; 404 when missing."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.adapters.stubs.artifact_store import MemoryArtifactStore
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import Document, EngineRun
from apex.routers import artifacts as artifacts_router
from apex.routers.artifacts import get_engine_runs_repository, router
from apex.services.documents import get_artifact_store, get_documents_repository


class FakeDocumentsRepository:
    def __init__(self) -> None:
        self.rows: dict[str, Document] = {}

    async def get_by_artifact_key(self, artifact_key: str) -> Document | None:
        for row in self.rows.values():
            if row.artifact_key == artifact_key:
                return row
        return None


class FakeEngineRunsRepository:
    def __init__(self) -> None:
        self.rows: list[EngineRun] = []

    async def get_by_external_run_id(
        self,
        external_run_id: str,
        *,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None:
        for row in self.rows:
            if row.external_run_id != external_run_id:
                continue
            if allowed_project_ids is not None and row.project_id not in allowed_project_ids:
                continue
            return row
        return None


class FakeThreads:
    async def get(self, thread_id: str) -> dict[str, str]:
        return {"thread_id": thread_id}


class FakeLoopbackClient:
    threads = FakeThreads()


@pytest.fixture(autouse=True)
def clean_artifact_store() -> Iterator[None]:
    MemoryArtifactStore.clear()
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
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_documents_repository] = lambda: repo
    app.dependency_overrides[get_engine_runs_repository] = lambda: (
        engine_repo or FakeEngineRunsRepository()
    )
    app.dependency_overrides[get_artifact_store] = lambda: MemoryArtifactStore()
    if who is not None:
        app.dependency_overrides[get_current_identity] = lambda: who
    return app


def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    asyncio.run(MemoryArtifactStore().put(key, data, content_type=content_type))


def document_row(doc_id: str, key: str, media_type: str, project_id: str | None) -> Document:
    return Document(
        id=doc_id,
        name=key.rsplit("/", 1)[-1],
        media_type=media_type,
        size_bytes=1,
        artifact_key=key,
        project_id=project_id,
        created_at=datetime.now(UTC),
    )


def engine_run_row(thread_id: str, external_run_id: str, project_id: str | None) -> EngineRun:
    return EngineRun(
        id=f"{thread_id}-1",
        thread_id=thread_id,
        project_id=project_id,
        attempt=1,
        engine="sim",
        external_run_id=external_run_id,
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


def test_get_artifact_transcript_key_is_text_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifacts_router, "loopback_client", lambda _api_key: FakeLoopbackClient())
    put("transcripts/t-1/execution.txt", b"phase log")
    app = make_app(FakeDocumentsRepository(), identity())
    with TestClient(app) as client:
        response = client.get("/v1/artifacts/transcripts/t-1/execution.txt")
    assert response.status_code == 200
    assert response.content == b"phase log"
    assert response.headers["content-type"].startswith("text/plain")


def test_get_artifact_unknown_key_is_404() -> None:
    put("blobs/raw.bin", b"\x00\x01\x02")
    app = make_app(FakeDocumentsRepository(), identity())
    with TestClient(app) as client:
        response = client.get("/v1/artifacts/blobs/raw.bin")
    assert response.status_code == 404


def test_get_artifact_engine_key_requires_project_scope() -> None:
    key = "engine-runs/run-p1/results.json"
    put(key, b"{}", content_type="application/json")
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(engine_run_row("t-1", "run-p1", "p1"))
    app = make_app(
        FakeDocumentsRepository(),
        identity(scopes=[ScopeRef(project_id="p1")]),
        engine_repo,
    )
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 200
    assert response.content == b"{}"


def test_get_artifact_engine_key_out_of_scope_is_404() -> None:
    key = "engine-runs/run-p2/results.json"
    put(key, b"{}", content_type="application/json")
    engine_repo = FakeEngineRunsRepository()
    engine_repo.rows.append(engine_run_row("t-2", "run-p2", "p2"))
    app = make_app(
        FakeDocumentsRepository(),
        identity(scopes=[ScopeRef(project_id="p1")]),
        engine_repo,
    )
    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}")
    assert response.status_code == 404


def test_get_artifact_missing_is_404_problem() -> None:
    app = make_app(FakeDocumentsRepository(), identity())
    with TestClient(app) as client:
        response = client.get("/v1/artifacts/documents/nope/file.txt")
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
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", "k")  # auth on; no key sent -> 401
    put("blobs/raw.bin", b"x")
    app = make_app(FakeDocumentsRepository(), who=None)
    with TestClient(app) as client:
        response = client.get("/v1/artifacts/blobs/raw.bin")
    assert response.status_code == 401
