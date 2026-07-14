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
