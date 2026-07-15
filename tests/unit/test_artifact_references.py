"""Durable artifact-reference batch and commit-ambiguity regressions."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import apex.services.artifact_references as artifact_reference_service
from apex.persistence.models import ArtifactReference, ArtifactUploadIntent
from apex.ports.artifact_store import StoredArtifact
from apex.services.artifact_references import (
    MAX_DURABLE_ARTIFACT_UPLOAD_BACKLOG_BYTES,
    ArtifactReferenceInput,
    ArtifactUploadBacklogFullError,
    ArtifactUploadReplay,
    ArtifactUploadReservation,
    _commit_references,
    _pending_upload_claim_statement,
    _stage_upload_intent,
    _upload_values,
    persist_artifact_with_intent,
    record_artifact_references,
    replay_artifact_upload,
)


class FailingCommitSession:
    def __init__(self, error: Exception, rows: list[ArtifactReference]) -> None:
        self.error = error
        self.rows = rows
        self.rolled_back = False

    async def execute(self, _statement: Any) -> None:
        return None

    async def scalars(self, _statement: Any) -> list[ArtifactReference]:
        return self.rows

    async def commit(self) -> None:
        raise self.error

    async def rollback(self) -> None:
        self.rolled_back = True


class ResolutionSession:
    def __init__(self, rows: list[ArtifactReference]) -> None:
        self.rows = rows

    async def __aenter__(self) -> "ResolutionSession":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def scalars(self, _statement: Any) -> list[ArtifactReference]:
        return self.rows


class ResolutionFactory:
    def __init__(self, rows: list[ArtifactReference]) -> None:
        self.rows = rows

    def __call__(self) -> ResolutionSession:
        return ResolutionSession(self.rows)


class SuccessfulSession:
    def __init__(self, rows: list[ArtifactReference]) -> None:
        self.rows = rows
        self.committed = False

    async def __aenter__(self) -> "SuccessfulSession":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def scalar(self, _statement: Any) -> object:
        return SimpleNamespace(enabled=True, kind="artifact_store")

    def get_bind(self) -> object:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def execute(self, _statement: Any) -> None:
        return None

    async def scalars(self, _statement: Any) -> list[ArtifactReference]:
        return self.rows

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


class SuccessfulFactory:
    def __init__(self, session: SuccessfulSession) -> None:
        self.session = session

    def __call__(self) -> SuccessfulSession:
        return self.session


class DisposeFailingEngine:
    def __init__(self) -> None:
        self.dispose_called = False

    async def dispose(self) -> None:
        self.dispose_called = True
        raise RuntimeError("pool cleanup failed")


class StageSession:
    def __init__(self, scalar_results: list[Any]) -> None:
        self.scalar_results = iter(scalar_results)
        self.added: list[Any] = []

    async def scalar(self, _statement: Any) -> Any:
        return next(self.scalar_results)

    def add(self, value: Any) -> None:
        self.added.append(value)


class CancelledCommitSession:
    def __init__(self) -> None:
        self.durable_intent = False
        self.rolled_back = False

    async def __aenter__(self) -> "CancelledCommitSession":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def commit(self) -> None:
        # Simulate a cancellation delivered after PostgreSQL committed the row
        # but before the driver returned its acknowledgement.
        self.durable_intent = True
        raise asyncio.CancelledError

    async def rollback(self) -> None:
        self.rolled_back = True


class SingleSessionFactory:
    def __init__(self, session: Any) -> None:
        self.session = session

    def __call__(self) -> Any:
        return self.session


class CleanEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def _values(suffix: str) -> dict[str, Any]:
    return {
        "artifact_key": f"engine-runs/hash/{suffix}.json",
        "connection_id": "store-1",
        "kind": "engine_results",
        "thread_id": "thread-1",
        "project_id": "p1",
        "app_id": "a1",
        "ownership_known": True,
    }


def _row(values: dict[str, Any], *, row_id: str) -> ArtifactReference:
    return ArtifactReference(id=row_id, **values)


async def test_commit_error_is_success_only_when_every_exact_reference_persisted() -> None:
    error = ConnectionError("commit acknowledgement lost")
    values = [_values("results"), _values("summary")]
    rows = [_row(value, row_id=f"r{index}") for index, value in enumerate(values)]
    writer = FailingCommitSession(error, rows)

    await _commit_references(
        cast(AsyncSession, writer),
        cast(async_sessionmaker[AsyncSession], ResolutionFactory(rows)),
        object(),
        values,
    )

    assert writer.rolled_back is True


async def test_commit_error_is_raised_when_even_one_reference_is_not_confirmed() -> None:
    error = ConnectionError("commit acknowledgement lost")
    values = [_values("results"), _values("summary")]
    rows = [_row(value, row_id=f"r{index}") for index, value in enumerate(values)]
    writer = FailingCommitSession(error, rows)

    with pytest.raises(ConnectionError) as raised:
        await _commit_references(
            cast(AsyncSession, writer),
            cast(async_sessionmaker[AsyncSession], ResolutionFactory(rows[:1])),
            object(),
            values,
        )

    assert raised.value is error


async def test_batch_rejects_duplicate_keys_before_opening_database() -> None:
    with pytest.raises(ValueError, match="duplicate keys"):
        await record_artifact_references(
            [
                ArtifactReferenceInput(artifact_key="same", kind="engine_results"),
                ArtifactReferenceInput(artifact_key="same", kind="engine_report"),
            ],
            connection_id="store-1",
            thread_id="thread-1",
            project_id="p1",
            app_id=None,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_key", "key\x00shadow"),
        ("connection_id", "store\x00shadow"),
        ("kind", "result\x00shadow"),
        ("thread_id", "thread\x00shadow"),
        ("project_id", "project\x00shadow"),
        ("app_id", "app\x00shadow"),
        ("content_type", "text/plain\x00shadow"),
    ],
)
def test_upload_values_reject_nul_metadata_before_database_or_provider(
    field: str,
    value: str,
) -> None:
    kwargs: dict[str, Any] = {
        "artifact_key": "artifacts/result.json",
        "connection_id": "store-1",
        "kind": "result",
        "thread_id": "thread-1",
        "project_id": "project-1",
        "app_id": "app-1",
        "payload": b"{}",
        "content_type": "application/json",
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match="without U\\+0000"):
        _upload_values(**kwargs)


async def test_committed_batch_is_not_failed_when_engine_dispose_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = [_values("results")]
    session = SuccessfulSession([_row(values[0], row_id="r1")])
    factory = SuccessfulFactory(session)
    engine = DisposeFailingEngine()
    monkeypatch.setattr(
        artifact_reference_service,
        "create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        artifact_reference_service,
        "async_sessionmaker",
        lambda *_args, **_kwargs: factory,
    )

    await record_artifact_references(
        [ArtifactReferenceInput(artifact_key=values[0]["artifact_key"], kind="engine_results")],
        connection_id="store-1",
        thread_id="thread-1",
        project_id="p1",
        app_id="a1",
    )

    assert session.committed is True
    assert engine.dispose_called is True


async def test_artifact_upload_outbox_is_reserved_before_provider_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class OrderedStore:
        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            events.append("put")
            return StoredArtifact(key=key, uri=f"memory://{key}", size=len(data))

    async def reserve(**_kwargs: Any) -> ArtifactUploadReservation:
        events.append("reserve")
        return ArtifactUploadReservation(durable=True, already_finalized=False, owned=True)

    async def finalize(**_kwargs: Any) -> None:
        events.append("finalize")

    monkeypatch.setattr(artifact_reference_service, "reserve_artifact_upload", reserve)
    monkeypatch.setattr(artifact_reference_service, "finalize_artifact_upload", finalize)

    stored = await persist_artifact_with_intent(
        cast(Any, OrderedStore()),
        artifact_key="transcripts/thread/story/attempt-1.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        payload=b"durable transcript",
        content_type="text/plain",
    )

    assert events == ["reserve", "put", "finalize"]
    assert stored.uri == "apex-artifact:///transcripts/thread/story/attempt-1.txt"


async def test_finalized_artifact_replay_never_mints_provider_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CapabilityStore:
        async def get_url(self, _key: str) -> str:
            raise AssertionError("finalized replay must not mint a presigned URL")

        async def put(self, *_args: Any, **_kwargs: Any) -> StoredArtifact:
            raise AssertionError("finalized replay must not write provider bytes")

    async def reserve(**_kwargs: Any) -> ArtifactUploadReservation:
        return ArtifactUploadReservation(durable=True, already_finalized=True, owned=False)

    monkeypatch.setattr(artifact_reference_service, "reserve_artifact_upload", reserve)

    stored = await persist_artifact_with_intent(
        cast(Any, CapabilityStore()),
        artifact_key="transcripts/thread/a b/attempt-1.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        payload=b"same bytes",
        content_type="text/plain",
    )

    assert stored.key == "transcripts/thread/a b/attempt-1.txt"
    assert stored.uri == "apex-artifact:///transcripts/thread/a%20b/attempt-1.txt"
    assert stored.size == len(b"same bytes")


async def test_artifact_upload_rejects_provider_key_drift_before_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalized = False

    class RedirectingStore:
        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            return StoredArtifact(key="other-prefix/transcript.txt", uri="memory://other", size=1)

    async def reserve(**_kwargs: Any) -> ArtifactUploadReservation:
        return ArtifactUploadReservation(durable=True, already_finalized=False, owned=True)

    async def finalize(**_kwargs: Any) -> None:
        nonlocal finalized
        finalized = True

    monkeypatch.setattr(artifact_reference_service, "reserve_artifact_upload", reserve)
    monkeypatch.setattr(artifact_reference_service, "finalize_artifact_upload", finalize)

    with pytest.raises(RuntimeError, match="different from the reserved key") as raised:
        await persist_artifact_with_intent(
            cast(Any, RedirectingStore()),
            artifact_key="transcripts/thread/story/attempt-1.txt",
            connection_id="store-1",
            kind="transcript",
            thread_id="thread",
            project_id="p1",
            app_id=None,
            payload=b"x",
            content_type="text/plain",
        )

    assert "other-prefix" not in str(raised.value)
    assert finalized is False


async def test_cancelled_reservation_commit_never_starts_foreground_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CancelledCommitSession()
    engine = CleanEngine()
    puts = 0

    class Store:
        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            nonlocal puts
            puts += 1
            return StoredArtifact(key=key, uri=f"memory://{key}", size=len(data))

    async def stage(*_args: Any, **_kwargs: Any) -> tuple[bool, bool]:
        return False, True

    monkeypatch.setattr(
        artifact_reference_service,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=True),
    )
    monkeypatch.setattr(
        artifact_reference_service,
        "_new_session_factory",
        lambda: (engine, SingleSessionFactory(session)),
    )
    monkeypatch.setattr(artifact_reference_service, "_stage_upload_intent", stage)

    with pytest.raises(asyncio.CancelledError):
        await persist_artifact_with_intent(
            cast(Any, Store()),
            artifact_key="transcripts/thread/story/attempt-1.txt",
            connection_id="store-1",
            kind="transcript",
            thread_id="thread",
            project_id="p1",
            app_id=None,
            payload=b"durably queued",
            content_type="text/plain",
        )

    assert session.durable_intent is True
    assert session.rolled_back is True
    assert engine.disposed is True
    assert puts == 0


async def test_existing_pending_upload_does_not_create_a_second_writer() -> None:
    values = _upload_values(
        artifact_key="transcripts/thread/story/attempt-1.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        payload=b"same bytes",
        content_type="text/plain",
    )
    connection = SimpleNamespace(enabled=True, kind="artifact_store")
    first_session = StageSession([connection, None, None, 0])

    assert await _stage_upload_intent(
        cast(AsyncSession, first_session), values, claim_token="a" * 32
    ) == (False, True)
    intent = cast(ArtifactUploadIntent, first_session.added[0])

    second_session = StageSession([connection, None, intent])
    assert await _stage_upload_intent(
        cast(AsyncSession, second_session), values, claim_token="b" * 32
    ) == (False, False)
    assert second_session.added == []


async def test_finalized_upload_requires_the_same_exact_content_identity() -> None:
    values = _upload_values(
        artifact_key="transcripts/thread/story/attempt-1.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        payload=b"original bytes",
        content_type="text/plain",
    )
    connection = SimpleNamespace(enabled=True, kind="artifact_store")
    reference = ArtifactReference(
        id="r1",
        artifact_key=values["artifact_key"],
        connection_id=values["connection_id"],
        kind=values["kind"],
        thread_id=values["thread_id"],
        project_id=values["project_id"],
        app_id=values["app_id"],
        ownership_known=True,
        content_sha256=values["content_sha256"],
        size_bytes=values["size_bytes"],
        content_type=values["content_type"],
    )

    exact_retry = StageSession([connection, reference])
    assert await _stage_upload_intent(
        cast(AsyncSession, exact_retry), values, claim_token="a" * 32
    ) == (True, False)

    changed = _upload_values(
        artifact_key=values["artifact_key"],
        connection_id=values["connection_id"],
        kind=values["kind"],
        thread_id=values["thread_id"],
        project_id=values["project_id"],
        app_id=values["app_id"],
        payload=b"changed bytes",
        content_type="text/plain",
    )
    changed_retry = StageSession([connection, reference])
    with pytest.raises(RuntimeError, match="different durable ownership"):
        await _stage_upload_intent(cast(AsyncSession, changed_retry), changed, claim_token="b" * 32)


async def test_legacy_finalized_upload_without_content_identity_fails_closed() -> None:
    values = _upload_values(
        artifact_key="transcripts/thread/story/attempt-legacy.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        payload=b"cannot prove legacy bytes",
        content_type="text/plain",
    )
    connection = SimpleNamespace(enabled=True, kind="artifact_store")
    legacy_reference = ArtifactReference(
        id="legacy",
        **{
            key: values[key]
            for key in (
                "artifact_key",
                "connection_id",
                "kind",
                "thread_id",
                "project_id",
                "app_id",
                "ownership_known",
            )
        },
    )

    with pytest.raises(RuntimeError, match="different durable ownership"):
        await _stage_upload_intent(
            cast(AsyncSession, StageSession([connection, legacy_reference])),
            values,
            claim_token="c" * 32,
        )


async def test_artifact_upload_outbox_rejects_unbounded_per_store_byte_backlog() -> None:
    values = _upload_values(
        artifact_key="transcripts/thread/story/attempt-2.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        payload=b"x",
        content_type="text/plain",
    )
    connection = SimpleNamespace(enabled=True, kind="artifact_store")
    session = StageSession([connection, None, None, MAX_DURABLE_ARTIFACT_UPLOAD_BACKLOG_BYTES])

    with pytest.raises(ArtifactUploadBacklogFullError, match="backlog is full"):
        await _stage_upload_intent(cast(AsyncSession, session), values, claim_token="c" * 32)

    assert session.added == []


def test_reconciler_claim_query_serializes_replicas_with_skip_locked() -> None:
    statement = _pending_upload_claim_statement(datetime.now(UTC))
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "claimed_at" in sql
    assert "payload" not in sql
    assert "content_type" not in sql


async def test_replay_puts_exact_outbox_payload_before_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []

    class ReplayStore:
        async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
            events.append(("put", (key, data, content_type)))
            return StoredArtifact(key=key, uri=f"memory://{key}", size=len(data))

    class Resolver:
        async def resolve_with_connection_id(self, *_args: Any, **kwargs: Any) -> tuple[Any, str]:
            events.append(("resolve", kwargs))
            return ReplayStore(), "store-1"

    async def finalize(**kwargs: Any) -> None:
        events.append(("finalize", kwargs))

    monkeypatch.setattr(artifact_reference_service, "finalize_artifact_upload", finalize)
    upload = ArtifactUploadReplay(
        artifact_key="transcripts/thread/story/attempt-1.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        ownership_known=False,
        payload=b"exact payload",
        content_type="text/plain",
        claim_token="c" * 32,
    )

    await replay_artifact_upload(upload, cast(Any, Resolver()))

    assert [event[0] for event in events] == ["resolve", "put", "finalize"]
    assert events[1][1] == (
        "transcripts/thread/story/attempt-1.txt",
        b"exact payload",
        "text/plain",
    )
    assert events[2][1]["ownership_known"] is False
