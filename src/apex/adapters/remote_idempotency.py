"""Cross-loop and multi-replica guards for costly remote create operations.

Execution adapters are cached across graph nodes, and those nodes may run on
different short-lived event loops.  An ``asyncio.Lock`` therefore cannot be
stored on an adapter (or in a module-level keyed registry): it is bound to the
loop that first waits on it.  The process guard below uses non-blocking
``threading.Lock`` acquisition plus async backoff, so it is safe across both
threads and event loops without blocking either loop.

Multi-replica deployments additionally serialize through PostgreSQL when
``distributed_remote_creation_lock`` is enabled.  A
transaction-scoped advisory lock gives all API/worker replicas the same create
critical section and is released automatically if a worker exits.  Local/dev
adapters retain the process guard and provider-side idempotency mechanisms,
without turning hermetic adapter tests into database integration tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings

_INITIAL_WAIT_S = 0.001
_MAX_WAIT_S = 0.05


@dataclass(slots=True)
class _LockEntry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


_registry_lock = threading.Lock()
_registry: dict[str, _LockEntry] = {}


def _lease(key: str) -> _LockEntry:
    with _registry_lock:
        entry = _registry.get(key)
        if entry is None:
            entry = _LockEntry()
            _registry[key] = entry
        entry.users += 1
        return entry


def _return_lease(key: str, entry: _LockEntry) -> None:
    with _registry_lock:
        entry.users -= 1
        if entry.users == 0 and _registry.get(key) is entry:
            _registry.pop(key, None)


async def _acquire(entry: _LockEntry) -> None:
    """Acquire without binding to an event loop or blocking its thread.

    A synchronous non-blocking attempt cannot be cancelled between taking the
    lock and returning, so cancellation never strands a lock owned by an
    abandoned ``to_thread`` call.
    """

    delay = _INITIAL_WAIT_S
    while not entry.lock.acquire(blocking=False):
        await asyncio.sleep(delay)
        delay = min(delay * 2, _MAX_WAIT_S)


def _advisory_key(key: str) -> int:
    """Stable signed int64 accepted by PostgreSQL advisory-lock functions."""

    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@asynccontextmanager
async def _postgres_guard(key: str) -> AsyncIterator[None]:
    settings = get_settings()
    database = settings.database
    engine = create_async_engine(
        database_asyncpg_uri(database.uri),
        poolclass=NullPool,
        connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _advisory_key(key)},
            )
            yield
    finally:
        await engine.dispose()


@asynccontextmanager
async def remote_create_guard(key: str) -> AsyncIterator[None]:
    """Serialize one remote get-or-create critical section by stable key.

    Callers must repeat the provider lookup *inside* this context immediately
    before creating.  When the deployment enables the distributed lock,
    PostgreSQL protects across replicas; the process lock prevents duplicate
    local contenders from each occupying a database connection while they wait.
    """

    entry = _lease(key)
    acquired = False
    try:
        await _acquire(entry)
        acquired = True
        if get_settings().distributed_remote_creation_lock:
            async with _postgres_guard(key):
                yield
        else:
            yield
    finally:
        if acquired:
            entry.lock.release()
        _return_lease(key, entry)
