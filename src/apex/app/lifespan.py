import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from apex.app.distributed_limits import LimitBackendUnavailable
from apex.app.logging import configure_logging
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

    checked_at = getattr(app.state, "readiness_last_checked_at", None)
    cached_ready = getattr(app.state, "readiness_last_result", None)
    if not isinstance(checked_at, (int, float)) or not isinstance(cached_ready, bool):
        return False
    cache_s = READINESS_SUCCESS_CACHE_S if cached_ready else READINESS_FAILURE_CACHE_S
    if asyncio.get_running_loop().time() - checked_at >= cache_s:
        return False
    if not cached_ready:
        raise RuntimeNotReadyError("a serving dependency is unavailable")
    return True


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
    if isinstance(heartbeats, dict):
        expected_workers = {task.get_name() for task in workers}
        now = asyncio.get_running_loop().time()
        if set(heartbeats) != expected_workers or any(
            not isinstance(heartbeat, (int, float)) or now - heartbeat > RECONCILER_STALE_AFTER_S
            for heartbeat in heartbeats.values()
        ):
            raise RuntimeNotReadyError("a required reconciliation worker is stale")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    app.state.settings = settings
    logger.info(
        "apex.startup",
        environment=settings.environment,
        version=settings.version,
    )
    limit_backend = getattr(app.state, "distributed_limit_backend", None)
    workers: list[asyncio.Task[None]] = []
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
        try:
            yield
        finally:
            app.state.runtime_ready = False
            deletion_stop.set()
            for task in workers:
                task.cancel()
            results = await asyncio.gather(*workers, return_exceptions=True)
            for task, result in zip(workers, results, strict=True):
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    logger.error(
                        "apex.background_worker_failed",
                        worker=task.get_name(),
                        error_type=result.__class__.__name__,
                    )
            app.state.reconciler_tasks = ()
            app.state.reconciler_heartbeats = None
            app.state.readiness_last_checked_at = None
            app.state.readiness_last_result = None
    finally:
        try:
            if runtime_started:
                try:
                    await get_connection_resolver().close()
                except Exception as exc:
                    logger.error(
                        "apex.connection_resolver_close_failed",
                        error_type=exc.__class__.__name__,
                    )
        finally:
            try:
                if limit_backend is not None:
                    try:
                        await limit_backend.close()
                    except LimitBackendUnavailable:
                        pass
                    except Exception as exc:
                        logger.error(
                            "apex.limit_backend_close_failed",
                            error_type=exc.__class__.__name__,
                        )
            finally:
                # A cancellation/BaseException from another closer must not
                # strand the SQLAlchemy pool. Nested finally blocks preserve
                # the original exception after every independent resource has
                # received its teardown attempt.
                try:
                    await dispose_engine()
                except Exception as exc:
                    logger.error(
                        "apex.database_engine_close_failed",
                        error_type=exc.__class__.__name__,
                    )
        if runtime_started:
            logger.info("apex.shutdown")
