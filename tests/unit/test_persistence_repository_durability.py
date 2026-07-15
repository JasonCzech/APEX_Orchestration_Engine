"""Focused durability regressions over the real SQLAlchemy statements."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from apex.persistence.models import Base, Connection, Document, EngineRun
from apex.persistence.repositories.connections import ConnectionsRepository
from apex.persistence.repositories.documents import (
    DocumentsRepository,
    DocumentUploadNotPendingError,
)
from apex.persistence.repositories.engine_runs import EngineRunsRepository
from apex.services.connections import stored_connection_from_row


class _AsyncFacade:
    """Run async repository methods against a synchronous in-memory test session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instance: Any) -> None:
        self._session.add(instance)

    async def scalar(self, statement: Any) -> Any:
        return self._session.scalar(statement)

    async def scalars(self, statement: Any) -> Any:
        return self._session.scalars(statement)

    async def execute(self, statement: Any) -> Any:
        return self._session.execute(statement)

    async def commit(self) -> None:
        self._session.commit()

    async def rollback(self) -> None:
        self._session.rollback()

    async def refresh(self, instance: Any) -> None:
        self._session.refresh(instance)


def _schema_engine(*table_keys: str) -> Engine:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        Base.metadata.tables["apex.connections"].create(connection)
        for table_key in table_keys:
            Base.metadata.tables[table_key].create(connection)
    return engine


@pytest.mark.parametrize("kind", ["execution_engine", "artifact_store"])
def test_connection_metadata_rename_preserves_runtime_generation(kind: str) -> None:
    engine = _schema_engine("apex.host_mappings")
    original = datetime(2020, 1, 1, tzinfo=UTC)
    try:
        with Session(engine) as session:
            row = Connection(
                id=("e" if kind == "execution_engine" else "a") * 32,
                kind=kind,
                provider="sim" if kind == "execution_engine" else "stub",
                name=f"{kind}-original",
                options={},
                enabled=True,
                created_at=original,
                updated_at=original,
                runtime_version=original,
            )
            session.add(row)
            session.commit()
            checkpointed_generation = stored_connection_from_row(row).runtime_version
            repository = ConnectionsRepository(cast(AsyncSession, _AsyncFacade(session)))

            asyncio.run(repository.update(row, {"name": f"{kind}-renamed"}))

            assert row.updated_at != original
            assert row.runtime_version == checkpointed_generation
            assert row.runtime_version.replace(tzinfo=UTC) == original
    finally:
        engine.dispose()


def test_runtime_connection_mutations_advance_generation() -> None:
    engine = _schema_engine("apex.host_mappings")
    original = datetime(2020, 1, 1, tzinfo=UTC)
    try:
        with Session(engine) as session:
            row = Connection(
                id="r" * 32,
                kind="work_tracking",
                provider="stub",
                name="runtime-versioned",
                options={},
                enabled=True,
                created_at=original,
                updated_at=original,
                runtime_version=original,
            )
            session.add(row)
            session.commit()
            repository = ConnectionsRepository(cast(AsyncSession, _AsyncFacade(session)))

            asyncio.run(repository.update(row, {"options": {"project": "changed"}}))
            after_options = row.runtime_version
            assert after_options != original

            row.runtime_version = original
            session.commit()
            asyncio.run(repository.set_enabled(row, False))

            assert row.runtime_version != original
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("operation", "changes", "error"),
    [
        (
            "create",
            {"options": {"auth": [{"access_token": "must-never-be-persisted"}]}},
            "secrets must be supplied through secret_ref",
        ),
        (
            "create",
            {"secret_ref": "vault:path/to/key"},
            "supported env:NAME reference format",
        ),
        (
            "update",
            {"options": {"password": "must-never-be-persisted"}},
            "secrets must be supplied through secret_ref",
        ),
        (
            "update",
            {"secret_ref": "literal-secret-value"},
            "supported env:NAME reference format",
        ),
        (
            "create",
            {"base_url": "https://user:must-never-be-persisted@example.com"},
            "base_url contains unsafe credential-bearing configuration",
        ),
        (
            "update",
            {"options": {"endpoint": "https://example.com?key=must-never-be-persisted"}},
            "options contain unsafe credential-bearing transport configuration",
        ),
    ],
)
def test_connections_repository_rejects_secret_bearing_direct_writes(
    operation: str,
    changes: dict[str, Any],
    error: str,
) -> None:
    # Validation must happen before the repository touches the session, so a
    # non-HTTP/bootstrap caller cannot bypass the credential boundary.
    repository = ConnectionsRepository(cast(AsyncSession, object()))

    with pytest.raises(ValueError, match=error) as exc_info:
        if operation == "create":
            asyncio.run(
                repository.create(
                    kind="work_tracking",
                    provider="stub",
                    name="direct-writer",
                    **changes,
                )
            )
        else:
            asyncio.run(repository.update(cast(Connection, object()), changes))

    assert "must-never-be-persisted" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("operation", "options"),
    [
        ("create", []),
        ("create", "not-an-object"),
        ("update", None),
        ("update", []),
        ("update", "not-an-object"),
    ],
)
def test_connections_repository_rejects_non_object_options(
    operation: str,
    options: Any,
) -> None:
    repository = ConnectionsRepository(cast(AsyncSession, object()))

    with pytest.raises(ValueError, match="options must be a JSON object"):
        if operation == "create":
            asyncio.run(
                repository.create(
                    kind="work_tracking",
                    provider="stub",
                    name="direct-writer",
                    options=options,
                )
            )
        else:
            asyncio.run(repository.update(cast(Connection, object()), {"options": options}))


def test_dev_document_connection_id_is_normalized_before_fk_insert() -> None:
    engine = _schema_engine("apex.documents")
    document_id = "d" * 32
    try:
        with Session(engine) as session:
            repository = DocumentsRepository(cast(AsyncSession, _AsyncFacade(session)))
            document = Document(
                id=document_id,
                name="dev.txt",
                media_type="text/plain",
                size_bytes=3,
                artifact_key=f"documents/{document_id}/dev.txt",
                artifact_connection_id="dev-artifact-store-memory",
            )

            asyncio.run(repository.add(document))
            persisted = session.get(Document, document_id)

            assert persisted is not None
            assert persisted.artifact_connection_id is None
    finally:
        engine.dispose()


def _pending_document(document_id: str) -> Document:
    return Document(
        id=document_id,
        name="pending.txt",
        media_type="text/plain",
        size_bytes=3,
        artifact_key=f"documents/{document_id}/pending.txt",
    )


def test_document_finalizer_wins_against_stale_cleanup_snapshot() -> None:
    engine = _schema_engine("apex.documents")
    try:
        with Session(engine, expire_on_commit=False) as session:
            repository = DocumentsRepository(cast(AsyncSession, _AsyncFacade(session)))
            document = _pending_document("f" * 32)
            asyncio.run(repository.stage_upload(document))
            stale_snapshot = _pending_document(document.id)
            stale_snapshot.upload_pending_at = document.upload_pending_at
            document.summary = "persisted summary"
            document.extracted_text = "persisted text"
            document.extracted_chars = len("persisted text")
            document.parse_status = "parsed"

            finalized = asyncio.run(repository.finalize_upload(document))
            claimed = asyncio.run(repository.claim_stale_upload(stale_snapshot))

            assert finalized.upload_pending_at is None
            assert claimed is None
            persisted = session.get(Document, document.id)
            assert persisted is not None
            assert persisted.deletion_pending_at is None
            assert persisted.summary == "persisted summary"
            assert persisted.extracted_text == "persisted text"
            assert persisted.parse_status == "parsed"
    finally:
        engine.dispose()


def test_document_cleanup_claim_prevents_late_finalize() -> None:
    engine = _schema_engine("apex.documents")
    try:
        with Session(engine, expire_on_commit=False) as session:
            repository = DocumentsRepository(cast(AsyncSession, _AsyncFacade(session)))
            document = _pending_document("c" * 32)
            asyncio.run(repository.stage_upload(document))

            claimed = asyncio.run(repository.claim_stale_upload(document))

            assert claimed is not None
            assert claimed.deletion_pending_at is not None
            with pytest.raises(DocumentUploadNotPendingError):
                asyncio.run(repository.finalize_upload(document))
    finally:
        engine.dispose()


def test_upload_failure_tombstone_is_id_based_and_rejects_finalized_rows() -> None:
    engine = _schema_engine("apex.documents")
    try:
        with Session(engine, expire_on_commit=False) as session:
            repository = DocumentsRepository(cast(AsyncSession, _AsyncFacade(session)))
            failed = _pending_document("e" * 32)
            finalized = _pending_document("a" * 32)
            asyncio.run(repository.stage_upload(failed))
            asyncio.run(repository.stage_upload(finalized))
            asyncio.run(repository.finalize_upload(finalized))

            tombstone = asyncio.run(repository.mark_upload_deletion_pending(failed.id))
            refused = asyncio.run(repository.mark_upload_deletion_pending(finalized.id))

            assert tombstone is not None
            assert tombstone.id == failed.id
            assert tombstone.deletion_pending_at is not None
            assert refused is None
            persisted_finalized = session.get(Document, finalized.id)
            assert persisted_finalized is not None
            assert persisted_finalized.deletion_pending_at is None
    finally:
        engine.dispose()


def test_document_text_fields_are_nul_sanitized_before_persistence() -> None:
    engine = _schema_engine("apex.documents")
    try:
        with Session(engine, expire_on_commit=False) as session:
            repository = DocumentsRepository(cast(AsyncSession, _AsyncFacade(session)))
            document = _pending_document("0" * 32)
            document.summary = "summary\x00value"
            asyncio.run(repository.stage_upload(document))
            document.extracted_text = "extracted\x00value"
            document.parse_error = "parse\x00error"

            finalized = asyncio.run(repository.finalize_upload(document))

            assert finalized.summary == "summary\ufffdvalue"
            assert finalized.extracted_text == "extracted\ufffdvalue"
            assert finalized.parse_error == "parse\ufffderror"
    finally:
        engine.dispose()


def test_document_durable_diagnostics_redact_credentials_before_persistence() -> None:
    engine = _schema_engine("apex.documents")
    canary = "document-diagnostic-secret-canary"
    try:
        with Session(engine, expire_on_commit=False) as session:
            repository = DocumentsRepository(cast(AsyncSession, _AsyncFacade(session)))
            document = _pending_document("d" * 32)
            document.parse_error = f"Authorization: Bearer {canary}"
            asyncio.run(repository.stage_upload(document))
            document.upload_pending_at = None
            document.deletion_pending_at = datetime.now(UTC)
            session.commit()

            deferred = asyncio.run(
                repository.defer_cleanup(
                    document.id,
                    error=(
                        "cleanup failed for "
                        f"https://storage.example/blob?X-Amz-Signature={canary}"
                    ),
                )
            )

            persisted = session.get(Document, document.id)
            assert deferred is True
            assert persisted is not None
            assert canary not in (persisted.parse_error or "")
            assert canary not in (persisted.cleanup_last_error or "")
            assert "[REDACTED]" in (persisted.parse_error or "")
            assert "[REDACTED]" in (persisted.cleanup_last_error or "")
    finally:
        engine.dispose()


def test_document_upload_lease_renewal_invalidates_stale_cleanup_snapshot() -> None:
    engine = _schema_engine("apex.documents")
    try:
        with Session(engine, expire_on_commit=False) as session:
            repository = DocumentsRepository(cast(AsyncSession, _AsyncFacade(session)))
            document = _pending_document("l" * 32)
            asyncio.run(repository.stage_upload(document))
            stale_snapshot = _pending_document(document.id)
            stale_snapshot.upload_pending_at = document.upload_pending_at

            assert asyncio.run(repository.renew_upload_lease(document.id)) is True
            claimed = asyncio.run(repository.claim_stale_upload(stale_snapshot))
            finalized = asyncio.run(repository.finalize_upload(document))

            assert claimed is None
            assert finalized.upload_pending_at is None
            assert finalized.deletion_pending_at is None
    finally:
        engine.dispose()


def test_document_cleanup_backoff_prevents_poison_head_starvation() -> None:
    engine = _schema_engine("apex.documents")
    pending_at = datetime.now(UTC) - timedelta(hours=2)
    try:
        with Session(engine, expire_on_commit=False) as session:
            documents = []
            for index in range(101):
                document = _pending_document(f"{index:032x}")
                document.upload_pending_at = None
                document.deletion_pending_at = pending_at
                documents.append(document)
            session.add_all(documents)
            session.commit()
            repository = DocumentsRepository(cast(AsyncSession, _AsyncFacade(session)))

            first_batch = asyncio.run(repository.list_pending_deletions(limit=100))
            for document in first_batch:
                assert asyncio.run(
                    repository.defer_cleanup(document.id, error="PermanentStoreFailure")
                )
            next_batch = asyncio.run(repository.list_pending_deletions(limit=100))

            assert len(first_batch) == 100
            assert [document.id for document in next_batch] == [f"{100:032x}"]
            deferred = session.get(Document, first_batch[0].id)
            assert deferred is not None
            assert deferred.cleanup_attempt_count == 1
            assert deferred.cleanup_retry_at is not None
            assert deferred.cleanup_last_error == "PermanentStoreFailure"
    finally:
        engine.dispose()


def test_legacy_document_artifact_affinity_assignment_is_scoped_and_immutable() -> None:
    engine = _schema_engine("apex.documents")
    try:
        with Session(engine, expire_on_commit=False) as session:
            session.add_all(
                [
                    Connection(
                        id="a" * 32,
                        kind="artifact_store",
                        provider="memory",
                        name="global-artifacts",
                        project_id=None,
                        options={},
                        enabled=True,
                    ),
                    Connection(
                        id="b" * 32,
                        kind="artifact_store",
                        provider="memory",
                        name="other-artifacts",
                        project_id="p2",
                        options={},
                        enabled=True,
                    ),
                ]
            )
            document = _pending_document("d" * 32)
            document.project_id = "p1"
            document.upload_pending_at = None
            document.cleanup_attempt_count = 4
            document.cleanup_retry_at = datetime.now(UTC)
            document.cleanup_last_error = "missing object"
            session.add(document)
            session.commit()
            repository = DocumentsRepository(cast(AsyncSession, _AsyncFacade(session)))
            loaded = asyncio.run(repository.get_any_for_update(document.id))
            assert loaded is not None

            with pytest.raises(ValueError, match="outside the document project"):
                asyncio.run(repository.assign_artifact_connection(loaded, "b" * 32))

            assigned = asyncio.run(repository.assign_artifact_connection(loaded, "a" * 32))
            assert assigned.artifact_connection_id == "a" * 32
            assert assigned.cleanup_attempt_count == 0
            assert assigned.cleanup_retry_at is None
            assert assigned.cleanup_last_error is None
            with pytest.raises(ValueError, match="already fixed"):
                asyncio.run(repository.assign_artifact_connection(assigned, "b" * 32))
    finally:
        engine.dispose()


def test_mark_terminal_preserves_prior_terminal_attempts() -> None:
    engine = _schema_engine("apex.engine_runs")
    prior_ended_at = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    EngineRun(
                        id="1" * 32,
                        thread_id="thread-1",
                        attempt=1,
                        engine="sim",
                        handle={},
                        status="failed",
                        ended_at=prior_ended_at,
                    ),
                    EngineRun(
                        id="2" * 32,
                        thread_id="thread-1",
                        attempt=2,
                        engine="sim",
                        handle={},
                        status="running",
                    ),
                    EngineRun(
                        id="3" * 32,
                        thread_id="thread-1",
                        attempt=3,
                        engine="sim",
                        handle={},
                        status="running",
                    ),
                ]
            )
            session.commit()
            repository = EngineRunsRepository(cast(AsyncSession, _AsyncFacade(session)))

            changed = asyncio.run(
                repository.mark_terminal(
                    "thread-1",
                    "aborted",
                    projection_id="2" * 32,
                    attempt=2,
                    expected_external_run_id=None,
                )
            )
            session.expire_all()
            attempts = list(session.scalars(select(EngineRun).order_by(EngineRun.attempt)))

            assert changed == 1
            assert [(row.attempt, row.status) for row in attempts] == [
                (1, "failed"),
                (2, "aborted"),
                (3, "running"),
            ]
            assert attempts[0].ended_at == prior_ended_at.replace(tzinfo=None)
            assert attempts[1].ended_at is not None
    finally:
        engine.dispose()
