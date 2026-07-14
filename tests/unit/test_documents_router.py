"""/documents routes: multipart upload into MemoryArtifactStore, scoping, CRUD."""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.adapters.stubs.artifact_store import MemoryArtifactStore
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import Document
from apex.routers.documents import router
from apex.services.connections import get_connection_resolver
from apex.services.documents import (
    MAX_DOCUMENT_BYTES,
    extract_boundary,
    get_documents_repository,
    parse_multipart,
    safe_filename,
)


class FakeArtifactResolver:
    async def resolve_with_connection_id(
        self,
        _kind: object,
        connection_id: str | None = None,
        project_id: str | None = None,
        **_kwargs: object,
    ) -> tuple[MemoryArtifactStore, str]:
        return MemoryArtifactStore(), connection_id or f"artifacts-{project_id or 'global'}"


class FakeDocumentsRepository:
    """In-memory stand-in matching DocumentsRepository's surface."""

    def __init__(self) -> None:
        self.rows: dict[str, Document] = {}

    async def add(self, document: Document) -> Document:
        if document.created_at is None:
            document.created_at = datetime.now(UTC)
        self.rows[document.id] = document
        return document

    async def get(self, document_id: str) -> Document | None:
        return self.rows.get(document_id)

    async def get_by_artifact_key(self, artifact_key: str) -> Document | None:
        for row in self.rows.values():
            if row.artifact_key == artifact_key:
                return row
        return None

    async def list(
        self,
        *,
        project: str | None = None,
        q: str | None = None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        rows = list(self.rows.values())
        if allowed_scopes is not None:
            rows = [
                row
                for row in rows
                if row.project_id is None
                or any(
                    scope.project_id == row.project_id
                    and (row.app_id is None or scope.app_id is None or scope.app_id == row.app_id)
                    for scope in allowed_scopes
                )
            ]
        if project is not None:
            rows = [r for r in rows if r.project_id == project]
        if q:
            needle = q.lower()
            rows = [
                r for r in rows if needle in r.name.lower() or needle in (r.summary or "").lower()
            ]
        return rows[offset : offset + limit]

    async def delete(self, document: Document) -> None:
        self.rows.pop(document.id, None)


@pytest.fixture(autouse=True)
def clean_artifact_store() -> Iterator[None]:
    MemoryArtifactStore.clear()
    yield
    MemoryArtifactStore.clear()


def identity(role: Role = Role.OPERATOR, scopes: list[ScopeRef] | None = None) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="op",
        consumer_type=ConsumerType.DASHBOARD,
        role=role,
        scopes=scopes or [],
    )


def make_app(repo: FakeDocumentsRepository, who: ConsumerIdentity) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_documents_repository] = lambda: repo
    app.dependency_overrides[get_connection_resolver] = lambda: FakeArtifactResolver()
    app.dependency_overrides[get_current_identity] = lambda: who
    return app


def make_document(
    doc_id: str,
    project_id: str | None,
    name: str = "doc.txt",
    *,
    app_id: str | None = None,
) -> Document:
    return Document(
        id=doc_id,
        name=name,
        media_type="text/plain",
        size_bytes=3,
        artifact_key=f"documents/{doc_id}/{name}",
        project_id=project_id,
        app_id=app_id,
        created_at=datetime.now(UTC),
    )


# ── Multipart helpers (pure) ────────────────────────────────────────────────


def test_extract_boundary_variants() -> None:
    assert extract_boundary('multipart/form-data; boundary="abc123"') == "abc123"
    assert extract_boundary("multipart/form-data; boundary=xyz") == "xyz"
    assert extract_boundary("application/json") is None
    assert extract_boundary(None) is None


def test_parse_multipart_extracts_file_and_fields() -> None:
    boundary = "BOUND"
    body = (
        b"--BOUND\r\n"
        b'Content-Disposition: form-data; name="project_id"\r\n\r\n'
        b"p1\r\n"
        b"--BOUND\r\n"
        b'Content-Disposition: form-data; name="file"; filename="notes.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
        b"hello\r\nworld\r\n"
        b"--BOUND--\r\n"
    )
    parsed = parse_multipart(body, boundary)
    assert parsed.fields == {"project_id": "p1"}
    assert parsed.file is not None
    assert parsed.file.filename == "notes.txt"
    assert parsed.file.content_type == "text/plain"
    assert parsed.file.data == b"hello\r\nworld"


def test_safe_filename_strips_paths() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\evil\\name.bin") == "name.bin"
    assert safe_filename("") == "upload.bin"


# ── Upload ───────────────────────────────────────────────────────────────────


def test_upload_document_stores_bytes_and_metadata() -> None:
    repo = FakeDocumentsRepository()
    app = make_app(repo, identity(scopes=[ScopeRef(project_id="p1")]))
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("spec.md", b"# Spec\nbody", "text/markdown")},
            data={"project_id": "p1", "summary": "API spec"},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "spec.md"
    assert body["media_type"] == "text/markdown"
    assert body["size_bytes"] == len(b"# Spec\nbody")
    assert body["project_id"] == "p1"
    assert body["summary"] == "API spec"
    assert body["uploaded_by"] == "op"
    assert body["artifact_key"] == f"documents/{body['id']}/spec.md"
    # Bytes actually landed in the artifact store under the documented key.
    assert MemoryArtifactStore._objects[body["artifact_key"]][0] == b"# Spec\nbody"
    assert body["id"] in repo.rows
    assert repo.rows[body["id"]].artifact_connection_id == "artifacts-p1"


def test_upload_parses_text_and_populates_fields() -> None:
    repo = FakeDocumentsRepository()
    app = make_app(repo, identity(scopes=[ScopeRef(project_id="p1")]))
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("story.md", b"# Login\n\nUser can sign in with SSO.", "text/markdown")},
            data={"project_id": "p1", "summary": "Story"},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["parse_status"] == "parsed"
    assert body["extracted_chars"] == len("# Login\n\nUser can sign in with SSO.")
    assert body["parse_error"] is None
    assert "User can sign in with SSO." in body["text_preview"]
    # The extracted text is persisted on the row for later context injection.
    assert "User can sign in with SSO." in (repo.rows[body["id"]].extracted_text or "")


def test_upload_auto_derives_summary_when_missing() -> None:
    repo = FakeDocumentsRepository()
    app = make_app(repo, identity(scopes=[ScopeRef(project_id="p1")]))
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            files={
                "file": ("story.md", b"First evidence paragraph.\n\nMore detail.", "text/markdown")
            },
            data={"project_id": "p1"},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["summary"] == "First evidence paragraph."
    assert body["parse_status"] == "parsed"


def test_upload_unsupported_type_marks_unsupported_but_succeeds() -> None:
    repo = FakeDocumentsRepository()
    app = make_app(repo, identity(scopes=[ScopeRef(project_id="p1")]))
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("diagram.png", b"\x89PNG\r\n\x1a\n binary", "image/png")},
            data={"project_id": "p1", "summary": "Diagram"},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["parse_status"] == "unsupported"
    assert body["text_preview"] is None
    assert not body["extracted_chars"]
    # Still stored and attachable as a titled reference.
    assert body["id"] in repo.rows


def test_upload_document_rejects_oversize_with_413() -> None:
    repo = FakeDocumentsRepository()
    app = make_app(repo, identity())
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("big.bin", b"x" * (MAX_DOCUMENT_BYTES + 1), "application/x-big")},
        )
    assert response.status_code == 413
    assert repo.rows == {}


def test_upload_document_requires_multipart() -> None:
    app = make_app(FakeDocumentsRepository(), identity())
    with TestClient(app) as client:
        response = client.post("/v1/documents", json={"nope": True})
    assert response.status_code == 415


def test_upload_document_missing_file_part_is_422() -> None:
    app = make_app(FakeDocumentsRepository(), identity())
    with TestClient(app) as client:
        # (None, value) -> multipart text field without any file part.
        response = client.post("/v1/documents", files={"summary": (None, "just a field")})
    assert response.status_code == 422


def test_upload_document_viewer_is_403() -> None:
    app = make_app(FakeDocumentsRepository(), identity(role=Role.VIEWER))
    with TestClient(app) as client:
        response = client.post("/v1/documents", files={"file": ("a.txt", b"a", "text/plain")})
    assert response.status_code == 403


def test_upload_document_out_of_scope_project_is_403() -> None:
    who = identity(scopes=[ScopeRef(project_id="p1")])
    app = make_app(FakeDocumentsRepository(), who)
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("a.txt", b"a", "text/plain")},
            data={"project_id": "p2"},
        )
    assert response.status_code == 403


def test_upload_document_app_scope_is_inferred_without_widening() -> None:
    repo = FakeDocumentsRepository()
    who = identity(scopes=[ScopeRef(project_id="p1", app_id="app-a")])
    app = make_app(repo, who)
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("a.txt", b"a", "text/plain")},
            data={"project_id": "p1"},
        )

    assert response.status_code == 201
    row = repo.rows[response.json()["id"]]
    assert (row.project_id, row.app_id) == ("p1", "app-a")


def test_upload_document_single_scoped_project_is_inferred_not_global() -> None:
    repo = FakeDocumentsRepository()
    who = identity(scopes=[ScopeRef(project_id="p1")])
    app = make_app(repo, who)
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("a.txt", b"a", "text/plain")},
        )

    assert response.status_code == 201
    row = repo.rows[response.json()["id"]]
    assert (row.project_id, row.app_id) == ("p1", None)


def test_upload_document_requires_explicit_ambiguous_scope() -> None:
    who = identity(
        scopes=[
            ScopeRef(project_id="p1", app_id="app-a"),
            ScopeRef(project_id="p1", app_id="app-b"),
        ]
    )
    app = make_app(FakeDocumentsRepository(), who)
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("a.txt", b"a", "text/plain")},
            data={"project_id": "p1"},
        )
    assert response.status_code == 422

    multi_project = identity(scopes=[ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    with TestClient(make_app(FakeDocumentsRepository(), multi_project)) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("a.txt", b"a", "text/plain")},
        )
    assert response.status_code == 422


# ── List / get / delete + scoping ───────────────────────────────────────────


def seeded_repo() -> FakeDocumentsRepository:
    repo = FakeDocumentsRepository()
    repo.rows["d1"] = make_document("d1", "p1", "alpha.txt")
    repo.rows["d2"] = make_document("d2", "p2", "beta.txt")
    repo.rows["d3"] = make_document("d3", None, "global.txt")
    return repo


def test_list_documents_scoped_consumer_sees_own_and_global() -> None:
    who = identity(scopes=[ScopeRef(project_id="p1")])
    app = make_app(seeded_repo(), who)
    with TestClient(app) as client:
        response = client.get("/v1/documents")
    ids = [item["id"] for item in response.json()["items"]]
    assert sorted(ids) == ["d1", "d3"]


def test_list_documents_app_scope_excludes_sibling_app_preview() -> None:
    repo = seeded_repo()
    repo.rows["app-a"] = make_document("app-a", "p1", "a.txt", app_id="a1")
    repo.rows["app-b"] = make_document("app-b", "p1", "b.txt", app_id="a2")
    who = identity(scopes=[ScopeRef(project_id="p1", app_id="a1")])

    with TestClient(make_app(repo, who)) as client:
        response = client.get("/v1/documents")

    assert response.status_code == 200
    assert {item["id"] for item in response.json()["items"]} == {"d1", "d3", "app-a"}


def test_list_documents_unscoped_admin_sees_all() -> None:
    app = make_app(seeded_repo(), identity(role=Role.ADMIN))
    with TestClient(app) as client:
        response = client.get("/v1/documents")
    assert len(response.json()["items"]) == 3


def test_list_documents_q_filter() -> None:
    app = make_app(seeded_repo(), identity(role=Role.ADMIN))
    with TestClient(app) as client:
        response = client.get("/v1/documents", params={"q": "beta"})
    assert [item["id"] for item in response.json()["items"]] == ["d2"]


def test_get_document_found_and_scoped_404() -> None:
    who = identity(scopes=[ScopeRef(project_id="p1")])
    app = make_app(seeded_repo(), who)
    with TestClient(app) as client:
        assert client.get("/v1/documents/d1").status_code == 200
        assert client.get("/v1/documents/d2").status_code == 404  # out of scope
        assert client.get("/v1/documents/missing").status_code == 404


def test_delete_document_removes_row_only() -> None:
    repo = seeded_repo()
    app = make_app(repo, identity(role=Role.ADMIN))
    with TestClient(app) as client:
        response = client.delete("/v1/documents/d1")
    assert response.status_code == 204
    assert "d1" not in repo.rows


def test_delete_document_requires_write_scope_not_read_visibility() -> None:
    repo = seeded_repo()
    repo.rows["app-a"] = make_document("app-a", "p1", "a.txt", app_id="a1")
    app_only = identity(scopes=[ScopeRef(project_id="p1", app_id="a1")])
    with TestClient(make_app(repo, app_only)) as client:
        assert client.delete("/v1/documents/app-a").status_code == 204
        assert client.delete("/v1/documents/d1").status_code == 404
        assert client.delete("/v1/documents/d3").status_code == 404

    project_operator = identity(scopes=[ScopeRef(project_id="p1")])
    with TestClient(make_app(repo, project_operator)) as client:
        assert client.delete("/v1/documents/d1").status_code == 204


def test_delete_global_document_requires_unscoped_admin() -> None:
    repo = seeded_repo()
    with TestClient(make_app(repo, identity(role=Role.OPERATOR))) as client:
        assert client.delete("/v1/documents/d3").status_code == 404
    with TestClient(make_app(repo, identity(role=Role.ADMIN))) as client:
        assert client.delete("/v1/documents/d3").status_code == 204


def test_delete_document_viewer_is_403() -> None:
    app = make_app(seeded_repo(), identity(role=Role.VIEWER))
    with TestClient(app) as client:
        assert client.delete("/v1/documents/d1").status_code == 403
