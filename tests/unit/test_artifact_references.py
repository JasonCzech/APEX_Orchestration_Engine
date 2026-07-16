"""Durable artifact-reference batch and commit-ambiguity regressions."""

import asyncio
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import apex.services.artifact_references as artifact_reference_service
from apex.persistence.models import ArtifactReference, ArtifactUploadIntent
from apex.ports.artifact_store import StoredArtifact, validate_stored_artifact_ack
from apex.services.artifact_references import (
    MAX_DURABLE_ARTIFACT_UPLOAD_BACKLOG_BYTES,
    ArtifactReferenceInput,
    ArtifactUploadBacklogFullError,
    ArtifactUploadReplay,
    ArtifactUploadReservation,
    _commit_references,
    _finalize_upload_intent,
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


def test_upload_values_reject_header_control_characters_before_database_or_provider() -> None:
    with pytest.raises(ValueError, match="unsafe characters"):
        _upload_values(
            artifact_key="artifacts/result.json",
            connection_id="store-1",
            kind="result",
            thread_id="thread-1",
            project_id="project-1",
            app_id="app-1",
            payload=b"{}",
            content_type="application/json\r\nX-Unsafe: yes",
        )


@pytest.mark.parametrize(
    "field",
    [
        "artifact_key",
        "connection_id",
        "kind",
        "thread_id",
        "project_id",
        "app_id",
        "content_type",
    ],
)
def test_upload_values_reject_credential_metadata_without_reflection(field: str) -> None:
    canary = "meta-secret"
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
    kwargs[field] = f"api_key={canary}"

    with pytest.raises(ValueError, match="credential material") as raised:
        _upload_values(**kwargs)

    assert canary not in str(raised.value)


async def test_replay_revalidates_legacy_metadata_before_provider_resolution() -> None:
    canary = "legacy-meta-secret"

    class Resolver:
        async def resolve_with_connection_id(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("unsafe legacy intent must not resolve a provider")

    upload = ArtifactUploadReplay(
        artifact_key=f"api_key={canary}",
        connection_id="store-1",
        kind="result",
        thread_id="thread-1",
        project_id="project-1",
        app_id="app-1",
        ownership_known=True,
        payload=b"{}",
        content_type="application/json",
        claim_token="claim-1",
    )

    with pytest.raises(ValueError, match="credential material") as raised:
        await replay_artifact_upload(upload, cast(Any, Resolver()))

    assert canary not in str(raised.value)


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


async def test_reference_batch_rejects_cross_project_artifact_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CrossProjectSession(SuccessfulSession):
        async def scalar(self, _statement: Any) -> object:
            return SimpleNamespace(enabled=True, kind="artifact_store", project_id="p2")

    values = [_values("cross-project")]
    session = CrossProjectSession([])
    engine = CleanEngine()
    monkeypatch.setattr(
        artifact_reference_service,
        "create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        artifact_reference_service,
        "async_sessionmaker",
        lambda *_args, **_kwargs: SuccessfulFactory(session),
    )

    with pytest.raises(RuntimeError, match="not available for the artifact project"):
        await record_artifact_references(
            [
                ArtifactReferenceInput(
                    artifact_key=values[0]["artifact_key"],
                    kind="engine_results",
                )
            ],
            connection_id="store-1",
            thread_id="thread-1",
            project_id="p1",
            app_id="a1",
        )

    assert session.committed is False
    assert engine.disposed is True


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

    with pytest.raises(RuntimeError, match="invalid object metadata") as raised:
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


@pytest.mark.parametrize(
    "acknowledgement",
    [
        {
            "key": "transcripts/thread/story/attempt-1.txt",
            "uri": "javascript:alert(1)",
            "size": 1,
        },
        {
            "key": "transcripts/thread/story/attempt-1.txt",
            "uri": "memory://other/object.txt",
            "size": 1,
        },
        {
            "key": "transcripts/thread/story/attempt-1.txt",
            "uri": "memory://transcripts/thread/story/attempt-1.txt",
            "size": 1,
            "provider_token": "artifact-extra-secret-canary",
        },
    ],
)
def test_artifact_ack_rejects_unsafe_uri_or_extra_fields_without_reflection(
    acknowledgement: dict[str, Any],
) -> None:
    with pytest.raises(RuntimeError, match="invalid object metadata") as raised:
        validate_stored_artifact_ack(
            acknowledgement,
            "transcripts/thread/story/attempt-1.txt",
            expected_size=1,
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "artifact-extra-secret-canary" not in str(raised.value)


def test_artifact_ack_sanitizes_hostile_model_dump_exception() -> None:
    secret = "hostile-artifact-model-secret-canary"

    class HostileStoredArtifact(StoredArtifact):
        def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            raise RuntimeError(secret)

    acknowledgement = HostileStoredArtifact(
        key="transcripts/thread/story/attempt-1.txt",
        uri="memory://transcripts/thread/story/attempt-1.txt",
        size=1,
    )
    with pytest.raises(RuntimeError, match="invalid object metadata") as raised:
        validate_stored_artifact_ack(
            acknowledgement,
            "transcripts/thread/story/attempt-1.txt",
            expected_size=1,
        )

    assert raised.value.__cause__ is None
    assert secret not in str(raised.value)


def test_artifact_ack_rejects_hostile_class_descriptor_without_execution() -> None:
    hooks: list[str] = []

    class HostileAcknowledgement:
        def __getattribute__(self, name: str) -> object:
            if name == "__class__":
                hooks.append("class")
                raise AssertionError("provider __class__ hook executed")
            return object.__getattribute__(self, name)

    with pytest.raises(RuntimeError, match="invalid object metadata"):
        validate_stored_artifact_ack(
            HostileAcknowledgement(),
            "transcripts/thread/story/attempt-1.txt",
            expected_size=1,
        )

    assert hooks == []


def test_artifact_ack_rejects_model_copy_extra_fields() -> None:
    secret = "artifact-model-extra-secret-canary"
    acknowledgement = StoredArtifact(
        key="transcripts/thread/story/attempt-1.txt",
        uri="memory://transcripts/thread/story/attempt-1.txt",
        size=1,
    ).model_copy(update={"provider_token": secret})

    with pytest.raises(RuntimeError, match="invalid object metadata") as raised:
        validate_stored_artifact_ack(
            acknowledgement,
            "transcripts/thread/story/attempt-1.txt",
            expected_size=1,
        )

    assert raised.value.__cause__ is None
    assert secret not in str(raised.value)


def test_artifact_ack_sanitizes_hostile_mapping_exception() -> None:
    secret = "hostile-artifact-mapping-secret-canary"

    class HostileMapping(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            del key
            raise RuntimeError(secret)

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError(secret)

        def __len__(self) -> int:
            return 3

    with pytest.raises(RuntimeError, match="invalid object metadata") as raised:
        validate_stored_artifact_ack(
            HostileMapping(),
            "transcripts/thread/story/attempt-1.txt",
            expected_size=1,
        )

    assert raised.value.__cause__ is None
    assert secret not in str(raised.value)


def test_artifact_ack_rejects_oversized_mapping_without_iteration() -> None:
    iterations = 0

    class EndlessMapping(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            return key

        def __iter__(self) -> Iterator[str]:
            nonlocal iterations
            index = 0
            while True:
                iterations += 1
                yield f"field-{index}"
                index += 1

        def __len__(self) -> int:
            return 1_000_000_000

    with pytest.raises(RuntimeError, match="invalid object metadata"):
        validate_stored_artifact_ack(EndlessMapping(), "expected", expected_size=1)

    assert iterations == 0


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


async def test_upload_reservation_rejects_cross_project_artifact_store() -> None:
    values = _upload_values(
        artifact_key="transcripts/thread/story/attempt-cross-project.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        payload=b"bytes",
        content_type="text/plain",
    )
    connection = SimpleNamespace(enabled=True, kind="artifact_store", project_id="p2")
    session = StageSession([connection])

    with pytest.raises(RuntimeError, match="not available for the artifact project"):
        await _stage_upload_intent(
            cast(AsyncSession, session),
            values,
            claim_token="a" * 32,
        )

    assert session.added == []


async def test_upload_finalization_locks_connection_before_intent_transition() -> None:
    values = _upload_values(
        artifact_key="transcripts/thread/story/attempt-finalize.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        payload=b"bytes",
        content_type="text/plain",
    )
    connection = SimpleNamespace(enabled=True, kind="artifact_store", project_id="p1")
    intent = ArtifactUploadIntent(
        id="intent-1",
        claim_token="a" * 32,
        claimed_at=datetime.now(UTC),
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
                "payload",
                "content_type",
            )
        },
    )

    class FinalizeSession:
        def __init__(self) -> None:
            self.results = iter((connection, intent, None))
            self.statements: list[str] = []
            self.added: list[Any] = []
            self.deleted: list[Any] = []

        async def scalar(self, statement: Any) -> Any:
            self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return next(self.results)

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def delete(self, value: Any) -> None:
            self.deleted.append(value)

    session = FinalizeSession()

    await _finalize_upload_intent(cast(AsyncSession, session), values)

    assert "FROM apex.connections" in session.statements[0]
    assert "FOR UPDATE" in session.statements[0]
    assert "FROM apex.artifact_upload_intents" in session.statements[1]
    assert "FOR UPDATE" in session.statements[1]
    assert "FROM apex.artifact_references" in session.statements[2]
    assert len(session.added) == 1
    assert session.deleted == [intent]


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


async def test_replay_rejects_resolver_connection_drift_before_provider_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_puts = 0
    finalizations = 0

    class ReplayStore:
        async def put(self, *_args: Any, **_kwargs: Any) -> StoredArtifact:
            nonlocal provider_puts
            provider_puts += 1
            raise AssertionError("connection drift must fail before provider I/O")

    class Resolver:
        async def resolve_with_connection_id(self, *_args: Any, **_kwargs: Any) -> tuple[Any, str]:
            return ReplayStore(), "store-2"

    async def finalize(**_kwargs: Any) -> None:
        nonlocal finalizations
        finalizations += 1

    monkeypatch.setattr(artifact_reference_service, "finalize_artifact_upload", finalize)
    upload = ArtifactUploadReplay(
        artifact_key="transcripts/thread/story/attempt-1.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        ownership_known=True,
        payload=b"payload",
        content_type="text/plain",
        claim_token="c" * 32,
    )

    with pytest.raises(RuntimeError, match="upload connection affinity"):
        await replay_artifact_upload(upload, cast(Any, Resolver()))

    assert provider_puts == 0
    assert finalizations == 0


async def test_replay_does_not_invoke_polymorphic_resolver_id_comparison() -> None:
    hooks: list[str] = []

    class HostileId(str):
        def __eq__(self, _other: object) -> bool:
            hooks.append("eq")
            return True

        def __ne__(self, _other: object) -> bool:
            hooks.append("ne")
            return False

    class ReplayStore:
        async def put(self, *_args: Any, **_kwargs: Any) -> StoredArtifact:
            raise AssertionError("invalid resolver metadata must fail before provider I/O")

    class Resolver:
        async def resolve_with_connection_id(self, *_args: Any, **_kwargs: Any) -> Any:
            return ReplayStore(), HostileId("store-2")

    upload = ArtifactUploadReplay(
        artifact_key="transcripts/thread/story/attempt-1.txt",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id=None,
        ownership_known=True,
        payload=b"payload",
        content_type="text/plain",
        claim_token="c" * 32,
    )

    with pytest.raises(ValueError, match="resolved artifact connection id"):
        await replay_artifact_upload(upload, cast(Any, Resolver()))

    assert hooks == []


async def test_reconciler_processes_bounded_claims_and_records_only_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = [
        artifact_reference_service.ArtifactUploadClaim("i1", "missing", "c1"),
        artifact_reference_service.ArtifactUploadClaim("i2", "success", "c2"),
        artifact_reference_service.ArtifactUploadClaim("i3", "failure", "c3"),
    ]
    upload = ArtifactUploadReplay(
        artifact_key="success",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread",
        project_id="p1",
        app_id="a1",
        ownership_known=True,
        payload=b"payload",
        content_type="text/plain",
        claim_token="c2",
    )
    replayed: list[str] = []
    failures: list[tuple[str, str, str]] = []
    heartbeats = 0

    async def claim() -> list[Any]:
        return claims

    async def load(value: Any) -> ArtifactUploadReplay | None:
        if value.artifact_key == "missing":
            return None
        if value.artifact_key == "failure":
            raise RuntimeError("provider unavailable")
        return upload

    async def replay(value: ArtifactUploadReplay, _resolver: Any) -> None:
        replayed.append(value.artifact_key)

    async def record_failure(key: str, token: str, exc: Exception) -> None:
        failures.append((key, token, exc.__class__.__name__))

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    monkeypatch.setattr(artifact_reference_service, "_claim_pending_artifact_uploads", claim)
    monkeypatch.setattr(artifact_reference_service, "_load_claimed_artifact_upload", load)
    monkeypatch.setattr(artifact_reference_service, "replay_artifact_upload", replay)
    monkeypatch.setattr(artifact_reference_service, "_record_replay_failure", record_failure)
    monkeypatch.setattr(artifact_reference_service, "get_connection_resolver", object)

    await artifact_reference_service.reconcile_pending_artifact_uploads_once(heartbeat=heartbeat)

    assert replayed == ["success"]
    assert failures == [("failure", "c3", "RuntimeError")]
    assert heartbeats == 4


async def test_reconciler_preserves_cancellation_without_retry_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = artifact_reference_service.ArtifactUploadClaim("i1", "cancelled", "c1")
    telemetry_calls = 0

    async def claims() -> list[Any]:
        return [claim]

    async def cancelled(_claim: Any) -> None:
        raise asyncio.CancelledError

    async def telemetry(*_args: Any) -> None:
        nonlocal telemetry_calls
        telemetry_calls += 1

    monkeypatch.setattr(artifact_reference_service, "_claim_pending_artifact_uploads", claims)
    monkeypatch.setattr(
        artifact_reference_service,
        "_load_claimed_artifact_upload",
        cancelled,
    )
    monkeypatch.setattr(artifact_reference_service, "_record_replay_failure", telemetry)
    monkeypatch.setattr(artifact_reference_service, "get_connection_resolver", object)

    with pytest.raises(asyncio.CancelledError):
        await artifact_reference_service.reconcile_pending_artifact_uploads_once()

    assert telemetry_calls == 0


async def test_claim_pending_uploads_leases_rows_without_loading_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def all(self) -> list[Any]:
            return [SimpleNamespace(id="intent-1", artifact_key="artifact-1")]

    class Session:
        def __init__(self) -> None:
            self.statements: list[Any] = []
            self.committed = False

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def execute(self, statement: Any) -> Any:
            self.statements.append(statement)
            return Result() if len(self.statements) == 1 else SimpleNamespace()

        async def commit(self) -> None:
            self.committed = True

    session = Session()
    monkeypatch.setattr(
        artifact_reference_service,
        "get_sessionmaker",
        lambda: SingleSessionFactory(session),
    )

    claims = await artifact_reference_service._claim_pending_artifact_uploads()

    assert len(claims) == 1
    assert claims[0].id == "intent-1"
    assert claims[0].artifact_key == "artifact-1"
    assert len(claims[0].claim_token) == 32
    assert len(session.statements) == 2
    assert session.committed is True


@pytest.mark.parametrize("present", [True, False])
async def test_load_claimed_upload_materializes_at_most_one_payload(
    monkeypatch: pytest.MonkeyPatch,
    present: bool,
) -> None:
    row = SimpleNamespace(
        artifact_key="artifact-1",
        connection_id="store-1",
        kind="transcript",
        thread_id="thread-1",
        project_id="p1",
        app_id="a1",
        ownership_known=True,
        payload=bytearray(b"payload"),
        content_type="text/plain",
        claim_token="claim-1",
    )

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def scalar(self, _statement: Any) -> Any:
            return row if present else None

    monkeypatch.setattr(
        artifact_reference_service,
        "get_sessionmaker",
        lambda: SingleSessionFactory(Session()),
    )
    claim = artifact_reference_service.ArtifactUploadClaim("intent-1", "artifact-1", "claim-1")

    loaded = await artifact_reference_service._load_claimed_artifact_upload(claim)

    if present:
        assert loaded is not None
        assert loaded.payload == b"payload"
        assert loaded.claim_token == "claim-1"
    else:
        assert loaded is None


@pytest.mark.parametrize("present", [True, False])
async def test_replay_failure_telemetry_is_bounded_to_error_type(
    monkeypatch: pytest.MonkeyPatch,
    present: bool,
) -> None:
    intent = SimpleNamespace(attempt_count=2, last_error=None)

    class Session:
        def __init__(self) -> None:
            self.committed = False

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def scalar(self, _statement: Any) -> Any:
            return intent if present else None

        async def commit(self) -> None:
            self.committed = True

    session = Session()
    monkeypatch.setattr(
        artifact_reference_service,
        "get_sessionmaker",
        lambda: SingleSessionFactory(session),
    )

    await artifact_reference_service._record_replay_failure(
        "artifact-1", "claim-1", ConnectionError("credential-secret-canary")
    )

    if present:
        assert intent.attempt_count == 3
        assert intent.last_error == "ConnectionError"
        assert session.committed is True
    else:
        assert session.committed is False


async def test_artifact_reconciler_loop_heartbeats_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    heartbeats = 0

    async def reconcile(*, heartbeat: Any = None) -> None:
        assert heartbeat is not None
        stop.set()

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    monkeypatch.setattr(
        artifact_reference_service,
        "reconcile_pending_artifact_uploads_once",
        reconcile,
    )

    await artifact_reference_service.run_artifact_upload_reconciler(stop, heartbeat)

    assert heartbeats == 1


async def test_artifact_reconciler_logs_iteration_failure_and_honors_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()

    async def reconcile(*, heartbeat: Any = None) -> None:
        del heartbeat
        raise RuntimeError("temporary database failure")

    async def timeout_once(awaitable: Any, **kwargs: float) -> None:
        assert kwargs["timeout"] == artifact_reference_service.ARTIFACT_UPLOAD_RETRY_INTERVAL_S
        awaitable.close()
        stop.set()
        raise TimeoutError

    monkeypatch.setattr(
        artifact_reference_service,
        "reconcile_pending_artifact_uploads_once",
        reconcile,
    )
    monkeypatch.setattr(artifact_reference_service.asyncio, "wait_for", timeout_once)

    await artifact_reference_service.run_artifact_upload_reconciler(stop)
