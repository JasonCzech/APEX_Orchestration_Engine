"""Required durable index writes for checkpoint-addressed artifacts."""

import asyncio
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.adapters.registry import PortKind
from apex.domain.diagnostics import contains_credential_material, safe_type_name
from apex.persistence.db import dispose_engine_instance_definitively, get_sessionmaker
from apex.persistence.models import ArtifactReference, ArtifactUploadIntent, Connection
from apex.ports.artifact_store import (
    ArtifactStorePort,
    StoredArtifact,
    validate_stored_artifact_ack,
)
from apex.services.connections import ConnectionResolver, close_adapter, get_connection_resolver
from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings

logger = structlog.get_logger(__name__)
MAX_DURABLE_ARTIFACT_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_DURABLE_ARTIFACT_UPLOAD_BACKLOG_BYTES = 256 * 1024 * 1024
ARTIFACT_UPLOAD_RETRY_INTERVAL_S = 30.0
ARTIFACT_UPLOAD_RETRY_BATCH_SIZE = 50
ARTIFACT_UPLOAD_CLAIM_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class ArtifactReferenceInput:
    artifact_key: str
    kind: str


@dataclass(frozen=True)
class ArtifactUploadReservation:
    """Result of committing the durable outbox row before provider IO."""

    durable: bool
    already_finalized: bool
    owned: bool


@dataclass(frozen=True)
class ArtifactUploadReplay:
    artifact_key: str
    connection_id: str
    kind: str
    thread_id: str
    project_id: str | None
    app_id: str | None
    ownership_known: bool
    payload: bytes
    content_type: str
    claim_token: str


@dataclass(frozen=True)
class ArtifactUploadClaim:
    """Small lease descriptor; intentionally never carries the payload blob."""

    id: str
    artifact_key: str
    claim_token: str


class ArtifactUploadInProgressError(RuntimeError):
    """Another worker owns the durable upload lease for this exact payload."""


class ArtifactUploadBacklogFullError(RuntimeError):
    """Durable byte-bearing intents reached the per-store safety bound."""


async def persist_artifact_with_intent(
    store: ArtifactStorePort,
    *,
    artifact_key: str,
    connection_id: str,
    kind: str,
    thread_id: str,
    project_id: str | None,
    app_id: str | None,
    payload: bytes,
    content_type: str,
) -> StoredArtifact:
    """Persist bytes through a DB outbox that is durable before provider IO.

    A failed/lost PUT or reference commit leaves the exact payload in the outbox.
    The background reconciler can therefore replay the same idempotent write and
    atomically replace the intent with its public ownership reference.
    """

    data = bytes(payload)
    reservation = await reserve_artifact_upload(
        artifact_key=artifact_key,
        connection_id=connection_id,
        kind=kind,
        thread_id=thread_id,
        project_id=project_id,
        app_id=app_id,
        payload=data,
        content_type=content_type,
    )
    if reservation.already_finalized:
        return _canonical_stored_artifact(artifact_key, size=len(data))
    if not reservation.owned:
        raise ArtifactUploadInProgressError(
            f"artifact upload {artifact_key!r} is already pending reconciliation"
        )

    stored = await store.put(artifact_key, data, content_type=content_type)
    _verify_stored_artifact(stored, artifact_key, expected_size=len(data))
    if reservation.durable:
        await finalize_artifact_upload(
            artifact_key=artifact_key,
            connection_id=connection_id,
            kind=kind,
            thread_id=thread_id,
            project_id=project_id,
            app_id=app_id,
            payload=data,
            content_type=content_type,
        )
    return _canonical_stored_artifact(artifact_key, size=len(data))


def _verify_stored_artifact(
    stored: StoredArtifact, expected_key: str, *, expected_size: int
) -> None:
    # Provider-returned fields are untrusted and may contain signed material or
    # arbitrarily large text. The shared validator keeps all failures fixed.
    validate_stored_artifact_ack(stored, expected_key, expected_size=expected_size)


def _canonical_stored_artifact(artifact_key: str, *, size: int) -> StoredArtifact:
    """Return a deterministic capability-free reference for checkpoint state."""

    encoded_key = quote(artifact_key, safe="/-._~")
    return StoredArtifact(
        key=artifact_key,
        uri=f"apex-artifact:///{encoded_key}",
        size=size,
    )


def _validate_bounded_text(
    value: str | None,
    *,
    label: str,
    max_chars: int,
    required: bool = True,
) -> None:
    if value is None:
        if required:
            raise ValueError(f"{label} is required")
        return
    if type(value) is not str or not value or len(value) > max_chars or "\x00" in value:
        raise ValueError(f"{label} must contain 1-{max_chars} characters without U+0000")
    if value != value.strip() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise ValueError(f"{label} contains unsafe characters")
    if contains_credential_material(value):
        raise ValueError(f"{label} must not contain credential material")


def _validate_reference_identity(
    *,
    artifact_key: str,
    connection_id: str,
    kind: str,
    thread_id: str,
    project_id: str | None,
    app_id: str | None,
) -> None:
    _validate_bounded_text(artifact_key, label="artifact key", max_chars=1024)
    _validate_bounded_text(connection_id, label="artifact connection id", max_chars=32)
    _validate_bounded_text(kind, label="artifact kind", max_chars=64)
    _validate_bounded_text(thread_id, label="artifact thread id", max_chars=255)
    _validate_bounded_text(
        project_id,
        label="artifact project id",
        max_chars=255,
        required=False,
    )
    _validate_bounded_text(
        app_id,
        label="artifact application id",
        max_chars=255,
        required=False,
    )


async def reserve_artifact_upload(
    *,
    artifact_key: str,
    connection_id: str,
    kind: str,
    thread_id: str,
    project_id: str | None,
    app_id: str | None,
    payload: bytes,
    content_type: str,
) -> ArtifactUploadReservation:
    """Commit an exact upload outbox row, resolving a lost commit acknowledgement."""

    values = _upload_values(
        artifact_key=artifact_key,
        connection_id=connection_id,
        kind=kind,
        thread_id=thread_id,
        project_id=project_id,
        app_id=app_id,
        payload=payload,
        content_type=content_type,
    )
    settings = get_settings()
    if connection_id.startswith("dev-") and not settings.is_locked_down:
        return ArtifactUploadReservation(
            durable=False,
            already_finalized=False,
            owned=True,
        )

    engine, session_factory = _new_session_factory()
    outcome_succeeded = False
    claim_token = uuid4().hex
    try:
        async with session_factory() as session:
            already_finalized, owned = await _stage_upload_intent(
                session,
                values,
                claim_token=claim_token,
            )
            try:
                await session.commit()
            except asyncio.CancelledError:
                # The outbox commit may already be durable, but cancellation is
                # still the caller's outcome. Never continue into a foreground
                # PUT; the reconciler owns any committed intent from here.
                await _rollback_quietly(session)
                raise
            except BaseException:
                await _rollback_quietly(session)
                state = await _resolved_upload_state(session_factory, values)
                if state[0] == "missing":
                    raise
                already_finalized = state[0] == "finalized"
                owned = state[0] == "pending" and state[1] == claim_token
        outcome_succeeded = True
        return ArtifactUploadReservation(
            durable=True,
            already_finalized=already_finalized,
            owned=owned,
        )
    finally:
        await _dispose_engine(engine, operation="reserve", succeeded=outcome_succeeded)


async def finalize_artifact_upload(
    *,
    artifact_key: str,
    connection_id: str,
    kind: str,
    thread_id: str,
    project_id: str | None,
    app_id: str | None,
    payload: bytes,
    content_type: str,
    ownership_known: bool = True,
) -> None:
    """Atomically replace an exact upload intent with its ownership reference."""

    values = _upload_values(
        artifact_key=artifact_key,
        connection_id=connection_id,
        kind=kind,
        thread_id=thread_id,
        project_id=project_id,
        app_id=app_id,
        payload=payload,
        content_type=content_type,
        ownership_known=ownership_known,
    )
    engine, session_factory = _new_session_factory()
    outcome_succeeded = False
    try:
        async with session_factory() as session:
            await _finalize_upload_intent(session, values)
            try:
                await session.commit()
            except asyncio.CancelledError:
                await _rollback_quietly(session)
                raise
            except BaseException:
                await _rollback_quietly(session)
                if (await _resolved_upload_state(session_factory, values))[0] != "finalized":
                    raise
        outcome_succeeded = True
    finally:
        await _dispose_engine(engine, operation="finalize", succeeded=outcome_succeeded)


def _upload_values(
    *,
    artifact_key: str,
    connection_id: str,
    kind: str,
    thread_id: str,
    project_id: str | None,
    app_id: str | None,
    payload: bytes,
    content_type: str,
    ownership_known: bool = True,
) -> dict[str, Any]:
    data = bytes(payload)
    _validate_reference_identity(
        artifact_key=artifact_key,
        connection_id=connection_id,
        kind=kind,
        thread_id=thread_id,
        project_id=project_id,
        app_id=app_id,
    )
    _validate_bounded_text(content_type, label="artifact content type", max_chars=255)
    if not isinstance(ownership_known, bool):
        raise ValueError("artifact ownership provenance must be boolean")
    if len(data) > MAX_DURABLE_ARTIFACT_UPLOAD_BYTES:
        raise ValueError(
            f"artifact upload intent exceeds {MAX_DURABLE_ARTIFACT_UPLOAD_BYTES} bytes"
        )
    return {
        "artifact_key": artifact_key,
        "connection_id": connection_id,
        "kind": kind,
        "thread_id": thread_id,
        "project_id": project_id,
        "app_id": app_id,
        "ownership_known": ownership_known,
        "payload": data,
        "content_type": content_type,
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


async def _stage_upload_intent(
    session: AsyncSession,
    values: dict[str, Any],
    *,
    claim_token: str,
) -> tuple[bool, bool]:
    connection = await session.scalar(
        select(Connection).where(Connection.id == values["connection_id"]).with_for_update()
    )
    _require_artifact_store_project_binding(connection, values["project_id"])

    reference = await session.scalar(
        select(ArtifactReference).where(ArtifactReference.artifact_key == values["artifact_key"])
    )
    if reference is not None:
        if not _reference_matches(reference, values):
            raise RuntimeError("artifact key is already bound to different durable ownership")
        return True, False

    intent = await session.scalar(
        select(ArtifactUploadIntent)
        .where(ArtifactUploadIntent.artifact_key == values["artifact_key"])
        .with_for_update()
    )
    if intent is not None:
        if not _intent_matches(intent, values):
            raise RuntimeError("artifact key has a different pending upload intent")
        return False, False

    # The connection row lock serializes reservations for this store, making
    # the aggregate check authoritative. Preserve enough durable buffering for
    # transient outages without allowing a failed provider to turn Postgres
    # into an unbounded artifact store.
    pending_bytes = int(
        await session.scalar(
            select(func.coalesce(func.sum(func.length(ArtifactUploadIntent.payload)), 0)).where(
                ArtifactUploadIntent.connection_id == values["connection_id"]
            )
        )
        or 0
    )
    if pending_bytes + len(values["payload"]) > MAX_DURABLE_ARTIFACT_UPLOAD_BACKLOG_BYTES:
        raise ArtifactUploadBacklogFullError(
            "artifact upload backlog is full; restore the artifact store and allow "
            "reconciliation before accepting more durable payloads"
        )

    session.add(
        ArtifactUploadIntent(
            id=uuid4().hex,
            claim_token=claim_token,
            claimed_at=datetime.now(UTC),
            **{key: values[key] for key in _INTENT_FIELDS},
        )
    )
    return False, True


async def _finalize_upload_intent(session: AsyncSession, values: dict[str, Any]) -> None:
    # Connection lifecycle mutations lock this row before checking references
    # and intents. Take the same lock first so disable/delete cannot observe the
    # gap between deleting an intent and publishing its replacement reference.
    connection = await session.scalar(
        select(Connection).where(Connection.id == values["connection_id"]).with_for_update()
    )
    _require_artifact_store_project_binding(connection, values["project_id"])

    # Concurrent finalizers then observe the first one's committed reference
    # instead of racing two inserts for the same key.
    intent = await session.scalar(
        select(ArtifactUploadIntent)
        .where(ArtifactUploadIntent.artifact_key == values["artifact_key"])
        .with_for_update()
    )
    reference = await session.scalar(
        select(ArtifactReference).where(ArtifactReference.artifact_key == values["artifact_key"])
    )
    if reference is not None:
        if not _reference_matches(reference, values):
            raise RuntimeError("artifact key is already bound to different durable ownership")
        if intent is not None:
            if not _intent_matches(intent, values):
                raise RuntimeError("artifact key has a different pending upload intent")
            await session.delete(intent)
        return
    if intent is None:
        raise RuntimeError("artifact upload intent disappeared before finalization")
    if not _intent_matches(intent, values):
        raise RuntimeError("artifact key has a different pending upload intent")

    session.add(
        ArtifactReference(
            id=uuid4().hex,
            **{key: values[key] for key in _REFERENCE_FIELDS},
        )
    )
    await session.delete(intent)


_OWNERSHIP_FIELDS = (
    "artifact_key",
    "connection_id",
    "kind",
    "thread_id",
    "project_id",
    "app_id",
    "ownership_known",
)
_CONTENT_IDENTITY_FIELDS = ("content_sha256", "size_bytes", "content_type")
_REFERENCE_FIELDS = _OWNERSHIP_FIELDS + _CONTENT_IDENTITY_FIELDS
_INTENT_FIELDS = _OWNERSHIP_FIELDS + ("payload", "content_type")


def _reference_matches(row: Any, values: dict[str, Any]) -> bool:
    return all(getattr(row, field) == values[field] for field in _REFERENCE_FIELDS)


def _intent_matches(row: ArtifactUploadIntent, values: dict[str, Any]) -> bool:
    return all(getattr(row, field) == values[field] for field in _INTENT_FIELDS)


def _require_artifact_store_project_binding(
    connection: Any,
    project_id: str | None,
) -> None:
    """Require an enabled store whose persisted scope can own this artifact."""

    if connection is None or not connection.enabled or connection.kind != "artifact_store":
        raise RuntimeError("artifact-store connection is missing or disabled")
    connection_project_id = getattr(connection, "project_id", None)
    if connection_project_id is not None and connection_project_id != project_id:
        raise RuntimeError("artifact-store connection is not available for the artifact project")


async def _resolved_upload_state(
    session_factory: async_sessionmaker[AsyncSession], values: dict[str, Any]
) -> tuple[str, str | None]:
    """Return finalized/pending/missing after a fresh authoritative read."""

    async with session_factory() as session:
        reference = await session.scalar(
            select(ArtifactReference).where(
                ArtifactReference.artifact_key == values["artifact_key"]
            )
        )
        if reference is not None:
            if not _reference_matches(reference, values):
                raise RuntimeError("artifact key is already bound to different durable ownership")
            return "finalized", None
        intent = await session.scalar(
            select(ArtifactUploadIntent).where(
                ArtifactUploadIntent.artifact_key == values["artifact_key"]
            )
        )
        if intent is None:
            return "missing", None
        if not _intent_matches(intent, values):
            raise RuntimeError("artifact key has a different pending upload intent")
        return "pending", intent.claim_token


def _new_session_factory() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    database = get_settings().database
    engine = create_async_engine(
        database_asyncpg_uri(database.uri),
        poolclass=NullPool,
        connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _rollback_quietly(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except BaseException:
        pass


async def _dispose_engine(engine: Any, *, operation: str, succeeded: bool) -> None:
    try:
        await dispose_engine_instance_definitively(engine)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "artifact_upload_intents.engine_dispose_failed",
            operation=operation,
            operation_succeeded=succeeded,
            error_type=safe_type_name(exc),
        )


async def record_artifact_reference(
    *,
    artifact_key: str,
    connection_id: str,
    kind: str,
    thread_id: str,
    project_id: str | None,
    app_id: str | None,
) -> None:
    """Compatibility wrapper for callers that persist one artifact at a time."""

    await record_artifact_references(
        [ArtifactReferenceInput(artifact_key=artifact_key, kind=kind)],
        connection_id=connection_id,
        thread_id=thread_id,
        project_id=project_id,
        app_id=app_id,
    )


async def record_artifact_references(
    references: Sequence[ArtifactReferenceInput],
    *,
    connection_id: str,
    thread_id: str,
    project_id: str | None,
    app_id: str | None,
) -> None:
    """Atomically record an exact batch under one artifact-store lifecycle lock."""

    if not references:
        return
    keys = [reference.artifact_key for reference in references]
    if len(set(keys)) != len(keys):
        raise ValueError("artifact reference batch contains duplicate keys")
    for reference in references:
        _validate_reference_identity(
            artifact_key=reference.artifact_key,
            connection_id=connection_id,
            kind=reference.kind,
            thread_id=thread_id,
            project_id=project_id,
            app_id=app_id,
        )

    settings = get_settings()
    if connection_id.startswith("dev-") and not settings.is_locked_down:
        # Static development adapters have no persisted Connection row to protect.
        return
    values = [
        {
            "artifact_key": reference.artifact_key,
            "connection_id": connection_id,
            "kind": reference.kind,
            "thread_id": thread_id,
            "project_id": project_id,
            "app_id": app_id,
            "ownership_known": True,
        }
        for reference in references
    ]
    database = settings.database
    engine = create_async_engine(
        database_asyncpg_uri(database.uri),
        poolclass=NullPool,
        connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
    )
    indexing_succeeded = False
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            connection = await session.scalar(
                select(Connection).where(Connection.id == connection_id).with_for_update()
            )
            _require_artifact_store_project_binding(connection, project_id)
            if session.get_bind().dialect.name == "postgresql":
                statement = pg_insert(ArtifactReference).values(values)
            elif session.get_bind().dialect.name == "sqlite":
                statement = sqlite_insert(ArtifactReference).values(values)
            else:
                raise RuntimeError("artifact-reference insert requires PostgreSQL or SQLite")
            # Artifact affinity is immutable.  An idempotent retry observes the
            # existing exact row; a cross-thread/store collision is rejected by
            # the exact-match check rather than silently reassigning ownership.
            statement = statement.on_conflict_do_nothing(
                index_elements=[ArtifactReference.artifact_key]
            )
            await _commit_references(session, session_factory, statement, values)
            indexing_succeeded = True
    finally:
        try:
            await dispose_engine_instance_definitively(engine)
        except asyncio.CancelledError:
            # Cancellation remains observable to the caller.  Execution callers
            # must not compensate on cancellation because the commit may already
            # be durable (and currently deliberately skip that compensation).
            raise
        except Exception as exc:
            # Pool cleanup cannot change an already definitive indexing outcome.
            # Suppress it after success so callers never delete committed bytes;
            # when indexing itself failed, suppression also preserves that
            # original exception instead of masking it with cleanup noise.
            logger.warning(
                "artifact_references.engine_dispose_failed",
                indexing_succeeded=indexing_succeeded,
                error_type=safe_type_name(exc),
            )


async def _commit_references(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    statement: Any,
    values: Sequence[dict[str, Any]],
) -> None:
    """Commit a batch and resolve a lost acknowledgement before compensation."""

    await session.execute(statement)
    if not await _references_match(session, values):
        await session.rollback()
        raise RuntimeError("artifact key is already bound to different durable ownership")
    try:
        await session.commit()
    except BaseException:
        try:
            await session.rollback()
        except BaseException:
            # A broken connection is expected in the ambiguity case; the fresh
            # session below is the authoritative check.
            pass
        async with session_factory() as resolution_session:
            if await _references_match(resolution_session, values):
                return
        raise


async def _references_match(session: AsyncSession, values: Sequence[dict[str, Any]]) -> bool:
    expected = {str(value["artifact_key"]): value for value in values}
    rows = list(
        await session.scalars(
            select(ArtifactReference).where(ArtifactReference.artifact_key.in_(sorted(expected)))
        )
    )
    if len(rows) != len(expected):
        return False
    return all(
        row.artifact_key in expected
        and all(
            getattr(row, field) == expected[row.artifact_key][field]
            for field in (
                "connection_id",
                "kind",
                "thread_id",
                "project_id",
                "app_id",
                "ownership_known",
            )
        )
        for row in rows
    )


async def reconcile_pending_artifact_uploads_once(
    *, heartbeat: Callable[[], None] | None = None
) -> None:
    """Replay one bounded outbox batch; concurrent replicas are idempotent."""

    if heartbeat is not None:
        heartbeat()
    claims = await _claim_pending_artifact_uploads()
    resolver = get_connection_resolver()
    for claim in claims:
        if heartbeat is not None:
            heartbeat()
        # Drop the prior iteration's bytes before loading the next row.
        upload: ArtifactUploadReplay | None = None
        try:
            upload = await _load_claimed_artifact_upload(claim)
            if upload is None:
                continue
            await replay_artifact_upload(upload, resolver)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "artifact_upload_intents.replay_failed",
                artifact_key=claim.artifact_key,
                error_type=safe_type_name(exc),
            )
            await _record_replay_failure(claim.artifact_key, claim.claim_token, exc)


async def _claim_pending_artifact_uploads() -> list[ArtifactUploadClaim]:
    """Lease stale rows without materializing their potentially large payloads."""

    sessionmaker = get_sessionmaker()
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        rows = list((await session.execute(_pending_upload_claim_statement(now))).all())
        claims: list[ArtifactUploadClaim] = []
        for row in rows:
            claim_token = uuid4().hex
            await session.execute(
                update(ArtifactUploadIntent)
                .where(ArtifactUploadIntent.id == row.id)
                .values(claim_token=claim_token, claimed_at=now)
            )
            claims.append(
                ArtifactUploadClaim(
                    id=str(row.id),
                    artifact_key=str(row.artifact_key),
                    claim_token=claim_token,
                )
            )
        await session.commit()
    return claims


async def _load_claimed_artifact_upload(
    claim: ArtifactUploadClaim,
) -> ArtifactUploadReplay | None:
    """Materialize one leased payload, bounding reconciler memory to one intent."""

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.scalar(
            select(ArtifactUploadIntent).where(
                ArtifactUploadIntent.id == claim.id,
                ArtifactUploadIntent.claim_token == claim.claim_token,
            )
        )
        if row is None:
            return None
        return ArtifactUploadReplay(
            artifact_key=row.artifact_key,
            connection_id=row.connection_id,
            kind=row.kind,
            thread_id=row.thread_id,
            project_id=row.project_id,
            app_id=row.app_id,
            ownership_known=row.ownership_known,
            payload=bytes(row.payload),
            content_type=row.content_type,
            claim_token=row.claim_token,
        )


def _pending_upload_claim_statement(now: datetime) -> Any:
    return (
        select(ArtifactUploadIntent.id, ArtifactUploadIntent.artifact_key)
        .where(ArtifactUploadIntent.claimed_at <= now - ARTIFACT_UPLOAD_CLAIM_TTL)
        .order_by(ArtifactUploadIntent.updated_at, ArtifactUploadIntent.id)
        .limit(ARTIFACT_UPLOAD_RETRY_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )


async def replay_artifact_upload(
    upload: ArtifactUploadReplay,
    resolver: ConnectionResolver,
) -> None:
    """Idempotently PUT the durable payload and publish its ownership row."""

    # Legacy/corrupted outbox rows bypass the foreground reservation boundary.
    # Revalidate every provider-directed field before resolving or writing.
    _upload_values(
        artifact_key=upload.artifact_key,
        connection_id=upload.connection_id,
        kind=upload.kind,
        thread_id=upload.thread_id,
        project_id=upload.project_id,
        app_id=upload.app_id,
        payload=upload.payload,
        content_type=upload.content_type,
        ownership_known=upload.ownership_known,
    )
    store, resolved_connection_id = await resolver.resolve_with_connection_id(
        PortKind.ARTIFACT_STORE,
        connection_id=upload.connection_id,
        project_id=upload.project_id,
    )
    try:
        _validate_bounded_text(
            resolved_connection_id,
            label="resolved artifact connection id",
            max_chars=32,
        )
        if resolved_connection_id != upload.connection_id:
            raise RuntimeError(
                "artifact-store resolver did not honor the upload connection affinity"
            )
        stored = await store.put(
            upload.artifact_key,
            upload.payload,
            content_type=upload.content_type,
        )
        _verify_stored_artifact(
            stored,
            upload.artifact_key,
            expected_size=len(upload.payload),
        )
        await finalize_artifact_upload(
            artifact_key=upload.artifact_key,
            connection_id=upload.connection_id,
            kind=upload.kind,
            thread_id=upload.thread_id,
            project_id=upload.project_id,
            app_id=upload.app_id,
            payload=upload.payload,
            content_type=upload.content_type,
            ownership_known=upload.ownership_known,
        )
    finally:
        await close_adapter(store)


async def _record_replay_failure(
    artifact_key: str,
    claim_token: str,
    exc: Exception,
) -> None:
    """Best-effort retry telemetry without persisting provider error details."""

    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            intent = await session.scalar(
                select(ArtifactUploadIntent).where(
                    ArtifactUploadIntent.artifact_key == artifact_key,
                    ArtifactUploadIntent.claim_token == claim_token,
                )
            )
            if intent is None:
                return
            intent.attempt_count += 1
            intent.last_error = safe_type_name(exc)
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception as telemetry_exc:
        logger.warning(
            "artifact_upload_intents.retry_telemetry_failed",
            artifact_key=artifact_key,
            error_type=safe_type_name(telemetry_exc),
        )


async def run_artifact_upload_reconciler(
    stop: asyncio.Event,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    """Continuously drain durable artifact uploads interrupted between stores."""

    while not stop.is_set():
        try:
            await reconcile_pending_artifact_uploads_once(heartbeat=heartbeat)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "artifact_upload_intents.reconciler_failed",
                error_type=safe_type_name(exc),
            )
        if heartbeat is not None:
            heartbeat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=ARTIFACT_UPLOAD_RETRY_INTERVAL_S)
        except TimeoutError:
            pass
