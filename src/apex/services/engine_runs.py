"""Durable reservations and lifecycle projection for external engine runs.

Called from the execution phase's engine nodes. Checkpointed graph state remains the
source of truth for ordinary lifecycle reads, whose projection failures are logged.
Writes marked ``required`` are provider-I/O/ownership barriers and fail closed.
Upsert key: (thread_id, attempt).
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import and_, case, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.domain.diagnostics import bounded_diagnostic, contains_credential_material
from apex.domain.integrations import TestResultSummary
from apex.domain.pipeline import EngineHandle
from apex.persistence.db import dispose_engine_instance_definitively
from apex.persistence.models import Connection, EngineRun
from apex.ports.artifact_store import engine_artifact_namespace
from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings

logger = structlog.get_logger(__name__)

_TERMINAL = {"completed", "failed", "aborted"}
_STARTABLE = {"provisioning", "ready"}
_NONTERMINAL_ORDER = {
    "provisioning": 0,
    "ready": 1,
    "running": 2,
    "stopping": 3,
    "collecting": 4,
}
COMPLETION_COLLECTION_TEARDOWN = "collection_teardown_v1"
COMPLETION_CLEANUP_TEARDOWN = "cleanup_teardown_v1"
_COMPLETION_KINDS = frozenset({COMPLETION_COLLECTION_TEARDOWN, COMPLETION_CLEANUP_TEARDOWN})


class EngineRunReservationRejectedError(RuntimeError):
    """A required lifecycle write lost the monotonic projection race."""


class EngineRunProjectionError(RuntimeError):
    """A durable engine-run projection operation failed unexpectedly."""


def _stable_projection_failure(exc: Exception, *, operation: str) -> Exception:
    """Copy safe domain/input failures and hide backend exception objects."""

    if isinstance(exc, EngineRunReservationRejectedError):
        return EngineRunReservationRejectedError(bounded_diagnostic(exc))
    if isinstance(exc, ValueError):
        return ValueError(bounded_diagnostic(exc))
    return EngineRunProjectionError(f"engine-run {operation} failed")


def _validated_projection_text(
    value: Any,
    *,
    label: str,
    max_chars: int,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if (
        type(value) is not str
        or not value
        or len(value) > max_chars
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or contains_credential_material(value)
    ):
        raise ValueError(f"engine-run {label} is invalid or contains credential material")
    return value


def _validated_projection_handle(
    handle: Any,
    *,
    engine: str,
    allow_empty: bool,
) -> dict[str, Any]:
    if type(handle) is not dict:
        raise ValueError("engine-run handle is invalid or contains credential material")
    if not handle and allow_empty:
        return {}
    if contains_credential_material(handle, max_nodes=64, max_total_chars=32_768):
        raise ValueError("engine-run handle is invalid or contains credential material")
    validated: EngineHandle | None = None
    try:
        validated = EngineHandle.model_validate(handle, strict=True)
    except Exception:
        pass
    if validated is None:
        raise ValueError("engine-run handle is invalid or contains credential material")
    if validated.engine != engine:
        raise ValueError("engine-run handle provider does not match the projection")
    return validated.model_dump(mode="json")


def _validated_projection_summary(
    summary: Any,
    *,
    engine: str,
) -> dict[str, Any] | None:
    if summary is None:
        return None
    if type(summary) is not dict or contains_credential_material(
        summary,
        max_nodes=256,
        max_total_chars=300_000,
    ):
        raise ValueError("engine-run summary is invalid or contains credential material")
    validated: TestResultSummary | None = None
    try:
        validated = TestResultSummary.model_validate(summary, strict=True)
    except Exception:
        pass
    if validated is None:
        raise ValueError("engine-run summary is invalid or contains credential material")
    if validated.engine != engine:
        raise ValueError("engine-run summary provider does not match the projection")
    return validated.model_dump(mode="json")


def _validate_engine_run_projection_input(
    *,
    thread_id: Any,
    attempt: Any,
    engine: Any,
    handle: Any,
    status: Any,
    project_id: Any,
    app_id: Any,
    external_run_id: Any = None,
    artifact_namespace: Any = None,
    artifact_connection_id: Any = None,
    connection_id: Any = None,
    summary: Any = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Validate a complete projection before any durable row or provider lease."""

    _validated_projection_text(thread_id, label="thread id", max_chars=64)
    safe_engine = _validated_projection_text(engine, label="engine", max_chars=64)
    assert safe_engine is not None
    if type(attempt) is not int or not 1 <= attempt <= 1_000_000:
        raise ValueError("engine-run attempt must be between 1 and 1000000")
    if type(status) is not str or status not in {*_NONTERMINAL_ORDER, *_TERMINAL}:
        raise ValueError("engine-run status is invalid")
    for value, label, maximum in (
        (project_id, "project id", 255),
        (app_id, "application id", 255),
        (external_run_id, "external run id", 255),
        (artifact_namespace, "artifact namespace", 512),
        (artifact_connection_id, "artifact connection id", 255),
        (connection_id, "execution connection id", 255),
    ):
        _validated_projection_text(
            value,
            label=label,
            max_chars=maximum,
            optional=True,
        )
    canonical_handle = _validated_projection_handle(
        handle,
        engine=safe_engine,
        allow_empty=status == "failed",
    )
    if canonical_handle:
        handle_external_run_id = canonical_handle.get("external_run_id")
        if external_run_id is not None and handle_external_run_id != external_run_id:
            raise ValueError("engine-run external id does not match the durable handle")
        handle_connection_id = canonical_handle.get("connection_id")
        if connection_id is not None and handle_connection_id != connection_id:
            raise ValueError("engine-run connection does not match the durable handle")
        expected_namespace = engine_artifact_namespace(canonical_handle["idempotency_key"])
        if artifact_namespace is not None and artifact_namespace.rstrip("/") != expected_namespace:
            raise ValueError("engine-run artifact namespace does not match the durable handle")
    canonical_summary = _validated_projection_summary(summary, engine=safe_engine)
    return canonical_handle, canonical_summary


def _verify_execution_connection_reservation(
    connection: Connection | None,
    *,
    engine: str,
    project_id: str | None,
) -> Connection:
    """Require the reserved engine adapter to be authorized for this run scope."""

    if connection is None or not connection.enabled:
        raise EngineRunReservationRejectedError(
            "execution connection reservation is missing or disabled"
        )
    if connection.kind != "execution_engine" or connection.provider != engine:
        raise EngineRunReservationRejectedError(
            "execution connection reservation has a different adapter identity"
        )
    if connection.project_id is not None and connection.project_id != project_id:
        raise EngineRunReservationRejectedError(
            "execution connection reservation has different project ownership"
        )
    return connection


def _verify_artifact_connection_reservation(
    connection: Connection | None,
    *,
    project_id: str | None,
) -> Connection:
    """Require the reserved artifact store to be authorized for this run scope."""

    if connection is None or not connection.enabled or connection.kind != "artifact_store":
        raise EngineRunReservationRejectedError(
            "artifact-store connection reservation is missing, disabled, or has the wrong kind"
        )
    if connection.project_id is not None and connection.project_id != project_id:
        raise EngineRunReservationRejectedError(
            "artifact-store connection reservation has different project ownership"
        )
    return connection


def _authoritative_scope(
    project_id: str | None,
    app_id: str | None,
    *,
    required: bool,
    operation: str,
) -> bool:
    """Return true only for an exact provider-I/O ownership assertion."""

    complete = _is_exact_scope_component(project_id) and _is_exact_scope_component(app_id)
    if required and not complete:
        raise EngineRunReservationRejectedError(
            f"engine-run {operation} requires exact project and application ownership"
        )
    return required and complete


def _is_exact_scope_component(value: Any) -> bool:
    """Recognize a canonical bounded scope without invoking subclass hooks."""

    return (
        type(value) is str
        and 1 <= len(value) <= 255
        and value == value.strip()
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        and not contains_credential_material(value)
    )


def _verify_reservation_connection_generation(
    row: EngineRun,
    *,
    connection_id: str | None,
    connection_version: datetime | None,
    operation: str,
) -> None:
    """Bind a recovered durable attempt to the exact adapter generation.

    A connection row can be updated in place while retaining its id. Recovering a
    provider handle written for the previous generation against the replacement
    endpoint would graft one provider's external identity onto another provider
    instance. Both reservation recovery paths call this before provider I/O.
    """

    if connection_id is None:
        return
    if row.connection_id != connection_id:
        raise EngineRunReservationRejectedError(
            f"engine-run {operation} reservation has different connection affinity"
        )
    if connection_version is None or row.execution_connection_version != connection_version:
        raise EngineRunReservationRejectedError(
            f"engine-run {operation} reservation has different connection generation"
        )


def _verify_reservation_handle_identity(
    row_handle: dict[str, Any],
    *,
    engine: str,
    idempotency_key: Any,
    connection_id: str | None,
    operation: str,
) -> None:
    """Reject a recovered provider handle whose immutable identity was replaced."""

    if row_handle.get("engine") != engine:
        raise EngineRunReservationRejectedError(
            f"engine-run {operation} reservation handle has a different provider"
        )
    if row_handle.get("idempotency_key") != idempotency_key:
        raise EngineRunReservationRejectedError(
            f"engine-run {operation} reservation has a different idempotency key"
        )
    if connection_id is not None and row_handle.get("connection_id") != connection_id:
        raise EngineRunReservationRejectedError(
            f"engine-run {operation} reservation handle has different connection affinity"
        )


def _normalize_development_connection_ids(
    handle: dict[str, Any],
    connection_id: str | None,
    connection_version: datetime | None,
    artifact_connection_id: str | None,
    *,
    is_locked_down: bool,
) -> tuple[bool, str | None, datetime | None, str | None]:
    """Remove synthetic dev ids without hiding a durable half of a hybrid run.

    Returns ``(skip_projection, connection_id, connection_version,
    artifact_connection_id)``. A fully static execution has nothing durable to
    project; a real engine paired with the static artifact store must still settle
    its existing engine-run lease.
    """

    if is_locked_down:
        return False, connection_id, connection_version, artifact_connection_id

    handle_connection_id = handle.get("connection_id")
    execution_connection_id = connection_id or (
        handle_connection_id if isinstance(handle_connection_id, str) else None
    )
    execution_is_static = bool(
        execution_connection_id and execution_connection_id.startswith("dev-")
    )
    if connection_id is not None and connection_id.startswith("dev-"):
        connection_id = None
        connection_version = None
    if artifact_connection_id is not None and artifact_connection_id.startswith("dev-"):
        artifact_connection_id = None

    return (
        execution_is_static and artifact_connection_id is None,
        connection_id,
        connection_version,
        artifact_connection_id,
    )


def _normalize_scope_provenance(values: dict[str, Any]) -> dict[str, Any]:
    """Quarantine statement inputs that do not prove one exact app scope."""

    normalized = dict(values)
    authoritative = (
        normalized.get("ownership_known") is True
        and normalized.get("scope_ownership_known") is True
        and _is_exact_scope_component(normalized.get("project_id"))
        and _is_exact_scope_component(normalized.get("app_id"))
    )
    if not authoritative:
        normalized["ownership_known"] = False
        normalized["scope_ownership_known"] = False
    return normalized


def _upsert_statement(values: dict[str, Any], dialect: str) -> Any:
    """Build a replay-safe, monotonic upsert for runtime/test databases."""
    values = _normalize_scope_provenance(values)
    if dialect == "postgresql":
        statement = pg_insert(EngineRun).values(**values)
    elif dialect == "sqlite":
        statement = sqlite_insert(EngineRun).values(**values)
    else:
        raise RuntimeError(f"engine-run upsert is unsupported for database dialect {dialect!r}")
    incoming_update_cols = {
        key: value for key, value in values.items() if key not in {"thread_id", "attempt"}
    }
    incoming_status = str(values["status"])
    mutable_existing = EngineRun.status.not_in(_TERMINAL)
    # A terminal projection is immutable. Replays may enrich a nonterminal row or
    # settle it once, but a stale lifecycle callback cannot reopen or replace the
    # result of a completed/failed/aborted attempt.
    transition_allowed = (
        mutable_existing
        if incoming_status not in _TERMINAL
        else mutable_existing | (EngineRun.status == incoming_status)
    )
    # Provider identity is immutable for one durable (thread, attempt). Without
    # this predicate, a stale callback using the same ownership scope could
    # replace the row's engine and handle even though provision/start recovery
    # deliberately treats that identity as a provider-I/O lease.
    transition_allowed = and_(transition_allowed, EngineRun.engine == values["engine"])
    # A lifecycle callback may enrich a quarantined legacy row, but it must never
    # rebind a known (thread, attempt) projection to another project/application.
    # The conflict key is global, so status monotonicity alone is not an ownership
    # boundary under replay or a colliding caller-controlled thread id.
    if "project_id" in values:
        transition_allowed = and_(
            transition_allowed,
            or_(
                EngineRun.project_id == values["project_id"],
                and_(
                    EngineRun.scope_ownership_known.is_(False),
                    EngineRun.project_id.is_(None),
                ),
            ),
        )
    if "app_id" in values:
        transition_allowed = and_(
            transition_allowed,
            or_(
                EngineRun.app_id == values["app_id"],
                and_(
                    EngineRun.scope_ownership_known.is_(False),
                    EngineRun.app_id.is_(None),
                ),
            ),
        )
    update_cols: dict[str, Any]
    if incoming_status in _TERMINAL:
        # A lost commit acknowledgement must report one affected row on replay,
        # but the first terminal writer owns the immutable payload. Preserve every
        # existing column when the row is already at this status so a stale callback
        # cannot erase its handle/summary/ownership while masquerading as a replay.
        update_cols = {
            key: case(
                (EngineRun.status == incoming_status, getattr(EngineRun, key)),
                else_=getattr(statement.excluded, key),
            )
            for key in incoming_update_cols
        }
    elif incoming_status in _NONTERMINAL_ORDER:
        later_statuses = tuple(
            status
            for status, order in _NONTERMINAL_ORDER.items()
            if order > _NONTERMINAL_ORDER[incoming_status]
        )
        if later_statuses:
            # Concurrent/replayed workers may finish an earlier side effect after
            # another worker already advanced the durable lifecycle. Acknowledge
            # the stale write without downgrading status or erasing the richer
            # handle/ownership payload owned by the later stage.
            update_cols = {
                key: case(
                    (EngineRun.status.in_(later_statuses), getattr(EngineRun, key)),
                    else_=getattr(statement.excluded, key),
                )
                for key in incoming_update_cols
            }
        else:
            update_cols = incoming_update_cols
    else:
        update_cols = incoming_update_cols
    if values.get("scope_ownership_known") is not True:
        # Best-effort/direct/legacy callbacks may create quarantined diagnostic
        # rows, but cannot promote or poison an existing attempt's ownership.
        update_cols["ownership_known"] = EngineRun.ownership_known
        update_cols["scope_ownership_known"] = EngineRun.scope_ownership_known
        update_cols["project_id"] = EngineRun.project_id
        update_cols["app_id"] = EngineRun.app_id
    return statement.on_conflict_do_update(
        index_elements=[EngineRun.thread_id, EngineRun.attempt],
        set_=update_cols,
        where=transition_allowed,
    )


def _insert_reservation_statement(values: dict[str, Any], dialect: str) -> Any:
    """Insert the first provider lease without mutating an existing richer row."""

    values = _normalize_scope_provenance(values)

    if dialect == "postgresql":
        statement = pg_insert(EngineRun).values(**values)
    elif dialect == "sqlite":
        statement = sqlite_insert(EngineRun).values(**values)
    else:
        raise RuntimeError(f"engine-run insert is unsupported for database dialect {dialect!r}")
    return statement.on_conflict_do_nothing(index_elements=[EngineRun.thread_id, EngineRun.attempt])


def _bind_locked_run_ownership(
    row: EngineRun,
    *,
    project_id: str | None,
    app_id: str | None,
    operation: str,
) -> None:
    """Verify immutable ownership, narrowly repairing a quarantined legacy row."""

    _authoritative_scope(project_id, app_id, required=True, operation=operation)
    if row.project_id != project_id:
        if not row.scope_ownership_known and row.project_id is None and project_id is not None:
            row.project_id = project_id
        else:
            raise EngineRunReservationRejectedError(
                f"engine-run {operation} reservation has different project ownership"
            )
    if row.app_id != app_id:
        if not row.scope_ownership_known and row.app_id is None and app_id is not None:
            row.app_id = app_id
        else:
            raise EngineRunReservationRejectedError(
                f"engine-run {operation} reservation has different application ownership"
            )
    if not row.scope_ownership_known:
        if app_id is None:
            raise EngineRunReservationRejectedError(
                f"engine-run {operation} reservation has ambiguous application ownership"
            )
        # A non-null app id supplied by the environment-authorized execution path
        # is the only safe way to promote a legacy project-level-looking row.
        row.ownership_known = True
        row.scope_ownership_known = True


async def record_engine_run(
    thread_id: str,
    attempt: int,
    engine: str,
    handle: dict[str, Any],
    status: str,
    *,
    project_id: str | None = None,
    app_id: str | None = None,
    external_run_id: str | None = None,
    artifact_namespace: str | None = None,
    artifact_connection_id: str | None = None,
    artifact_connection_version: datetime | None = None,
    connection_id: str | None = None,
    connection_version: datetime | None = None,
    summary: dict[str, Any] | None = None,
    completion_kind: str | None = None,
    required: bool = False,
) -> None:
    """Upsert one engine-run row; terminal statuses stamp ended_at.

    Ordinary lifecycle projection remains best-effort. Provider reservations and
    artifact ownership writes pass ``required=True`` so graph retries cannot perform
    external I/O or checkpoint inaccessible objects without a durable projection.
    """
    failure: Exception | None = None
    diagnostic: str | None = None
    try:
        handle, summary = _validate_engine_run_projection_input(
            thread_id=thread_id,
            attempt=attempt,
            engine=engine,
            handle=handle,
            status=status,
            project_id=project_id,
            app_id=app_id,
            external_run_id=external_run_id,
            artifact_namespace=artifact_namespace,
            artifact_connection_id=artifact_connection_id,
            connection_id=connection_id,
            summary=summary,
        )
        settings = get_settings()
        (
            skip_projection,
            connection_id,
            connection_version,
            artifact_connection_id,
        ) = _normalize_development_connection_ids(
            handle,
            connection_id,
            connection_version,
            artifact_connection_id,
            is_locked_down=settings.is_locked_down,
        )
        if skip_projection:
            # Fully static development adapters intentionally work without PostgreSQL.
            return
        scope_ownership_known = _authoritative_scope(
            project_id,
            app_id,
            required=required,
            operation="record",
        )
        if artifact_connection_id is None:
            artifact_connection_version = None
        if required and connection_id is not None and connection_version is None:
            raise EngineRunReservationRejectedError(
                f"execution connection {connection_id!r} is missing its reservation version"
            )
        if completion_kind is not None:
            if completion_kind not in _COMPLETION_KINDS or status not in _TERMINAL:
                raise ValueError("engine completion witness is invalid")
            if connection_id is not None and connection_version is None:
                raise EngineRunReservationRejectedError(
                    "engine completion witness has no execution generation"
                )
        # Throwaway engine per call: graph nodes run on worker threads with
        # short-lived event loops, so pooled connections must not outlive them.
        database = settings.database
        engine_db = create_async_engine(
            database_asyncpg_uri(database.uri),
            poolclass=NullPool,
            connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
        )
        try:
            session_factory = async_sessionmaker(engine_db, expire_on_commit=False)
            async with session_factory() as session:
                if required and connection_id is not None and connection_version is not None:
                    connection = await session.scalar(
                        select(Connection).where(Connection.id == connection_id).with_for_update()
                    )
                    connection = _verify_execution_connection_reservation(
                        connection,
                        engine=engine,
                        project_id=project_id,
                    )
                    if connection.runtime_version != connection_version:
                        raise EngineRunReservationRejectedError(
                            f"execution connection {connection_id!r} changed during reservation"
                        )
                if required and artifact_connection_id is not None:
                    artifact_connection = await session.scalar(
                        select(Connection)
                        .where(Connection.id == artifact_connection_id)
                        .with_for_update()
                    )
                    artifact_connection = _verify_artifact_connection_reservation(
                        artifact_connection,
                        project_id=project_id,
                    )
                    if artifact_connection_version is None:
                        raise EngineRunReservationRejectedError(
                            "artifact-store connection is missing its reservation version"
                        )
                    if artifact_connection.runtime_version != artifact_connection_version:
                        raise EngineRunReservationRejectedError(
                            "artifact-store connection changed during reservation"
                        )
                values: dict[str, Any] = {
                    "thread_id": thread_id,
                    "attempt": attempt,
                    "project_id": project_id,
                    "app_id": app_id,
                    "engine": engine,
                    "handle": handle,
                    "status": status,
                    "ownership_known": scope_ownership_known,
                    "scope_ownership_known": scope_ownership_known,
                }
                if external_run_id is not None:
                    values["external_run_id"] = external_run_id
                handle_idempotency_key = handle.get("idempotency_key")
                if artifact_namespace is None and handle_idempotency_key:
                    artifact_namespace = engine_artifact_namespace(str(handle_idempotency_key))
                if artifact_namespace is not None:
                    values["artifact_namespace"] = artifact_namespace.rstrip("/")
                if artifact_connection_id is not None:
                    values["artifact_connection_id"] = artifact_connection_id
                if connection_id is not None and connection_version is not None:
                    values["connection_id"] = connection_id
                    values["execution_connection_version"] = connection_version
                if artifact_connection_version is not None:
                    values["artifact_connection_version"] = artifact_connection_version
                if summary is not None:
                    values["summary"] = summary
                if completion_kind is not None:
                    values["completion_kind"] = completion_kind
                if status in _TERMINAL:
                    values["ended_at"] = datetime.now(UTC)
                    values["connection_id"] = None
                else:
                    values["ended_at"] = None
                stmt = _upsert_statement(values, session.get_bind().dialect.name)
                result = await session.execute(stmt)
                if required and getattr(result, "rowcount", None) != 1:
                    # A terminal row rejects nonterminal re-entry in the upsert's
                    # WHERE clause. Treat that zero-row outcome as a failed durable
                    # reservation; provider I/O must never proceed unprojected.
                    await session.rollback()
                    raise EngineRunReservationRejectedError(
                        "required engine-run reservation was rejected by the durable projection"
                    )
                await session.commit()
        finally:
            await dispose_engine_instance_definitively(engine_db)
    except Exception as exc:  # noqa: BLE001 — projection writes never fail a run
        diagnostic = bounded_diagnostic(exc)
        failure = _stable_projection_failure(exc, operation="record")
    if failure is not None:
        logger.warning(
            "engine_runs.record_failed",
            thread_id=bounded_diagnostic(thread_id, max_chars=64),
            error=diagnostic,
        )
        if required:
            raise failure


async def prepare_engine_start(
    thread_id: str,
    attempt: int,
    engine: str,
    handle: dict[str, Any],
    *,
    project_id: str | None = None,
    app_id: str | None = None,
    connection_id: str | None = None,
    connection_version: datetime | None = None,
) -> dict[str, Any] | None:
    """Lock and verify a start reservation without weakening its durable handle.

    ``None`` means the provider start is still required. A returned handle means
    the RUNNING projection committed before its graph checkpoint; callers recover
    that provider-owned output and skip reissuing start. Terminal or inconsistent
    rows fail closed before provider I/O.
    """

    failure: Exception | None = None
    diagnostic: str | None = None
    try:
        handle, _summary = _validate_engine_run_projection_input(
            thread_id=thread_id,
            attempt=attempt,
            engine=engine,
            handle=handle,
            status="ready",
            project_id=project_id,
            app_id=app_id,
            connection_id=connection_id,
        )
        settings = get_settings()
        skip_projection, connection_id, connection_version, _artifact_connection_id = (
            _normalize_development_connection_ids(
                handle,
                connection_id,
                connection_version,
                None,
                is_locked_down=settings.is_locked_down,
            )
        )
        if skip_projection:
            return None
        _authoritative_scope(project_id, app_id, required=True, operation="start")
        if connection_id is not None and connection_version is None:
            raise EngineRunReservationRejectedError(
                f"execution connection {connection_id!r} is missing its reservation version"
            )
        database = settings.database
        engine_db = create_async_engine(
            database_asyncpg_uri(database.uri),
            poolclass=NullPool,
            connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
        )
        try:
            session_factory = async_sessionmaker(engine_db, expire_on_commit=False)
            async with session_factory() as session:
                if connection_id is not None and connection_version is not None:
                    connection = await session.scalar(
                        select(Connection).where(Connection.id == connection_id).with_for_update()
                    )
                    connection = _verify_execution_connection_reservation(
                        connection,
                        engine=engine,
                        project_id=project_id,
                    )
                    if connection.runtime_version != connection_version:
                        raise EngineRunReservationRejectedError(
                            f"execution connection {connection_id!r} changed during reservation"
                        )
                row = await session.scalar(
                    select(EngineRun)
                    .where(
                        EngineRun.thread_id == thread_id,
                        EngineRun.attempt == attempt,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise EngineRunReservationRejectedError(
                        "required engine-run start reservation is missing"
                    )
                if row.engine != engine:
                    raise EngineRunReservationRejectedError(
                        "engine-run start reservation has a different provider"
                    )
                _bind_locked_run_ownership(
                    row,
                    project_id=project_id,
                    app_id=app_id,
                    operation="start",
                )
                _verify_reservation_connection_generation(
                    row,
                    connection_id=connection_id,
                    connection_version=connection_version,
                    operation="start",
                )
                row_handle: dict[str, Any] | None = None
                invalid_handle = False
                try:
                    row_handle = _validated_projection_handle(
                        row.handle,
                        engine=engine,
                        allow_empty=False,
                    )
                except ValueError:
                    invalid_handle = True
                if invalid_handle or row_handle is None:
                    raise EngineRunReservationRejectedError(
                        "engine-run start reservation has an invalid durable handle"
                    )
                _verify_reservation_handle_identity(
                    row_handle,
                    engine=engine,
                    idempotency_key=handle.get("idempotency_key"),
                    connection_id=connection_id,
                    operation="start",
                )
                if row.status in _TERMINAL:
                    raise EngineRunReservationRejectedError(
                        "engine-run start reservation is already terminal"
                    )
                if row.status == "running":
                    recovered = dict(row_handle)
                elif row.status in _STARTABLE:
                    recovered = None
                else:
                    raise EngineRunReservationRejectedError(
                        f"engine-run status {row.status!r} is not startable"
                    )
                await session.commit()
                return recovered
        finally:
            await dispose_engine_instance_definitively(engine_db)
    except Exception as exc:  # noqa: BLE001 — provider I/O must fail closed
        diagnostic = bounded_diagnostic(exc)
        failure = _stable_projection_failure(exc, operation="start reservation")
    if failure is not None:
        logger.warning(
            "engine_runs.start_reservation_failed",
            thread_id=bounded_diagnostic(thread_id, max_chars=64),
            error=diagnostic,
        )
        raise failure


async def prepare_engine_provision(
    thread_id: str,
    attempt: int,
    engine: str,
    handle: dict[str, Any],
    *,
    project_id: str | None = None,
    app_id: str | None = None,
    artifact_namespace: str | None = None,
    connection_id: str | None = None,
    connection_version: datetime | None = None,
) -> dict[str, Any] | None:
    """Insert-or-lock the provider lease without erasing a recovered handle.

    ``None`` authorizes same-key validation/provisioning. A returned handle was
    already durably projected by an earlier worker and must be checkpointed without
    another provider call. Existing terminal or inconsistent attempts fail closed.
    """

    failure: Exception | None = None
    diagnostic: str | None = None
    try:
        handle, _summary = _validate_engine_run_projection_input(
            thread_id=thread_id,
            attempt=attempt,
            engine=engine,
            handle=handle,
            status="provisioning",
            project_id=project_id,
            app_id=app_id,
            artifact_namespace=artifact_namespace,
            connection_id=connection_id,
        )
        settings = get_settings()
        skip_projection, connection_id, connection_version, _artifact_connection_id = (
            _normalize_development_connection_ids(
                handle,
                connection_id,
                connection_version,
                None,
                is_locked_down=settings.is_locked_down,
            )
        )
        if skip_projection:
            return None
        _authoritative_scope(project_id, app_id, required=True, operation="provision")
        if connection_id is not None and connection_version is None:
            raise EngineRunReservationRejectedError(
                f"execution connection {connection_id!r} is missing its reservation version"
            )
        database = settings.database
        engine_db = create_async_engine(
            database_asyncpg_uri(database.uri),
            poolclass=NullPool,
            connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
        )
        try:
            session_factory = async_sessionmaker(engine_db, expire_on_commit=False)
            async with session_factory() as session:
                if connection_id is not None and connection_version is not None:
                    connection = await session.scalar(
                        select(Connection).where(Connection.id == connection_id).with_for_update()
                    )
                    connection = _verify_execution_connection_reservation(
                        connection,
                        engine=engine,
                        project_id=project_id,
                    )
                    if connection.runtime_version != connection_version:
                        raise EngineRunReservationRejectedError(
                            f"execution connection {connection_id!r} changed during reservation"
                        )
                values: dict[str, Any] = {
                    "thread_id": thread_id,
                    "attempt": attempt,
                    "project_id": project_id,
                    "app_id": app_id,
                    "engine": engine,
                    "handle": handle,
                    "status": "provisioning",
                    "ownership_known": True,
                    "scope_ownership_known": True,
                }
                if artifact_namespace is not None:
                    values["artifact_namespace"] = artifact_namespace.rstrip("/")
                if connection_id is not None:
                    values["connection_id"] = connection_id
                if connection_version is not None:
                    values["execution_connection_version"] = connection_version
                await session.execute(
                    _insert_reservation_statement(values, session.get_bind().dialect.name)
                )
                row = await session.scalar(
                    select(EngineRun)
                    .where(
                        EngineRun.thread_id == thread_id,
                        EngineRun.attempt == attempt,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise EngineRunReservationRejectedError(
                        "required engine-run provision reservation is missing"
                    )
                if row.engine != engine:
                    raise EngineRunReservationRejectedError(
                        "engine-run provision reservation has a different provider"
                    )
                _bind_locked_run_ownership(
                    row,
                    project_id=project_id,
                    app_id=app_id,
                    operation="provision",
                )
                _verify_reservation_connection_generation(
                    row,
                    connection_id=connection_id,
                    connection_version=connection_version,
                    operation="provision",
                )
                if (
                    artifact_namespace is not None
                    and row.artifact_namespace != artifact_namespace.rstrip("/")
                ):
                    raise EngineRunReservationRejectedError(
                        "engine-run provision reservation has a different artifact namespace"
                    )
                row_handle: dict[str, Any] | None = None
                invalid_handle = False
                try:
                    row_handle = _validated_projection_handle(
                        row.handle,
                        engine=engine,
                        allow_empty=False,
                    )
                except ValueError:
                    invalid_handle = True
                if invalid_handle or row_handle is None:
                    raise EngineRunReservationRejectedError(
                        "engine-run provision reservation has an invalid durable handle"
                    )
                _verify_reservation_handle_identity(
                    row_handle,
                    engine=engine,
                    idempotency_key=handle.get("idempotency_key"),
                    connection_id=connection_id,
                    operation="provision",
                )
                if row.status in _TERMINAL:
                    raise EngineRunReservationRejectedError(
                        "engine-run provision reservation is already terminal"
                    )
                if row.status in {"ready", "running"}:
                    recovered = dict(row_handle)
                elif row.status == "provisioning":
                    extras = row_handle.get("extras")
                    recovered = (
                        dict(row_handle)
                        if row_handle.get("external_run_id")
                        or (isinstance(extras, dict) and bool(extras))
                        else None
                    )
                else:
                    raise EngineRunReservationRejectedError(
                        f"engine-run status {row.status!r} cannot provision"
                    )
                await session.commit()
                return recovered
        finally:
            await dispose_engine_instance_definitively(engine_db)
    except Exception as exc:  # noqa: BLE001 — provider I/O must fail closed
        diagnostic = bounded_diagnostic(exc)
        failure = _stable_projection_failure(exc, operation="provision reservation")
    if failure is not None:
        logger.warning(
            "engine_runs.provision_reservation_failed",
            thread_id=bounded_diagnostic(thread_id, max_chars=64),
            error=diagnostic,
        )
        raise failure


def _completion_replay_status(
    row: EngineRun,
    *,
    thread_id: str,
    attempt: int,
    engine: str,
    handle: dict[str, Any],
    project_id: str | None,
    app_id: str | None,
    external_run_id: str | None,
    artifact_namespace: str,
    artifact_connection_id: str | None,
    artifact_connection_version: datetime | None,
    connection_version: datetime | None,
    completion_kind: str,
    expected_statuses: frozenset[str],
) -> str | None:
    """Verify an exact post-effect witness; nonterminal rows require normal I/O."""

    if row.status not in _TERMINAL:
        return None
    row_handle: dict[str, Any] | None = None
    invalid_handle = False
    try:
        row_handle = _validated_projection_handle(
            row.handle,
            engine=engine,
            allow_empty=False,
        )
    except ValueError:
        invalid_handle = True
    if invalid_handle or row_handle is None:
        raise EngineRunReservationRejectedError(
            "terminal engine-run projection does not match the completion witness"
        )
    expected_idempotency_key = handle.get("idempotency_key")
    mismatched = (
        row.thread_id != thread_id
        or row.attempt != attempt
        or row.engine != engine
        or row.ownership_known is not True
        or row.scope_ownership_known is not True
        or row.project_id != project_id
        or row.app_id != app_id
        or row.external_run_id != external_run_id
        or row.artifact_namespace != artifact_namespace.rstrip("/")
        or row.artifact_connection_id != artifact_connection_id
        or row.artifact_connection_version != artifact_connection_version
        or row.execution_connection_version != connection_version
        or row.completion_kind != completion_kind
        or row.status not in expected_statuses
        or row_handle.get("engine") != engine
        or row_handle.get("connection_id") != handle.get("connection_id")
        or row_handle.get("external_run_id") != external_run_id
        or row_handle.get("idempotency_key") != expected_idempotency_key
    )
    if mismatched:
        raise EngineRunReservationRejectedError(
            "terminal engine-run projection does not match the completion witness"
        )
    return str(row.status)


async def recover_engine_completion(
    thread_id: str,
    attempt: int,
    engine: str,
    handle: dict[str, Any],
    *,
    project_id: str | None = None,
    app_id: str | None = None,
    external_run_id: str | None = None,
    artifact_namespace: str,
    artifact_connection_id: str | None = None,
    artifact_connection_version: datetime | None = None,
    connection_id: str | None = None,
    connection_version: datetime | None = None,
    completion_kind: str,
    expected_statuses: frozenset[str],
) -> str | None:
    """Recover a committed post-teardown projection after its acknowledgement was lost."""

    if (
        completion_kind not in _COMPLETION_KINDS
        or not expected_statuses
        or not (expected_statuses <= _TERMINAL)
    ):
        raise ValueError("engine completion recovery contract is invalid")
    failure: Exception | None = None
    diagnostic: str | None = None
    try:
        handle, _summary = _validate_engine_run_projection_input(
            thread_id=thread_id,
            attempt=attempt,
            engine=engine,
            handle=handle,
            status=next(iter(expected_statuses)),
            project_id=project_id,
            app_id=app_id,
            external_run_id=external_run_id,
            artifact_namespace=artifact_namespace,
            artifact_connection_id=artifact_connection_id,
            connection_id=connection_id,
        )
        settings = get_settings()
        skip_projection, connection_id, connection_version, artifact_connection_id = (
            _normalize_development_connection_ids(
                handle,
                connection_id,
                connection_version,
                artifact_connection_id,
                is_locked_down=settings.is_locked_down,
            )
        )
        if skip_projection:
            return None
        _authoritative_scope(
            project_id,
            app_id,
            required=True,
            operation="completion recovery",
        )
        if artifact_connection_id is None:
            artifact_connection_version = None
        database = settings.database
        engine_db = create_async_engine(
            database_asyncpg_uri(database.uri),
            poolclass=NullPool,
            connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
        )
        try:
            session_factory = async_sessionmaker(engine_db, expire_on_commit=False)
            async with session_factory() as session:
                row = await session.scalar(
                    select(EngineRun)
                    .where(
                        EngineRun.thread_id == thread_id,
                        EngineRun.attempt == attempt,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise EngineRunReservationRejectedError(
                        "engine completion recovery projection is missing"
                    )
                recovered = _completion_replay_status(
                    row,
                    thread_id=thread_id,
                    attempt=attempt,
                    engine=engine,
                    handle=handle,
                    project_id=project_id,
                    app_id=app_id,
                    external_run_id=external_run_id,
                    artifact_namespace=artifact_namespace,
                    artifact_connection_id=artifact_connection_id,
                    artifact_connection_version=artifact_connection_version,
                    connection_version=connection_version,
                    completion_kind=completion_kind,
                    expected_statuses=expected_statuses,
                )
                await session.commit()
                return recovered
        finally:
            await dispose_engine_instance_definitively(engine_db)
    except Exception as exc:  # noqa: BLE001 - teardown recovery must fail closed
        diagnostic = bounded_diagnostic(exc)
        failure = _stable_projection_failure(exc, operation="completion recovery")
    if failure is not None:
        logger.warning(
            "engine_runs.completion_recovery_failed",
            thread_id=bounded_diagnostic(thread_id, max_chars=64),
            error=diagnostic,
        )
        raise failure


def record_engine_run_sync(*args: Any, **kwargs: Any) -> None:
    """Sync bridge that is safe when called from either sync or async graph nodes."""
    failure: Exception | None = None
    diagnostic: str | None = None
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(record_engine_run(*args, **kwargs))
        else:
            # The caller may be inside an asyncio.run wrapper whose loop will
            # close immediately after the graph node returns. Run the durable
            # projection on a short-lived worker loop and wait for completion.
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(asyncio.run, record_engine_run(*args, **kwargs)).result()
    except Exception as exc:  # noqa: BLE001
        diagnostic = bounded_diagnostic(exc)
        failure = _stable_projection_failure(exc, operation="record")
    if failure is not None:
        logger.warning("engine_runs.record_failed", error=diagnostic)
        if kwargs.get("required") is True:
            raise failure


def prepare_engine_start_sync(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
    """Sync bridge for the required pre-start reservation/recovery barrier."""

    failure: Exception | None = None
    diagnostic: str | None = None
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(prepare_engine_start(*args, **kwargs))
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, prepare_engine_start(*args, **kwargs)).result()
    except Exception as exc:  # noqa: BLE001
        diagnostic = bounded_diagnostic(exc)
        failure = _stable_projection_failure(exc, operation="start reservation")
    logger.warning("engine_runs.start_reservation_failed", error=diagnostic)
    if failure is None:  # pragma: no cover - sync bridge contract invariant
        raise EngineRunProjectionError("engine-run start reservation returned no result")
    raise failure


def prepare_engine_provision_sync(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
    """Sync bridge for the insert-or-recover provision reservation barrier."""

    failure: Exception | None = None
    diagnostic: str | None = None
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(prepare_engine_provision(*args, **kwargs))
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, prepare_engine_provision(*args, **kwargs)).result()
    except Exception as exc:  # noqa: BLE001
        diagnostic = bounded_diagnostic(exc)
        failure = _stable_projection_failure(exc, operation="provision reservation")
    logger.warning("engine_runs.provision_reservation_failed", error=diagnostic)
    if failure is None:  # pragma: no cover - sync bridge contract invariant
        raise EngineRunProjectionError("engine-run provision reservation returned no result")
    raise failure


def recover_engine_completion_sync(*args: Any, **kwargs: Any) -> str | None:
    """Sync bridge for the exact post-effect completion witness."""

    failure: Exception | None = None
    diagnostic: str | None = None
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(recover_engine_completion(*args, **kwargs))
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, recover_engine_completion(*args, **kwargs)).result()
    except Exception as exc:  # noqa: BLE001
        diagnostic = bounded_diagnostic(exc)
        failure = _stable_projection_failure(exc, operation="completion recovery")
    logger.warning("engine_runs.completion_recovery_failed", error=diagnostic)
    if failure is None:  # pragma: no cover - sync bridge contract invariant
        raise EngineRunProjectionError("engine-run completion recovery returned no result")
    raise failure
