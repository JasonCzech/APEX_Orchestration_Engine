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
from apex.persistence.models import Document
from apex.routers.artifacts import router
from apex.services.documents import get_artifact_store, get_documents_repository


class FakeDocumentsRepository:
    def __init__(self) -> None:
        self.rows: dict[str, Document] = {}

    async def get_by_artifact_key(self, artifact_key: str) -> Document | None:
        for row in self.rows.values():
            if row.artifact_key == artifact_key:
                return row
        return None


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


def make_app(repo: FakeDocumentsRepository, who: ConsumerIdentity | None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_documents_repository] = lambda: repo
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


def test_get_artifact_transcript_key_is_text_plain() -> None:
    put("transcripts/t-1/execution.txt", b"phase log")
    app = make_app(FakeDocumentsRepository(), identity())
    with TestClient(app) as client:
        response = client.get("/v1/artifacts/transcripts/t-1/execution.txt")
    assert response.status_code == 200
    assert response.content == b"phase log"
    assert response.headers["content-type"].startswith("text/plain")


def test_get_artifact_unknown_key_falls_back_to_octet_stream() -> None:
    put("blobs/raw.bin", b"\x00\x01\x02")
    app = make_app(FakeDocumentsRepository(), identity())
    with TestClient(app) as client:
        response = client.get("/v1/artifacts/blobs/raw.bin")
    assert response.content == b"\x00\x01\x02"
    assert response.headers["content-type"].startswith("application/octet-stream")


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
