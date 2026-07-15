"""/documents routes: multipart upload into MemoryArtifactStore, scoping, CRUD."""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apex.adapters.stubs.artifact_store import MemoryArtifactStore
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import Document
from apex.routers.documents import (
    get_catalog_repository,
    get_document_repository_factory,
    router,
)
from apex.services.connections import get_connection_resolver
from apex.services.documents import (
    MAX_DOCUMENT_BYTES,
    DocumentsService,
    DocumentTooLargeError,
    DocumentUploadBusyError,
    MultipartParseError,
    acquire_document_upload_slot,
    await_task_definitively,
    document_upload_admission,
    extract_boundary,
    get_documents_repository,
    parse_multipart,
    purge_document_tombstone,
    purge_stale_upload_intent,
    read_body_capped,
    reconcile_pending_document_deletions_once,
    safe_filename,
    uploaded_document_context_packets,
)


class FakeArtifactResolver:
    def __init__(self, store: Any | None = None) -> None:
        self.store = store or MemoryArtifactStore()
        self.calls: list[str] = []

    async def resolve_with_connection_id(
        self,
        _kind: object,
        connection_id: str | None = None,
        project_id: str | None = None,
        **_kwargs: object,
    ) -> tuple[Any, str]:
        self.calls.append(connection_id or f"artifacts-{project_id or 'global'}")
        return self.store, connection_id or f"artifacts-{project_id or 'global'}"


class FakeDocumentsRepository:
    """In-memory stand-in matching DocumentsRepository's surface."""

    def __init__(self) -> None:
        self.rows: dict[str, Document] = {}
        self.list_calls = 0
        self.fail_mark_deletion = False
        self.fail_complete_deletion = False

    async def add(self, document: Document) -> Document:
        if document.created_at is None:
            document.created_at = datetime.now(UTC)
        self.rows[document.id] = document
        return document

    async def stage_upload(self, document: Document) -> Document:
        if document.created_at is None:
            document.created_at = datetime.now(UTC)
        document.upload_pending_at = datetime.now(UTC)
        self.rows[document.id] = document
        return document

    async def finalize_upload(self, document: Document) -> Document:
        row = self.rows.get(document.id)
        if row is None or row.upload_pending_at is None or row.deletion_pending_at is not None:
            raise RuntimeError("upload is no longer pending")
        row.upload_pending_at = None
        return row

    async def claim_stale_upload(self, document: Document) -> Document | None:
        row = self.rows.get(document.id)
        if (
            row is None
            or document.upload_pending_at is None
            or row.upload_pending_at != document.upload_pending_at
            or row.deletion_pending_at is not None
        ):
            return None
        row.deletion_pending_at = datetime.now(UTC)
        return row

    async def resolve_finalized_upload(self, document_id: str) -> Document | None:
        row = self.rows.get(document_id)
        return row if row is not None and row.upload_pending_at is None else None

    async def mark_upload_deletion_pending(self, document_id: str) -> Document | None:
        row = self.rows.get(document_id)
        if row is None or row.upload_pending_at is None or row.deletion_pending_at is not None:
            return None
        if self.fail_mark_deletion:
            raise RuntimeError("tombstone commit failed")
        row.deletion_pending_at = datetime.now(UTC)
        row.cleanup_retry_at = None
        row.cleanup_attempt_count = 0
        row.cleanup_last_error = None
        return row

    async def get(self, document_id: str) -> Document | None:
        row = self.rows.get(document_id)
        return (
            row
            if row is not None and row.deletion_pending_at is None and row.upload_pending_at is None
            else None
        )

    async def get_any(self, document_id: str) -> Document | None:
        return self.rows.get(document_id)

    async def get_any_for_update(self, document_id: str) -> Document | None:
        return self.rows.get(document_id)

    async def assign_artifact_connection(
        self,
        document: Document,
        connection_id: str,
    ) -> Document:
        if document.artifact_connection_id not in {None, connection_id}:
            raise ValueError("document artifact-store affinity is already fixed")
        document.artifact_connection_id = connection_id
        document.cleanup_retry_at = None
        document.cleanup_attempt_count = 0
        document.cleanup_last_error = None
        return document

    async def get_by_artifact_key(self, artifact_key: str) -> Document | None:
        for row in self.rows.values():
            if (
                row.artifact_key == artifact_key
                and row.deletion_pending_at is None
                and row.upload_pending_at is None
            ):
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
        self.list_calls += 1
        rows = [
            row
            for row in self.rows.values()
            if row.deletion_pending_at is None and row.upload_pending_at is None
        ]
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

    async def mark_deletion_pending(self, document: Document) -> None:
        if self.fail_mark_deletion:
            raise RuntimeError("tombstone commit failed")
        document.deletion_pending_at = datetime.now(UTC)

    async def complete_deletion(self, document_id: str) -> None:
        if self.fail_complete_deletion:
            raise RuntimeError("metadata finalize failed")
        self.rows.pop(document_id, None)


class FakeCatalogRepository:
    def __init__(self, *, archived: bool = False) -> None:
        self.archived = archived

    async def get_application(self, app_id: str) -> object:
        return SimpleNamespace(
            id=app_id,
            project_id="p1",
            archived_at=datetime.now(UTC) if self.archived else None,
        )


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


def make_app(
    repo: FakeDocumentsRepository,
    who: ConsumerIdentity,
    *,
    catalog: FakeCatalogRepository | None = None,
    resolver: FakeArtifactResolver | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_documents_repository] = lambda: repo

    @asynccontextmanager
    async def open_document_repository() -> AsyncIterator[FakeDocumentsRepository]:
        yield repo

    app.dependency_overrides[get_document_repository_factory] = lambda: open_document_repository
    app.dependency_overrides[get_connection_resolver] = lambda: resolver or FakeArtifactResolver()
    app.dependency_overrides[get_catalog_repository] = lambda: catalog or FakeCatalogRepository()
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
    assert extract_boundary("text/plain; x=multipart/form-data; boundary=xyz") is None
    assert extract_boundary("multipart/form-data; xboundary=xyz") is None
    assert extract_boundary(None) is None
    assert extract_boundary(f"multipart/form-data; boundary={'x' * 71}") is None


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


def test_parse_multipart_preserves_boundary_like_binary_payload_bytes() -> None:
    boundary = "BOUND"
    payload = b"\x00before--BOUND--middle\r\n--BOUNDx\xffafter"
    body = (
        b"--BOUND\r\n"
        b'Content-Disposition: form-data; name="file"; filename="blob.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n" + payload + b"\r\n--BOUND--\r\n"
    )

    parsed = parse_multipart(body, boundary)

    assert parsed.file is not None
    assert parsed.file.data == payload


def test_parse_multipart_rejects_excess_parts_and_oversized_headers() -> None:
    parts = [
        b'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\na",
        b'Content-Disposition: form-data; name="project_id"\r\n\r\np1',
        b'Content-Disposition: form-data; name="app_id"\r\n\r\na1',
        b'Content-Disposition: form-data; name="summary"\r\n\r\nsummary',
        b'Content-Disposition: form-data; name="extra"\r\n\r\nvalue',
    ]
    body = b"--BOUND\r\n" + b"\r\n--BOUND\r\n".join(parts) + b"\r\n--BOUND--\r\n"

    with pytest.raises(MultipartParseError, match="more than 4 parts"):
        parse_multipart(body, "BOUND")

    huge_header = (
        b"--BOUND\r\nX-Fill: "
        + b"x" * 9000
        + b'\r\nContent-Disposition: form-data; name="file"; filename="a.txt"'
        + b"\r\n\r\na\r\n--BOUND--\r\n"
    )
    with pytest.raises(MultipartParseError, match="headers are too large"):
        parse_multipart(huge_header, "BOUND")


@pytest.mark.asyncio
async def test_document_upload_admission_caps_retained_requests_before_work() -> None:
    entered = 0
    active = 0
    peak = 0
    four_entered = asyncio.Event()
    release = asyncio.Event()

    async def worker() -> None:
        nonlocal entered, active, peak
        async with document_upload_admission():
            entered += 1
            active += 1
            peak = max(peak, active)
            if entered == 4:
                four_entered.set()
            await release.wait()
            active -= 1

    admitted = [asyncio.create_task(worker()) for _ in range(4)]
    await asyncio.wait_for(four_entered.wait(), timeout=1)

    rejected = [asyncio.create_task(worker()) for _ in range(16)]
    rejected_results = await asyncio.wait_for(
        asyncio.gather(*rejected, return_exceptions=True),
        timeout=1,
    )

    assert entered == 4
    assert peak == 4
    assert all(isinstance(result, DocumentUploadBusyError) for result in rejected_results)

    release.set()
    await asyncio.gather(*admitted)
    await worker()
    assert entered == 5


@pytest.mark.asyncio
async def test_document_upload_dependency_maps_saturation_to_retryable_503() -> None:
    admissions = [document_upload_admission() for _ in range(4)]
    for admission in admissions:
        await admission.__aenter__()
    try:
        dependency = acquire_document_upload_slot()
        with pytest.raises(HTTPException) as excinfo:
            await anext(dependency)
    finally:
        for admission in reversed(admissions):
            await admission.__aexit__(None, None, None)

    assert excinfo.value.status_code == 503
    assert excinfo.value.headers == {"Retry-After": "1"}


@pytest.mark.asyncio
async def test_cancelled_multipart_workers_keep_upload_permits_until_threads_finish() -> None:
    lock = threading.Lock()
    release = threading.Event()
    four_started = threading.Event()
    active = 0
    entered = 0
    peak = 0

    def blocking_parse() -> None:
        nonlocal active, entered, peak
        with lock:
            active += 1
            entered += 1
            peak = max(peak, active)
            if entered == 4:
                four_started.set()
        release.wait()
        with lock:
            active -= 1

    async def parse_request() -> None:
        async with document_upload_admission():
            worker = asyncio.create_task(asyncio.to_thread(blocking_parse))
            await await_task_definitively(worker)

    first = [asyncio.create_task(parse_request()) for _ in range(4)]
    assert await asyncio.to_thread(four_started.wait, 1)
    for task in first:
        task.cancel()
        task.cancel()  # repeated disconnect/cancellation must not detach the thread

    rejected = [asyncio.create_task(parse_request()) for _ in range(4)]
    rejected_results = await asyncio.wait_for(
        asyncio.gather(*rejected, return_exceptions=True),
        timeout=1,
    )
    with lock:
        assert entered == 4
        assert active == 4
        assert peak == 4
    assert all(isinstance(result, DocumentUploadBusyError) for result in rejected_results)

    release.set()
    results = await asyncio.gather(*first, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)

    admitted_after_release = [asyncio.create_task(parse_request()) for _ in range(4)]
    assert await asyncio.gather(*admitted_after_release) == [None] * 4
    assert peak == 4


@pytest.mark.asyncio
async def test_document_body_read_has_one_non_resetting_deadline() -> None:
    async def drip_then_stall() -> AsyncIterator[bytes]:
        yield b"x"
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await read_body_capped(drip_then_stall(), 1024, timeout_s=0.01)


@pytest.mark.asyncio
async def test_document_body_cap_rejects_one_oversized_frame_before_copying() -> None:
    async def one_frame() -> AsyncIterator[bytes]:
        yield b"x" * 17

    with pytest.raises(DocumentTooLargeError, match="upload exceeds"):
        await read_body_capped(one_frame(), 16)


def test_safe_filename_strips_paths() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\evil\\name.bin") == "name.bin"
    assert safe_filename("") == "upload.bin"


def test_safe_filename_rejects_controls_and_overlong_names() -> None:
    with pytest.raises(ValueError, match="control characters"):
        safe_filename("bad\x00name.txt")
    with pytest.raises(ValueError, match="too long"):
        safe_filename("x" * 256)


@pytest.mark.asyncio
async def test_upload_cleans_object_when_extraction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_extraction(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("extractor crashed")

    monkeypatch.setattr("apex.services.documents._extract_text_bounded", fail_extraction)
    service = DocumentsService(FakeDocumentsRepository(), MemoryArtifactStore())

    with pytest.raises(RuntimeError, match="extractor crashed"):
        await service.upload(
            filename="broken.txt",
            content_type="text/plain",
            data=b"payload",
            artifact_connection_id="artifacts-p1",
            project_id="p1",
            app_id=None,
            summary=None,
            uploaded_by="c1",
        )

    assert MemoryArtifactStore._objects == {}


@pytest.mark.asyncio
async def test_upload_cleans_object_after_lost_put_acknowledgement() -> None:
    class CommitThenFailStore(MemoryArtifactStore):
        async def put(self, *args: Any, **kwargs: Any) -> Any:
            await super().put(*args, **kwargs)
            raise ConnectionError("PUT acknowledgement lost")

    with pytest.raises(ConnectionError, match="acknowledgement lost"):
        await DocumentsService(FakeDocumentsRepository(), CommitThenFailStore()).upload(
            filename="ambiguous.txt",
            content_type="text/plain",
            data=b"payload",
            artifact_connection_id="artifacts-p1",
            project_id="p1",
            app_id=None,
            summary=None,
            uploaded_by="c1",
        )

    assert MemoryArtifactStore._objects == {}


@pytest.mark.asyncio
async def test_ambiguous_finalize_preserves_bytes_when_commit_may_have_succeeded() -> None:
    class AmbiguousFinalizeRepository(FakeDocumentsRepository):
        async def finalize_upload(self, document: Document) -> Document:
            await super().finalize_upload(document)
            raise ConnectionError("finalize acknowledgement lost")

        async def resolve_finalized_upload(self, document_id: str) -> Document | None:
            raise ConnectionError("finalize resolution unavailable")

    repository = AmbiguousFinalizeRepository()
    document_store = MemoryArtifactStore()

    with pytest.raises(ConnectionError, match="finalize acknowledgement lost"):
        await DocumentsService(repository, document_store).upload(
            filename="ambiguous-finalize.txt",
            content_type="text/plain",
            data=b"payload",
            artifact_connection_id="artifacts-p1",
            project_id="p1",
            app_id=None,
            summary=None,
            uploaded_by="c1",
        )

    (document,) = repository.rows.values()
    assert document.upload_pending_at is None
    assert document.deletion_pending_at is None
    assert await document_store.get(document.artifact_key) == b"payload"


@pytest.mark.asyncio
async def test_failed_finalize_uses_id_tombstone_after_resolution_miss() -> None:
    class ResolutionMissRepository(FakeDocumentsRepository):
        def __init__(self) -> None:
            super().__init__()
            self.tombstoned_ids: list[str] = []

        async def finalize_upload(self, document: Document) -> Document:
            del document
            raise ConnectionError("finalize failed")

        async def resolve_finalized_upload(self, document_id: str) -> Document | None:
            del document_id
            return None

        async def mark_deletion_pending(self, document: Document) -> None:
            del document
            raise AssertionError("rollback-expired ORM object must not be used")

        async def mark_upload_deletion_pending(self, document_id: str) -> Document | None:
            self.tombstoned_ids.append(document_id)
            return await super().mark_upload_deletion_pending(document_id)

    repository = ResolutionMissRepository()
    store = MemoryArtifactStore()

    with pytest.raises(ConnectionError, match="finalize failed"):
        await DocumentsService(repository, store).upload(
            filename="resolution-miss.txt",
            content_type="text/plain",
            data=b"payload",
            artifact_connection_id="artifacts-p1",
            project_id="p1",
            app_id=None,
            summary=None,
            uploaded_by="c1",
        )

    assert len(repository.tombstoned_ids) == 1
    assert repository.rows == {}
    assert MemoryArtifactStore._objects == {}


@pytest.mark.asyncio
async def test_failed_finalize_preserves_bytes_when_id_tombstone_is_uncertain() -> None:
    class UncertainTombstoneRepository(FakeDocumentsRepository):
        async def finalize_upload(self, document: Document) -> Document:
            del document
            raise ConnectionError("finalize failed")

        async def resolve_finalized_upload(self, document_id: str) -> Document | None:
            del document_id
            return None

        async def mark_upload_deletion_pending(self, document_id: str) -> Document | None:
            del document_id
            return None

    repository = UncertainTombstoneRepository()
    store = MemoryArtifactStore()

    with pytest.raises(ConnectionError, match="finalize failed"):
        await DocumentsService(repository, store).upload(
            filename="uncertain-tombstone.txt",
            content_type="text/plain",
            data=b"payload",
            artifact_connection_id="artifacts-p1",
            project_id="p1",
            app_id=None,
            summary=None,
            uploaded_by="c1",
        )

    (document,) = repository.rows.values()
    assert document.deletion_pending_at is None
    assert await store.get(document.artifact_key) == b"payload"


@pytest.mark.asyncio
async def test_upload_cancellation_waits_for_late_put_before_cleanup() -> None:
    class LateStore:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.objects: dict[str, bytes] = {}

        async def put(self, key: str, data: bytes, **_kwargs: Any) -> None:
            self.started.set()
            await self.release.wait()
            self.objects[key] = data

        async def delete(self, key: str) -> None:
            self.objects.pop(key, None)

    store = LateStore()
    task = asyncio.create_task(
        DocumentsService(FakeDocumentsRepository(), cast(Any, store)).upload(
            filename="cancelled.txt",
            content_type="text/plain",
            data=b"payload",
            artifact_connection_id="artifacts-p1",
            project_id="p1",
            app_id=None,
            summary=None,
            uploaded_by="c1",
        )
    )
    await store.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    store.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.objects == {}


@pytest.mark.asyncio
async def test_cancelled_extractions_hold_worker_permits_until_threads_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    release = threading.Event()
    four_started = threading.Event()
    active = 0
    entered = 0
    peak = 0

    def blocking_extract(*_args: object, **_kwargs: object) -> Any:
        nonlocal active, entered, peak
        with lock:
            active += 1
            entered += 1
            peak = max(peak, active)
            if entered == 4:
                four_started.set()
        release.wait()
        with lock:
            active -= 1
        return SimpleNamespace(status="parsed", text="ok", char_count=2, error=None)

    monkeypatch.setattr("apex.services.documents._extract_text_bounded", blocking_extract)
    repository = FakeDocumentsRepository()
    service = DocumentsService(repository, MemoryArtifactStore())

    async def upload(index: int) -> Document:
        return await service.upload(
            filename=f"worker-{index}.txt",
            content_type="text/plain",
            data=b"payload",
            artifact_connection_id="artifacts-p1",
            project_id="p1",
            app_id=None,
            summary=None,
            uploaded_by="c1",
        )

    first = [asyncio.create_task(upload(index)) for index in range(4)]
    assert await asyncio.to_thread(four_started.wait, 1)
    for task in first:
        task.cancel()
        task.cancel()

    second = [asyncio.create_task(upload(index)) for index in range(4, 8)]
    await asyncio.sleep(0.05)
    with lock:
        assert entered == 4
        assert active == 4
        assert peak == 4

    release.set()
    results = await asyncio.gather(*first, *second, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results[:4])
    assert all(isinstance(result, Document) for result in results[4:])
    assert peak == 4


@pytest.mark.asyncio
async def test_upload_runs_and_stops_durable_intent_lease_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fake_heartbeat(_document_id: str, stop: asyncio.Event) -> None:
        started.set()
        try:
            await stop.wait()
        finally:
            stopped.set()

    class WaitForHeartbeatStore(MemoryArtifactStore):
        async def put(self, *args: Any, **kwargs: Any) -> Any:
            await started.wait()
            return await super().put(*args, **kwargs)

    repository = FakeDocumentsRepository()
    monkeypatch.setattr("apex.services.documents.DocumentsRepository", FakeDocumentsRepository)
    monkeypatch.setattr(
        "apex.services.documents._renew_document_upload_lease",
        fake_heartbeat,
    )

    document = await DocumentsService(repository, WaitForHeartbeatStore()).upload(
        filename="leased.txt",
        content_type="text/plain",
        data=b"payload",
        artifact_connection_id="artifacts-p1",
        project_id="p1",
        app_id=None,
        summary=None,
        uploaded_by="c1",
    )

    assert document.upload_pending_at is None
    assert started.is_set()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_stale_precommitted_upload_intent_is_reconciled_after_interruption() -> None:
    repo = FakeDocumentsRepository()
    document = make_document("interrupted", "p1", "interrupted.txt")
    await repo.stage_upload(document)
    store = MemoryArtifactStore()
    await store.put(document.artifact_key, b"orphan", content_type="text/plain")

    await purge_stale_upload_intent(
        document,
        repo,  # type: ignore[arg-type]
        FakeArtifactResolver(store),
    )

    assert document.id not in repo.rows
    with pytest.raises(KeyError):
        await store.get(document.artifact_key)


@pytest.mark.asyncio
async def test_stale_reconciler_does_not_delete_concurrently_finalized_upload() -> None:
    repo = FakeDocumentsRepository()
    document = make_document("finalized", "p1", "finalized.txt")
    await repo.stage_upload(document)
    store = MemoryArtifactStore()
    await store.put(document.artifact_key, b"live", content_type="text/plain")
    await repo.finalize_upload(document)

    await purge_stale_upload_intent(
        document,
        repo,  # type: ignore[arg-type]
        FakeArtifactResolver(store),
    )

    assert repo.rows[document.id].deletion_pending_at is None
    assert await store.get(document.artifact_key) == b"live"


@pytest.mark.asyncio
async def test_reconciler_defers_failed_cleanup_without_starving_later_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = make_document("cleanup-retry", "p1")
    document.deletion_pending_at = datetime.now(UTC)
    document.artifact_connection_id = "a" * 32
    deferred: list[tuple[str, str]] = []

    class FailingStore:
        async def delete(self, _key: str) -> None:
            raise RuntimeError("provider unavailable")

    class RetryRepository:
        async def list_pending_deletions(self, *, limit: int = 100) -> list[Document]:
            return [document]

        async def list_stale_pending_uploads(
            self, *, before: datetime, limit: int = 100
        ) -> list[Document]:
            return []

        async def get_pending_deletion(self, document_id: str) -> Document | None:
            return document if document_id == document.id else None

        async def defer_cleanup(self, document_id: str, *, error: str) -> bool:
            deferred.append((document_id, error))
            return True

    class FakeSession:
        async def rollback(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self) -> FakeSession:
            return FakeSession()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class SessionMaker:
        def __call__(self) -> SessionContext:
            return SessionContext()

    repository = RetryRepository()
    resolver = FakeArtifactResolver(FailingStore())
    monkeypatch.setattr("apex.services.documents.DocumentsRepository", lambda _session: repository)
    monkeypatch.setattr("apex.services.documents.get_sessionmaker", lambda: SessionMaker())
    monkeypatch.setattr("apex.services.documents.get_connection_resolver", lambda: resolver)

    await reconcile_pending_document_deletions_once()

    assert deferred == [(document.id, "RuntimeError")]


@pytest.mark.asyncio
async def test_locked_cleanup_does_not_guess_legacy_document_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeDocumentsRepository()
    document = make_document("legacy-affinity", "p1")
    await repository.mark_deletion_pending(document)
    resolver = FakeArtifactResolver()
    monkeypatch.setattr(
        "apex.services.documents.get_settings",
        lambda: SimpleNamespace(is_locked_down=True),
    )

    with pytest.raises(RuntimeError, match="affinity must be mapped"):
        await purge_document_tombstone(
            document,
            repository,  # type: ignore[arg-type]
            resolver,
        )

    assert resolver.calls == []
    assert document.deletion_pending_at is not None


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


def test_upload_document_rejects_invalid_filename_before_store_io() -> None:
    repo = FakeDocumentsRepository()
    resolver = FakeArtifactResolver()

    with TestClient(make_app(repo, identity(), resolver=resolver)) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("x" * 256, b"payload", "text/plain")},
        )

    assert response.status_code == 422
    assert resolver.calls == []
    assert MemoryArtifactStore._objects == {}
    assert repo.rows == {}


@pytest.mark.parametrize(
    ("files", "data"),
    [
        ({"file": ("a.txt", b"a", "text/plain")}, {"project_id": "p" * 256}),
        ({"file": ("a.txt", b"a", "text/plain")}, {"summary": "s" * 4_001}),
        ({"file": ("a.txt", b"a", "x" * 256)}, {}),
    ],
)
def test_upload_rejects_oversized_metadata_before_resolver_or_store_io(
    files: dict[str, tuple[str, bytes, str]],
    data: dict[str, str],
) -> None:
    repo = FakeDocumentsRepository()
    resolver = FakeArtifactResolver()

    with TestClient(make_app(repo, identity(), resolver=resolver)) as client:
        response = client.post("/v1/documents", files=files, data=data)

    assert response.status_code == 422
    assert resolver.calls == []
    assert repo.rows == {}
    assert MemoryArtifactStore._objects == {}


def test_upload_rejects_nul_summary_before_resolver_or_persistence() -> None:
    repo = FakeDocumentsRepository()
    resolver = FakeArtifactResolver()

    with TestClient(make_app(repo, identity(), resolver=resolver)) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("a.txt", b"a", "text/plain")},
            data={"summary": "unsafe\x00summary"},
        )

    assert response.status_code == 422
    assert resolver.calls == []
    assert repo.rows == {}
    assert MemoryArtifactStore._objects == {}


@pytest.mark.asyncio
async def test_upload_sanitizes_nul_in_all_persisted_document_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nul_extraction(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            status="parsed",
            text="extracted\x00text",
            char_count=14,
            error="parse\x00diagnostic",
        )

    monkeypatch.setattr("apex.services.documents._extract_text_bounded", nul_extraction)
    repository = FakeDocumentsRepository()

    document = await DocumentsService(repository, MemoryArtifactStore()).upload(
        filename="nul.txt",
        content_type="text/plain",
        data=b"payload",
        artifact_connection_id="artifacts-p1",
        project_id="p1",
        app_id=None,
        summary="given\x00summary",
        uploaded_by="c1",
    )

    assert document.summary == "given\ufffdsummary"
    assert document.extracted_text == "extracted\ufffdtext"
    assert document.parse_error == "parse\ufffddiagnostic"
    assert "\x00" not in "".join(
        (document.summary or "", document.extracted_text or "", document.parse_error or "")
    )


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


@pytest.mark.asyncio
async def test_legacy_document_context_is_clamped_to_packet_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apex.domain.pipeline import MAX_CONTEXT_SUMMARY_CHARS, MAX_CONTEXT_TEXT_CHARS

    repository = FakeDocumentsRepository()
    document = make_document("legacy-long", "p1")
    document.summary = "s" * (MAX_CONTEXT_SUMMARY_CHARS + 1)
    document.extracted_text = "t" * (MAX_CONTEXT_TEXT_CHARS + 1)
    repository.rows[document.id] = document
    monkeypatch.setattr(
        "apex.services.documents.get_settings",
        lambda: SimpleNamespace(documents=SimpleNamespace(max_context_chars_per_doc=500_000)),
    )

    packets = await uploaded_document_context_packets(
        repository,  # type: ignore[arg-type]
        identity(scopes=[ScopeRef(project_id="p1")]),
        [document.id],
    )

    assert len(packets[0]["summary"]) <= MAX_CONTEXT_SUMMARY_CHARS
    assert len(packets[0]["text"]) <= MAX_CONTEXT_TEXT_CHARS
    assert packets[0]["summary"].endswith("…[truncated]")
    assert packets[0]["text"].endswith("…[truncated]")


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


def test_upload_document_maps_body_deadline_to_408(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def time_out(*_args: object, **_kwargs: object) -> bytes:
        raise TimeoutError

    monkeypatch.setattr("apex.routers.documents.read_body_capped", time_out)
    repo = FakeDocumentsRepository()
    resolver = FakeArtifactResolver()

    with TestClient(make_app(repo, identity(), resolver=resolver)) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("a.txt", b"a", "text/plain")},
        )

    assert response.status_code == 408
    assert resolver.calls == []
    assert repo.rows == {}


def test_admin_can_map_legacy_document_affinity_once() -> None:
    repo = FakeDocumentsRepository()
    document = make_document("legacy", "p1")
    document.cleanup_attempt_count = 7
    document.cleanup_last_error = "previous lookup failed"
    repo.rows[document.id] = document
    asyncio.run(
        MemoryArtifactStore().put(
            document.artifact_key,
            b"legacy bytes",
            content_type="text/plain",
        )
    )
    app = make_app(
        repo,
        identity(role=Role.ADMIN, scopes=[ScopeRef(project_id="p1")]),
    )

    with TestClient(app) as client:
        assigned = client.put(
            f"/v1/documents/{document.id}/artifact-connection",
            json={"connection_id": "a" * 32},
        )
        conflicting = client.put(
            f"/v1/documents/{document.id}/artifact-connection",
            json={"connection_id": "b" * 32},
        )

    assert assigned.status_code == 204
    assert conflicting.status_code == 409
    assert document.artifact_connection_id == "a" * 32
    assert document.cleanup_attempt_count == 0
    assert document.cleanup_last_error is None


def test_legacy_affinity_rejects_nul_connection_before_database_or_provider_io() -> None:
    repo = FakeDocumentsRepository()
    document = make_document("legacy-nul", "p1")
    repo.rows[document.id] = document
    resolver = FakeArtifactResolver()
    app = make_app(
        repo,
        identity(role=Role.ADMIN, scopes=[ScopeRef(project_id="p1")]),
        resolver=resolver,
    )

    with TestClient(app) as client:
        response = client.put(
            f"/v1/documents/{document.id}/artifact-connection",
            json={"connection_id": "artifacts\x00p1"},
        )

    assert response.status_code == 422
    assert resolver.calls == []
    assert document.artifact_connection_id is None


def test_legacy_affinity_rejects_candidate_store_without_exact_artifact() -> None:
    repo = FakeDocumentsRepository()
    document = make_document("legacy-missing", "p1")
    repo.rows[document.id] = document

    with TestClient(
        make_app(
            repo,
            identity(role=Role.ADMIN, scopes=[ScopeRef(project_id="p1")]),
        )
    ) as client:
        response = client.put(
            f"/v1/documents/{document.id}/artifact-connection",
            json={"connection_id": "a" * 32},
        )

    assert response.status_code == 409
    assert document.artifact_connection_id is None


def test_legacy_affinity_closes_database_context_during_provider_verification() -> None:
    repo = FakeDocumentsRepository()
    document = make_document("legacy-session", "p1")
    repo.rows[document.id] = document
    active_contexts = 0
    lifecycle: list[str] = []

    @asynccontextmanager
    async def open_repository() -> AsyncIterator[FakeDocumentsRepository]:
        nonlocal active_contexts
        active_contexts += 1
        lifecycle.append("db-open")
        try:
            yield repo
        finally:
            active_contexts -= 1
            lifecycle.append("db-close")

    class CheckingStore:
        def iter_bytes(self, key: str) -> AsyncIterator[bytes]:
            async def chunks() -> AsyncIterator[bytes]:
                assert active_contexts == 0
                assert key == document.artifact_key
                lifecycle.append("provider-read")
                yield b"present"

            return chunks()

    class CheckingResolver:
        async def resolve_with_connection_id(
            self,
            _kind: object,
            connection_id: str | None = None,
            project_id: str | None = None,
            **_kwargs: object,
        ) -> tuple[CheckingStore, str]:
            assert active_contexts == 0
            assert connection_id == "a" * 32
            assert project_id == "p1"
            lifecycle.append("resolved")
            return CheckingStore(), cast(str, connection_id)

    app = make_app(
        repo,
        identity(role=Role.ADMIN, scopes=[ScopeRef(project_id="p1")]),
        resolver=CheckingResolver(),  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_document_repository_factory] = lambda: open_repository

    with TestClient(app) as client:
        response = client.put(
            f"/v1/documents/{document.id}/artifact-connection",
            json={"connection_id": "a" * 32},
        )

    assert response.status_code == 204
    assert lifecycle == [
        "db-open",
        "db-close",
        "resolved",
        "provider-read",
        "db-open",
        "db-close",
    ]


def test_legacy_affinity_rejects_row_race_after_provider_verification() -> None:
    repo = FakeDocumentsRepository()
    document = make_document("legacy-race", "p1")
    repo.rows[document.id] = document
    asyncio.run(
        MemoryArtifactStore().put(
            document.artifact_key,
            b"legacy bytes",
            content_type="text/plain",
        )
    )

    class RacingResolver(FakeArtifactResolver):
        async def resolve_with_connection_id(
            self,
            _kind: object,
            connection_id: str | None = None,
            project_id: str | None = None,
            **kwargs: object,
        ) -> tuple[Any, str]:
            document.deletion_pending_at = datetime.now(UTC)
            return await super().resolve_with_connection_id(
                _kind,
                connection_id=connection_id,
                project_id=project_id,
                **kwargs,
            )

    with TestClient(
        make_app(
            repo,
            identity(role=Role.ADMIN, scopes=[ScopeRef(project_id="p1")]),
            resolver=RacingResolver(),
        )
    ) as client:
        response = client.put(
            f"/v1/documents/{document.id}/artifact-connection",
            json={"connection_id": "a" * 32},
        )

    assert response.status_code == 409
    assert document.artifact_connection_id is None


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


def test_upload_document_does_not_reflect_malformed_multipart_fields() -> None:
    canary = "authorization-token-do-not-reflect"
    body = (
        b"--BOUND\r\n"
        + f'Content-Disposition: form-data; name="{canary}"\r\n\r\n'.encode()
        + b"value\r\n--BOUND--\r\n"
    )
    app = make_app(FakeDocumentsRepository(), identity())

    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            content=body,
            headers={"Content-Type": "multipart/form-data; boundary=BOUND"},
        )

    assert response.status_code == 422
    assert response.json()["title"] == "malformed multipart body"
    assert canary not in response.text


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


def test_upload_document_rejects_archived_inferred_app_scope() -> None:
    who = identity(scopes=[ScopeRef(project_id="p1", app_id="app-a")])
    app = make_app(
        FakeDocumentsRepository(),
        who,
        catalog=FakeCatalogRepository(archived=True),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("a.txt", b"a", "text/plain")},
            data={"project_id": "p1"},
        )

    assert response.status_code == 422


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


def test_list_documents_rejects_huge_offset_before_repository() -> None:
    repo = seeded_repo()
    app = make_app(repo, identity(role=Role.ADMIN))

    with TestClient(app) as client:
        response = client.get("/v1/documents", params={"offset": 10_001})

    assert response.status_code == 422
    assert repo.list_calls == 0


def test_get_document_found_and_scoped_404() -> None:
    who = identity(scopes=[ScopeRef(project_id="p1")])
    app = make_app(seeded_repo(), who)
    with TestClient(app) as client:
        assert client.get("/v1/documents/d1").status_code == 200
        assert client.get("/v1/documents/d2").status_code == 404  # out of scope
        assert client.get("/v1/documents/missing").status_code == 404


def test_document_not_found_does_not_reflect_signed_query_shaped_id() -> None:
    canary = "sig=secret-canary"
    with TestClient(
        make_app(seeded_repo(), identity(scopes=[ScopeRef(project_id="p1")]))
    ) as client:
        response = client.get(f"/v1/documents/{canary}")

    assert response.status_code == 404
    assert response.json()["title"] == "document not found"
    assert canary not in response.text


def test_get_document_redacts_legacy_parse_error_credentials() -> None:
    canary = "legacy-parser-secret-canary-2c8f"
    repo = seeded_repo()
    repo.rows["d1"].parse_error = f"Authorization: Bearer {canary}"

    with TestClient(make_app(repo, identity(scopes=[ScopeRef(project_id="p1")]))) as client:
        response = client.get("/v1/documents/d1")

    assert response.status_code == 200
    assert canary not in response.text
    assert response.json()["parse_error"] == "Authorization: [REDACTED]"


def test_delete_document_removes_row_only() -> None:
    repo = seeded_repo()
    app = make_app(repo, identity(role=Role.ADMIN))
    with TestClient(app) as client:
        response = client.delete("/v1/documents/d1")
    assert response.status_code == 204
    assert "d1" not in repo.rows


def test_delete_document_never_touches_bytes_before_tombstone_commit() -> None:
    repo = seeded_repo()
    repo.fail_mark_deletion = True
    key = repo.rows["d1"].artifact_key
    asyncio.run(MemoryArtifactStore().put(key, b"still-present", content_type="text/plain"))

    with TestClient(make_app(repo, identity(role=Role.ADMIN))) as client:
        response = client.delete("/v1/documents/d1")

    assert response.status_code == 503
    assert repo.rows["d1"].deletion_pending_at is None
    assert asyncio.run(MemoryArtifactStore().get(key)) == b"still-present"


def test_delete_document_keeps_hidden_tombstone_when_metadata_finalize_fails() -> None:
    repo = seeded_repo()
    repo.fail_complete_deletion = True
    key = repo.rows["d1"].artifact_key
    asyncio.run(MemoryArtifactStore().put(key, b"delete-me", content_type="text/plain"))

    with TestClient(make_app(repo, identity(role=Role.ADMIN))) as client:
        response = client.delete("/v1/documents/d1")
        hidden = client.get("/v1/documents/d1")

    assert response.status_code == 503
    assert hidden.status_code == 404
    assert repo.rows["d1"].deletion_pending_at is not None
    with pytest.raises(KeyError):
        asyncio.run(MemoryArtifactStore().get(key))


def test_delete_document_without_store_delete_capability_stays_tombstoned() -> None:
    class ReadOnlyStore:
        pass

    repo = seeded_repo()
    resolver = FakeArtifactResolver(ReadOnlyStore())

    with TestClient(make_app(repo, identity(role=Role.ADMIN), resolver=resolver)) as client:
        response = client.delete("/v1/documents/d1")

    assert response.status_code == 503
    assert repo.rows["d1"].deletion_pending_at is not None


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
