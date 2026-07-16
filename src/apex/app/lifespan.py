import asyncio
import math
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from apex.app.distributed_limits import LimitBackendUnavailable
from apex.app.logging import configure_logging
from apex.domain.diagnostics import safe_type_name
from apex.persistence.db import dispose_engine
from apex.persistence.schema_readiness import validate_schema_head
from apex.services.artifact_references import run_artifact_upload_reconciler
from apex.services.connections import get_connection_resolver
from apex.services.documents import run_document_deletion_reconciler
from apex.services.work_item_mutations import run_work_item_mutation_reconciler
from apex.settings import get_settings

logger = structlog.get_logger(__name__)
READINESS_DEPENDENCY_TIMEOUT_S = 2.0
READINESS_SUCCESS_CACHE_S = 1.0
READINESS_FAILURE_CACHE_S = 0.25
RECONCILER_STALE_AFTER_S = 120.0


class RuntimeNotReadyError(RuntimeError):
    """A serving dependency or required reconciler is unavailable."""


async def _await_task_definitively(task: asyncio.Task[None]) -> None:
    """Settle the owned shutdown task despite repeated caller cancellation."""

    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except BaseException:
            break
    error: BaseException | None = None
    try:
        task.result()
    except BaseException as exc:
        error = exc
    if interrupted:
        raise asyncio.CancelledError from None
    if error is not None:
        raise error


async def _shutdown_runtime(
    app: FastAPI,
    *,
    workers: list[asyncio.Task[None]],
    deletion_stop: asyncio.Event | None,
    runtime_started: bool,
    limit_backend: object | None,
) -> None:
    """Settle workers and every independent resource before shutdown returns."""

    cleanup_cancellation: asyncio.CancelledError | None = None
    if runtime_started:
        app.state.runtime_ready = False
        if deletion_stop is not None:
            deletion_stop.set()
        for task in workers:
            task.cancel()
        results = await asyncio.gather(*workers, return_exceptions=True)
        for task, result in zip(workers, results, strict=True):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.error(
                    "apex.background_worker_failed",
                    worker=task.get_name(),
                    error_type=safe_type_name(result),
                )
        app.state.reconciler_tasks = ()
        app.state.reconciler_heartbeats = None
        app.state.readiness_last_checked_at = None
        app.state.readiness_last_result = None

    if runtime_started:
        try:
            await get_connection_resolver().close()
        except asyncio.CancelledError as exc:
            cleanup_cancellation = exc
        except Exception as exc:
            logger.error(
                "apex.connection_resolver_close_failed",
                error_type=safe_type_name(exc),
            )
    if limit_backend is not None:
        try:
            await limit_backend.close()  # type: ignore[attr-defined]
        except asyncio.CancelledError as exc:
            cleanup_cancellation = cleanup_cancellation or exc
        except LimitBackendUnavailable:
            pass
        except Exception as exc:
            logger.error(
                "apex.limit_backend_close_failed",
                error_type=safe_type_name(exc),
            )
    try:
        await dispose_engine()
    except asyncio.CancelledError as exc:
        cleanup_cancellation = cleanup_cancellation or exc
    except Exception as exc:
        logger.error(
            "apex.database_engine_close_failed",
            error_type=safe_type_name(exc),
        )
    if runtime_started:
        logger.info("apex.shutdown")
    if cleanup_cancellation is not None:
        raise cleanup_cancellation


def assert_langgraph_encryption_compatible() -> None:
    """Fail startup when built-in AES would hide run fields from auth hooks.

    LangGraph 0.10 encrypts allowlisted run JSON before ``Runs.put`` invokes
    ``threads.create_run`` authorization. APEX authorization must validate and
    scope-stamp plaintext metadata/input/config/commands, so enabling that
    allowlist would either bypass validation or reject otherwise-valid runs.
    Checkpoint encryption and an AES key used only for decryption/migration are
    unaffected; the incompatible switch is the non-empty JSON-key allowlist.
    """

    configured_keys = {
        key.strip()
        for key in os.environ.get("LANGGRAPH_AES_JSON_KEYS", "").split(",")
        if key.strip()
    }
    if configured_keys:
        raise RuntimeError(
            "LANGGRAPH_AES_JSON_KEYS is incompatible with APEX run authorization; "
            "leave built-in AES JSON encryption disabled"
        )


async def check_runtime_readiness(app: FastAPI) -> None:
    """Fail closed when this pod can no longer safely serve new requests.

    LangGraph's built-in ``/ok`` endpoint is intentionally only a process
    liveness signal. Kubernetes readiness needs a stronger contract after
    startup too: the application schema must still be reachable/current, the
    shared Redis admission backend must answer, and every required durable
    reconciler must still be running.
    """

    _assert_local_runtime_ready(app)
    if _use_cached_readiness(app):
        return
    lock = getattr(app.state, "readiness_check_lock", None)
    if lock is None:
        # Assignment contains no await and is therefore atomic on the serving
        # event loop. Lifespan initializes this eagerly; the fallback keeps the
        # checker independently testable.
        lock = asyncio.Lock()
        app.state.readiness_check_lock = lock

    async with lock:
        # A worker can exit while this request waits behind an in-flight probe.
        _assert_local_runtime_ready(app)
        if _use_cached_readiness(app):
            return

        try:
            # Keep the public probe below the chart's three-second request timeout
            # even when a driver/client is missing its own network timeout.
            async with asyncio.timeout(READINESS_DEPENDENCY_TIMEOUT_S):
                await validate_schema_head()
                limit_backend = getattr(app.state, "distributed_limit_backend", None)
                if limit_backend is not None:
                    await limit_backend.check_ready()
                # Do not publish a successful dependency result if a required
                # local worker exited while the network checks were in flight.
                _assert_local_runtime_ready(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Cache only the boolean. Dependency exceptions can contain URIs or
            # credentials and must not become long-lived application state.
            app.state.readiness_last_checked_at = asyncio.get_running_loop().time()
            app.state.readiness_last_result = False
            raise
        app.state.readiness_last_checked_at = asyncio.get_running_loop().time()
        app.state.readiness_last_result = True


def _use_cached_readiness(app: FastAPI) -> bool:
    """Return cached success or raise cached opaque failure when still fresh."""

    checked_at = _finite_clock_timestamp(getattr(app.state, "readiness_last_checked_at", None))
    cached_ready = getattr(app.state, "readiness_last_result", None)
    if checked_at is None or not isinstance(cached_ready, bool):
        return False
    cache_s = READINESS_SUCCESS_CACHE_S if cached_ready else READINESS_FAILURE_CACHE_S
    age = asyncio.get_running_loop().time() - checked_at
    if age < 0 or age >= cache_s:
        return False
    if not cached_ready:
        raise RuntimeNotReadyError("a serving dependency is unavailable")
    return True


def _finite_clock_timestamp(value: object) -> float | None:
    """Normalize an exact finite clock scalar without overflowing on huge integers."""

    if type(value) is int:
        if value.bit_length() > 128:
            return None
        return float(value)
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _assert_local_runtime_ready(app: FastAPI) -> None:
    """Check process-local health without any external I/O."""

    if not bool(getattr(app.state, "runtime_ready", False)):
        raise RuntimeNotReadyError("application runtime is not ready")
    raw_workers: object = getattr(app.state, "reconciler_tasks", ())
    workers = (
        tuple(task for task in raw_workers if isinstance(task, asyncio.Task))
        if isinstance(raw_workers, (list, tuple))
        else ()
    )
    if len(workers) != 3 or any(task.done() for task in workers):
        raise RuntimeNotReadyError("a required reconciliation worker is unavailable")
    heartbeats = getattr(app.state, "reconciler_heartbeats", None)
    if not isinstance(heartbeats, dict):
        raise RuntimeNotReadyError("reconciliation worker heartbeat state is unavailable")
    expected_workers = {task.get_name() for task in workers}
    now = asyncio.get_running_loop().time()
    heartbeat_timestamps = tuple(_finite_clock_timestamp(value) for value in heartbeats.values())
    if set(heartbeats) != expected_workers or any(
        heartbeat is None or not 0 <= now - heartbeat <= RECONCILER_STALE_AFTER_S
        for heartbeat in heartbeat_timestamps
    ):
        raise RuntimeNotReadyError("a required reconciliation worker is stale")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    assert_langgraph_encryption_compatible()
    settings = get_settings()
    app.state.settings = settings
    logger.info(
        "apex.startup",
        environment=settings.environment,
        version=settings.version,
    )
    limit_backend = getattr(app.state, "distributed_limit_backend", None)
    workers: list[asyncio.Task[None]] = []
    deletion_stop: asyncio.Event | None = None
    runtime_started = False
    app.state.runtime_ready = False
    app.state.reconciler_tasks = ()
    app.state.reconciler_heartbeats = None
    app.state.readiness_check_lock = asyncio.Lock()
    app.state.readiness_last_checked_at = None
    app.state.readiness_last_result = None
    try:
        if limit_backend is not None:
            # A locked/HA pod must not become ready with only process-local limits.
            await limit_backend.check_ready()
        # Do not start reconcilers or expose a healthy `/ok` endpoint until the
        # application-owned schema matches this exact image. This protects manual
        # migration deployments as well as the chart's migrate-then-roll hook.
        await validate_schema_head()
        deletion_stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        worker_names = (
            "document-deletion-reconciler",
            "artifact-upload-reconciler",
            "work-item-mutation-reconciler",
        )
        app.state.reconciler_heartbeats = {worker_name: loop.time() for worker_name in worker_names}

        def heartbeat(worker_name: str) -> None:
            app.state.reconciler_heartbeats[worker_name] = loop.time()

        workers = [
            asyncio.create_task(
                run_document_deletion_reconciler(
                    deletion_stop,
                    lambda: heartbeat(worker_names[0]),
                ),
                name=worker_names[0],
            ),
            asyncio.create_task(
                run_artifact_upload_reconciler(
                    deletion_stop,
                    lambda: heartbeat(worker_names[1]),
                ),
                name=worker_names[1],
            ),
            asyncio.create_task(
                run_work_item_mutation_reconciler(
                    deletion_stop,
                    lambda: heartbeat(worker_names[2]),
                ),
                name=worker_names[2],
            ),
        ]
        app.state.reconciler_tasks = tuple(workers)
        runtime_started = True
        app.state.runtime_ready = True
        yield
    finally:
        # A disconnect, server shutdown, and task-group teardown can cancel the
        # lifespan task more than once. One child owns the complete shutdown
        # sequence so workers settle before their adapters and pools disappear.
        original_error = sys.exception()
        shutdown_task = asyncio.create_task(
            _shutdown_runtime(
                app,
                workers=workers,
                deletion_stop=deletion_stop,
                runtime_started=runtime_started,
                limit_backend=limit_backend,
            ),
            name="apex-runtime-shutdown",
        )
        try:
            await _await_task_definitively(shutdown_task)
        except BaseException:
            if original_error is None:
                raise
