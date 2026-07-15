"""Atomic launch serialization for scoped idempotency keys."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings


class LaunchLockManager:
    """Use PostgreSQL advisory locks in production and per-service locks in dev/tests."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, scope: str) -> AsyncIterator[None]:
        settings = get_settings()
        if not settings.is_locked_down:
            lock = self._locks.setdefault(scope, asyncio.Lock())
            async with lock:
                yield
            return

        database = settings.database
        engine = create_async_engine(
            database_asyncpg_uri(database.uri),
            poolclass=NullPool,
            connect_args=database_ssl_connect_args(database.uri, database.ssl_mode),
        )
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                    {"scope": scope},
                )
                yield
        finally:
            await engine.dispose()
