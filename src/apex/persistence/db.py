from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apex.settings import database_asyncpg_uri, database_ssl_connect_args, get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        database = get_settings().database
        kwargs: dict[str, object] = {"pool_pre_ping": True}
        if not make_url(database.uri).drivername.startswith("sqlite"):
            kwargs.update(
                pool_size=database.pool_size,
                max_overflow=database.max_overflow,
                pool_recycle=database.pool_recycle_s,
            )
            connect_args = database_ssl_connect_args(database.uri, database.ssl_mode)
            if connect_args:
                kwargs["connect_args"] = connect_args
        _engine = create_async_engine(database_asyncpg_uri(database.uri), **kwargs)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def dispose_engine() -> None:
    """Close the process-wide pool and clear factories during application teardown."""

    global _engine, _sessionmaker
    engine = _engine
    # Reset before awaiting so a failed driver close cannot leave a disposed
    # engine cached for a later lifespan in the same worker/test process.
    _engine = None
    _sessionmaker = None
    if engine is not None:
        await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one session per request."""
    async with get_sessionmaker()() as session:
        yield session


async def release_read_transactions(*owners: Any) -> None:
    """Return read-only request sessions to the pool before slow external I/O.

    FastAPI caches ``get_session`` within a request, so multiple repositories can
    share one transaction.  Callers first materialize every required scalar or
    response model, then pass their repository/service objects here.  A pending
    ORM mutation is a programming error: silently committing it at this boundary
    would turn an availability safeguard into an unexpected write.
    """

    sessions: dict[int, AsyncSession] = {}
    pending = list(owners)
    seen: set[int] = set()
    while pending:
        owner = pending.pop()
        identity = id(owner)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(owner, AsyncSession):
            sessions[id(owner)] = owner
            continue
        # Repository wrappers are intentionally small and consistently keep
        # their dependency in one of these attributes. Fakes without a database
        # dependency remain a no-op in unit/router tests.
        for attribute in ("_session", "_store", "_repository"):
            nested = getattr(owner, attribute, None)
            if nested is not None:
                pending.append(nested)

    for session in sessions.values():
        if not session.in_transaction():
            continue
        if session.new or session.dirty or session.deleted:
            raise RuntimeError("cannot release a database session with pending mutations")
        # This is explicitly a read-only boundary. Roll back instead of commit:
        # SQLAlchemy does not represent Core/bulk DML in ``new``/``dirty``/``deleted``,
        # so committing here could otherwise persist an accidental write that the
        # ORM guard above cannot observe. Callers materialize every value they need
        # before releasing the transaction.
        await session.rollback()
