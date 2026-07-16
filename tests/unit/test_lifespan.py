"""Application lifespan owns all durable cleanup and reconciliation workers."""

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from apex.app import lifespan as lifespan_module


def _set_healthy_reconciler_state(
    app: FastAPI,
    tasks: tuple[asyncio.Task[None], ...],
) -> None:
    app.state.reconciler_tasks = tasks
    now = asyncio.get_running_loop().time()
    app.state.reconciler_heartbeats = {task.get_name(): now for task in tasks}


def test_langgraph_aes_json_allowlist_fails_startup_compatibility_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGGRAPH_AES_JSON_KEYS", "request,limits")

    with pytest.raises(RuntimeError, match="LANGGRAPH_AES_JSON_KEYS"):
        lifespan_module.assert_langgraph_encryption_compatible()


def test_langgraph_aes_decryption_only_mode_remains_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGGRAPH_AES_JSON_KEYS", raising=False)

    lifespan_module.assert_langgraph_encryption_compatible()


async def test_lifespan_starts_and_stops_artifact_upload_reconciler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = {
        "documents": asyncio.Event(),
        "artifacts": asyncio.Event(),
        "work-items": asyncio.Event(),
    }
    stopped = {
        "documents": asyncio.Event(),
        "artifacts": asyncio.Event(),
        "work-items": asyncio.Event(),
    }
    stop_events: list[asyncio.Event] = []

    def runner(name: str):  # noqa: ANN202 - local async worker factory
        async def _run(
            stop: asyncio.Event,
            heartbeat: Callable[[], None] | None = None,
        ) -> None:
            stop_events.append(stop)
            if heartbeat is not None:
                heartbeat()
            started[name].set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped[name].set()

        return _run

    class Resolver:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    resolver = Resolver()
    database_disposed = asyncio.Event()
    monkeypatch.setattr(lifespan_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        lifespan_module,
        "get_settings",
        lambda: SimpleNamespace(environment="test", version="test"),
    )
    schema_checked = asyncio.Event()

    async def validate_schema() -> None:
        schema_checked.set()

    monkeypatch.setattr(lifespan_module, "validate_schema_head", validate_schema)
    monkeypatch.setattr(lifespan_module, "run_document_deletion_reconciler", runner("documents"))
    monkeypatch.setattr(lifespan_module, "run_artifact_upload_reconciler", runner("artifacts"))
    monkeypatch.setattr(
        lifespan_module,
        "run_work_item_mutation_reconciler",
        runner("work-items"),
    )
    monkeypatch.setattr(lifespan_module, "get_connection_resolver", lambda: resolver)

    async def dispose_database() -> None:
        database_disposed.set()

    monkeypatch.setattr(lifespan_module, "dispose_engine", dispose_database)

    async with lifespan_module.lifespan(FastAPI()):
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in started.values())),
            timeout=1,
        )

    assert len({id(event) for event in stop_events}) == 1
    assert schema_checked.is_set()
    assert stop_events[0].is_set()
    assert all(event.is_set() for event in stopped.values())
    assert resolver.closed is True
    assert database_disposed.is_set()


async def test_runtime_readiness_rechecks_schema_redis_and_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.state.runtime_ready = True
    stop = asyncio.Event()

    async def worker() -> None:
        await stop.wait()

    tasks = tuple(asyncio.create_task(worker()) for _ in range(3))
    _set_healthy_reconciler_state(app, tasks)
    schema_checked = asyncio.Event()

    async def validate_schema() -> None:
        schema_checked.set()

    class Backend:
        def __init__(self) -> None:
            self.checked = False

        async def check_ready(self) -> None:
            self.checked = True

    backend = Backend()
    app.state.distributed_limit_backend = backend
    monkeypatch.setattr(lifespan_module, "validate_schema_head", validate_schema)
    try:
        await lifespan_module.check_runtime_readiness(app)
    finally:
        stop.set()
        await asyncio.gather(*tasks)

    assert schema_checked.is_set()
    assert backend.checked is True


async def test_runtime_readiness_coalesces_concurrent_public_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.state.runtime_ready = True
    worker_stop = asyncio.Event()
    dependency_started = asyncio.Event()
    release_dependency = asyncio.Event()
    calls = 0

    async def worker() -> None:
        await worker_stop.wait()

    async def validate_schema() -> None:
        nonlocal calls
        calls += 1
        dependency_started.set()
        await release_dependency.wait()

    tasks = tuple(asyncio.create_task(worker()) for _ in range(3))
    _set_healthy_reconciler_state(app, tasks)
    monkeypatch.setattr(lifespan_module, "validate_schema_head", validate_schema)
    probes = [asyncio.create_task(lifespan_module.check_runtime_readiness(app)) for _ in range(20)]
    try:
        await asyncio.wait_for(dependency_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert calls == 1
        release_dependency.set()
        await asyncio.gather(*probes)
        await lifespan_module.check_runtime_readiness(app)
        assert calls == 1
    finally:
        release_dependency.set()
        await asyncio.gather(*probes, return_exceptions=True)
        worker_stop.set()
        await asyncio.gather(*tasks)


async def test_runtime_readiness_caches_only_opaque_dependency_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.state.runtime_ready = True
    worker_stop = asyncio.Event()
    calls = 0
    secret = "postgresql://admin:readiness-canary@db.internal/apex"

    async def worker() -> None:
        await worker_stop.wait()

    async def fail_schema() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError(secret)

    tasks = tuple(asyncio.create_task(worker()) for _ in range(3))
    _set_healthy_reconciler_state(app, tasks)
    monkeypatch.setattr(lifespan_module, "validate_schema_head", fail_schema)
    monkeypatch.setattr(lifespan_module, "READINESS_FAILURE_CACHE_S", 60.0)
    try:
        with pytest.raises(RuntimeError, match="readiness-canary"):
            await lifespan_module.check_runtime_readiness(app)
        with pytest.raises(lifespan_module.RuntimeNotReadyError, match="dependency"):
            await lifespan_module.check_runtime_readiness(app)
    finally:
        worker_stop.set()
        await asyncio.gather(*tasks)

    assert calls == 1
    assert app.state.readiness_last_result is False
    assert secret not in str(vars(app.state))


async def test_runtime_readiness_fails_when_a_reconciler_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.state.runtime_ready = True

    async def worker() -> None:
        return None

    tasks = tuple(asyncio.create_task(worker()) for _ in range(3))
    await asyncio.gather(*tasks)
    _set_healthy_reconciler_state(app, tasks)
    monkeypatch.setattr(lifespan_module, "validate_schema_head", _async_none)

    with pytest.raises(lifespan_module.RuntimeNotReadyError, match="reconciliation worker"):
        await lifespan_module.check_runtime_readiness(app)


async def test_runtime_readiness_fails_when_a_reconciler_heartbeat_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.state.runtime_ready = True
    stop = asyncio.Event()

    async def worker() -> None:
        await stop.wait()

    worker_names = (
        "document-deletion-reconciler",
        "artifact-upload-reconciler",
        "work-item-mutation-reconciler",
    )
    tasks = tuple(asyncio.create_task(worker(), name=worker_name) for worker_name in worker_names)
    app.state.reconciler_tasks = tasks
    now = asyncio.get_running_loop().time()
    app.state.reconciler_heartbeats = {worker_name: now for worker_name in worker_names}
    app.state.reconciler_heartbeats[worker_names[1]] = now - 10
    monkeypatch.setattr(lifespan_module, "RECONCILER_STALE_AFTER_S", 1.0)
    monkeypatch.setattr(lifespan_module, "validate_schema_head", _async_none)
    try:
        with pytest.raises(lifespan_module.RuntimeNotReadyError, match="stale"):
            await lifespan_module.check_runtime_readiness(app)
    finally:
        stop.set()
        await asyncio.gather(*tasks)


async def test_runtime_readiness_fails_when_reconciler_heartbeat_state_is_missing() -> None:
    app = FastAPI()
    app.state.runtime_ready = True
    stop = asyncio.Event()

    async def worker() -> None:
        await stop.wait()

    tasks = tuple(asyncio.create_task(worker()) for _ in range(3))
    app.state.reconciler_tasks = tasks
    app.state.reconciler_heartbeats = None
    try:
        with pytest.raises(lifespan_module.RuntimeNotReadyError, match="heartbeat state"):
            await lifespan_module.check_runtime_readiness(app)
    finally:
        stop.set()
        await asyncio.gather(*tasks)


@pytest.mark.parametrize("invalid_heartbeat", [True, float("nan"), float("inf")])
async def test_runtime_readiness_rejects_non_finite_or_non_numeric_heartbeats(
    invalid_heartbeat: object,
) -> None:
    app = FastAPI()
    app.state.runtime_ready = True
    stop = asyncio.Event()

    async def worker() -> None:
        await stop.wait()

    tasks = tuple(asyncio.create_task(worker()) for _ in range(3))
    _set_healthy_reconciler_state(app, tasks)
    app.state.reconciler_heartbeats[tasks[0].get_name()] = invalid_heartbeat
    try:
        with pytest.raises(lifespan_module.RuntimeNotReadyError, match="stale"):
            await lifespan_module.check_runtime_readiness(app)
    finally:
        stop.set()
        await asyncio.gather(*tasks)


async def test_runtime_readiness_does_not_trust_a_future_cache_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.state.runtime_ready = True
    stop = asyncio.Event()
    schema_checked = False

    async def worker() -> None:
        await stop.wait()

    async def validate_schema() -> None:
        nonlocal schema_checked
        schema_checked = True

    tasks = tuple(asyncio.create_task(worker()) for _ in range(3))
    _set_healthy_reconciler_state(app, tasks)
    app.state.readiness_last_checked_at = asyncio.get_running_loop().time() + 60
    app.state.readiness_last_result = True
    monkeypatch.setattr(lifespan_module, "validate_schema_head", validate_schema)
    try:
        await lifespan_module.check_runtime_readiness(app)
    finally:
        stop.set()
        await asyncio.gather(*tasks)

    assert schema_checked is True


async def test_runtime_readiness_does_not_cache_success_when_worker_exits_mid_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.state.runtime_ready = True
    worker_stop = asyncio.Event()
    dependency_started = asyncio.Event()
    release_dependency = asyncio.Event()

    async def worker() -> None:
        await worker_stop.wait()

    async def validate_schema() -> None:
        dependency_started.set()
        await release_dependency.wait()

    tasks = tuple(asyncio.create_task(worker()) for _ in range(3))
    _set_healthy_reconciler_state(app, tasks)
    monkeypatch.setattr(lifespan_module, "validate_schema_head", validate_schema)
    probe = asyncio.create_task(lifespan_module.check_runtime_readiness(app))
    try:
        await asyncio.wait_for(dependency_started.wait(), timeout=1)
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        release_dependency.set()
        with pytest.raises(lifespan_module.RuntimeNotReadyError, match="reconciliation worker"):
            await probe
    finally:
        release_dependency.set()
        worker_stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert getattr(app.state, "readiness_last_result", None) is not True


async def test_runtime_readiness_bounds_dependency_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.state.runtime_ready = True
    stop = asyncio.Event()

    async def worker() -> None:
        await stop.wait()

    async def hung_schema_check() -> None:
        await asyncio.Event().wait()

    tasks = tuple(asyncio.create_task(worker()) for _ in range(3))
    _set_healthy_reconciler_state(app, tasks)
    monkeypatch.setattr(lifespan_module, "validate_schema_head", hung_schema_check)
    monkeypatch.setattr(lifespan_module, "READINESS_DEPENDENCY_TIMEOUT_S", 0.01)
    try:
        with pytest.raises(TimeoutError):
            await lifespan_module.check_runtime_readiness(app)
    finally:
        stop.set()
        await asyncio.gather(*tasks)


async def test_lifespan_does_not_start_workers_when_schema_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workers_started = False

    async def fail_schema_check() -> None:
        raise RuntimeError("stale schema")

    async def worker(
        _: asyncio.Event,
        _heartbeat: Callable[[], None] | None = None,
    ) -> None:
        nonlocal workers_started
        workers_started = True

    monkeypatch.setattr(lifespan_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        lifespan_module,
        "get_settings",
        lambda: SimpleNamespace(environment="test", version="test"),
    )
    monkeypatch.setattr(lifespan_module, "validate_schema_head", fail_schema_check)
    monkeypatch.setattr(lifespan_module, "run_document_deletion_reconciler", worker)
    monkeypatch.setattr(lifespan_module, "run_artifact_upload_reconciler", worker)
    monkeypatch.setattr(lifespan_module, "run_work_item_mutation_reconciler", worker)

    with pytest.raises(RuntimeError, match="stale schema"):
        async with lifespan_module.lifespan(FastAPI()):
            pytest.fail("lifespan must not yield when the schema is stale")

    assert workers_started is False


async def test_lifespan_closes_distributed_limit_backend_when_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LimitBackend:
        def __init__(self) -> None:
            self.ready = False
            self.closed = False

        async def check_ready(self) -> None:
            self.ready = True

        async def close(self) -> None:
            self.closed = True

    async def fail_schema_check() -> None:
        raise RuntimeError("stale schema")

    backend = LimitBackend()
    database_disposed = asyncio.Event()
    app = FastAPI()
    app.state.distributed_limit_backend = backend
    monkeypatch.setattr(lifespan_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        lifespan_module,
        "get_settings",
        lambda: SimpleNamespace(environment="test", version="test"),
    )
    monkeypatch.setattr(lifespan_module, "validate_schema_head", fail_schema_check)

    async def dispose_database() -> None:
        database_disposed.set()

    monkeypatch.setattr(lifespan_module, "dispose_engine", dispose_database)

    with pytest.raises(RuntimeError, match="stale schema"):
        async with lifespan_module.lifespan(app):
            pytest.fail("lifespan must not yield when the schema is stale")

    assert backend.ready is True
    assert backend.closed is True
    assert database_disposed.is_set()


async def test_lifespan_worker_failure_does_not_skip_other_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    waiting_worker_stopped = asyncio.Event()

    async def failed_worker(
        _: asyncio.Event,
        _heartbeat: Callable[[], None] | None = None,
    ) -> None:
        raise RuntimeError("worker failed")

    async def waiting_worker(
        _: asyncio.Event,
        _heartbeat: Callable[[], None] | None = None,
    ) -> None:
        try:
            await release.wait()
        finally:
            waiting_worker_stopped.set()

    class Resolver:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    resolver = Resolver()
    monkeypatch.setattr(lifespan_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        lifespan_module,
        "get_settings",
        lambda: SimpleNamespace(environment="test", version="test"),
    )
    monkeypatch.setattr(lifespan_module, "validate_schema_head", lambda: _async_none())
    monkeypatch.setattr(lifespan_module, "run_document_deletion_reconciler", failed_worker)
    monkeypatch.setattr(lifespan_module, "run_artifact_upload_reconciler", waiting_worker)
    monkeypatch.setattr(lifespan_module, "run_work_item_mutation_reconciler", waiting_worker)
    monkeypatch.setattr(lifespan_module, "get_connection_resolver", lambda: resolver)

    async with lifespan_module.lifespan(FastAPI()):
        await asyncio.sleep(0)

    assert waiting_worker_stopped.is_set()
    assert resolver.closed is True


async def test_lifespan_disposes_database_when_an_earlier_closer_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def waiting_worker(
        _: asyncio.Event,
        _heartbeat: Callable[[], None] | None = None,
    ) -> None:
        await asyncio.Event().wait()

    class Resolver:
        async def close(self) -> None:
            raise asyncio.CancelledError

    database_disposed = asyncio.Event()
    monkeypatch.setattr(lifespan_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        lifespan_module,
        "get_settings",
        lambda: SimpleNamespace(environment="test", version="test"),
    )
    monkeypatch.setattr(lifespan_module, "validate_schema_head", lambda: _async_none())
    monkeypatch.setattr(lifespan_module, "run_document_deletion_reconciler", waiting_worker)
    monkeypatch.setattr(lifespan_module, "run_artifact_upload_reconciler", waiting_worker)
    monkeypatch.setattr(
        lifespan_module,
        "run_work_item_mutation_reconciler",
        waiting_worker,
    )
    monkeypatch.setattr(lifespan_module, "get_connection_resolver", Resolver)

    async def dispose_database() -> None:
        database_disposed.set()

    monkeypatch.setattr(lifespan_module, "dispose_engine", dispose_database)

    manager = lifespan_module.lifespan(FastAPI())
    await manager.__aenter__()
    with pytest.raises(asyncio.CancelledError):
        await manager.__aexit__(None, None, None)

    assert database_disposed.is_set()


async def test_lifespan_shutdown_survives_repeated_cancellation_at_every_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workers_stopping = 0
    all_workers_stopping = asyncio.Event()
    allow_workers = asyncio.Event()
    workers_settled = 0

    async def waiting_worker(
        _: asyncio.Event,
        _heartbeat: Callable[[], None] | None = None,
    ) -> None:
        nonlocal all_workers_stopping, workers_settled, workers_stopping
        try:
            await asyncio.Event().wait()
        finally:
            workers_stopping += 1
            if workers_stopping == 3:
                all_workers_stopping.set()
            await allow_workers.wait()
            workers_settled += 1

    class Resolver:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.allow = asyncio.Event()
            self.closed = False

        async def close(self) -> None:
            self.entered.set()
            await self.allow.wait()
            self.closed = True

    class Backend:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.allow = asyncio.Event()
            self.closed = False

        async def check_ready(self) -> None:
            return None

        async def close(self) -> None:
            self.entered.set()
            await self.allow.wait()
            self.closed = True

    resolver = Resolver()
    backend = Backend()
    database_entered = asyncio.Event()
    allow_database = asyncio.Event()
    database_disposed = False
    app = FastAPI()
    app.state.distributed_limit_backend = backend
    monkeypatch.setattr(lifespan_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        lifespan_module,
        "get_settings",
        lambda: SimpleNamespace(environment="test", version="test"),
    )
    monkeypatch.setattr(lifespan_module, "validate_schema_head", _async_none)
    monkeypatch.setattr(lifespan_module, "run_document_deletion_reconciler", waiting_worker)
    monkeypatch.setattr(lifespan_module, "run_artifact_upload_reconciler", waiting_worker)
    monkeypatch.setattr(
        lifespan_module,
        "run_work_item_mutation_reconciler",
        waiting_worker,
    )
    monkeypatch.setattr(lifespan_module, "get_connection_resolver", lambda: resolver)

    async def dispose_database() -> None:
        nonlocal database_disposed
        database_entered.set()
        await allow_database.wait()
        database_disposed = True

    monkeypatch.setattr(lifespan_module, "dispose_engine", dispose_database)
    manager = lifespan_module.lifespan(app)
    await manager.__aenter__()
    exit_task = asyncio.create_task(manager.__aexit__(None, None, None))

    async def cancel_twice() -> None:
        exit_task.cancel()
        await asyncio.sleep(0)
        exit_task.cancel()
        await asyncio.sleep(0)
        assert exit_task.done() is False

    await all_workers_stopping.wait()
    await cancel_twice()
    assert resolver.entered.is_set() is False
    allow_workers.set()

    await resolver.entered.wait()
    await cancel_twice()
    assert backend.entered.is_set() is False
    resolver.allow.set()

    await backend.entered.wait()
    await cancel_twice()
    assert database_entered.is_set() is False
    backend.allow.set()

    await database_entered.wait()
    await cancel_twice()
    allow_database.set()

    with pytest.raises(asyncio.CancelledError):
        await exit_task
    assert workers_settled == 3
    assert resolver.closed is True
    assert backend.closed is True
    assert database_disposed is True
    assert app.state.reconciler_tasks == ()


async def _async_none() -> None:
    return None
