"""Concurrency checks for the remote-create guard."""

import asyncio
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import apex.adapters.remote_idempotency as idempotency
from apex.adapters.remote_idempotency import remote_create_guard
from apex.services.launch_locks import LaunchLockManager


def test_remote_create_guard_is_safe_across_event_loops() -> None:
    start = threading.Barrier(3)
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    async def guarded_work() -> None:
        nonlocal active, maximum_active
        async with remote_create_guard("provider:remote-scope:idempotency-key"):
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.03)
            with state_lock:
                active -= 1

    def worker() -> None:
        start.wait()
        asyncio.run(guarded_work())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        start.wait()
        for future in futures:
            future.result(timeout=2)

    assert maximum_active == 1


def test_launch_lock_is_shared_by_request_services_across_event_loops() -> None:
    start = threading.Barrier(3)
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    async def guarded_work() -> None:
        nonlocal active, maximum_active
        async with LaunchLockManager().hold("same-scoped-launch"):
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.03)
            with state_lock:
                active -= 1

    def worker() -> None:
        start.wait()
        asyncio.run(guarded_work())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        start.wait()
        for future in futures:
            future.result(timeout=2)

    assert maximum_active == 1


async def test_remote_create_guard_uses_configured_distributed_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def fake_postgres_guard(key: str) -> AsyncIterator[None]:
        events.append(f"enter:{key}")
        try:
            yield
        finally:
            events.append(f"exit:{key}")

    monkeypatch.setattr(
        idempotency,
        "get_settings",
        lambda: SimpleNamespace(distributed_remote_creation_lock=True),
    )
    monkeypatch.setattr(idempotency, "_postgres_guard", fake_postgres_guard)

    async with remote_create_guard("remote-key"):
        events.append("body")

    assert events == ["enter:remote-key", "body", "exit:remote-key"]


async def test_postgres_guard_sets_timeouts_and_releases_process_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Connection:
        async def execute(self, statement: object, *_args: object) -> None:
            statements.append(str(statement))

    class Transaction:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    class Engine:
        disposed = False

        def begin(self) -> Transaction:
            return Transaction()

        async def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    admission = threading.BoundedSemaphore(1)
    monkeypatch.setattr(idempotency, "_postgres_admission", admission)
    monkeypatch.setattr(
        idempotency,
        "get_settings",
        lambda: SimpleNamespace(
            database=SimpleNamespace(uri="postgresql://db/apex", ssl_mode="disable")
        ),
    )
    monkeypatch.setattr(idempotency, "database_asyncpg_uri", lambda uri: uri)
    monkeypatch.setattr(idempotency, "database_ssl_connect_args", lambda *_args: {})
    monkeypatch.setattr(idempotency, "create_async_engine", lambda *_args, **_kwargs: engine)

    async with idempotency._postgres_guard("remote-key"):
        assert admission.acquire(blocking=False) is False

    assert engine.disposed is True
    assert admission.acquire(blocking=False) is True
    admission.release()
    assert any("lock_timeout" in statement for statement in statements)
    assert any("statement_timeout" in statement for statement in statements)
    assert any("pg_advisory_xact_lock" in statement for statement in statements)


async def test_postgres_guard_disposal_survives_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_entered = asyncio.Event()

    class Connection:
        async def execute(self, _statement: object, *_args: object) -> None:
            return None

    class Transaction:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    class Engine:
        def __init__(self) -> None:
            self.dispose_entered = asyncio.Event()
            self.allow_dispose = asyncio.Event()
            self.disposed = False

        def begin(self) -> Transaction:
            return Transaction()

        async def dispose(self) -> None:
            self.dispose_entered.set()
            await self.allow_dispose.wait()
            self.disposed = True

    engine = Engine()
    admission = threading.BoundedSemaphore(1)
    monkeypatch.setattr(idempotency, "_postgres_admission", admission)
    monkeypatch.setattr(
        idempotency,
        "get_settings",
        lambda: SimpleNamespace(
            database=SimpleNamespace(uri="postgresql://db/apex", ssl_mode="disable")
        ),
    )
    monkeypatch.setattr(idempotency, "database_asyncpg_uri", lambda uri: uri)
    monkeypatch.setattr(idempotency, "database_ssl_connect_args", lambda *_args: {})
    monkeypatch.setattr(idempotency, "create_async_engine", lambda *_args, **_kwargs: engine)

    async def guarded() -> None:
        async with idempotency._postgres_guard("remote-key"):
            body_entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(guarded())
    await body_entered.wait()
    task.cancel()
    await engine.dispose_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)

    assert task.done() is False
    assert admission.acquire(blocking=False) is False
    engine.allow_dispose.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.disposed is True
    assert admission.acquire(blocking=False) is True
    admission.release()
