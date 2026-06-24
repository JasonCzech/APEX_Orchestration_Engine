from collections.abc import AsyncIterator

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apex.settings import get_settings

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
        _engine = create_async_engine(database.uri, **kwargs)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one session per request."""
    async with get_sessionmaker()() as session:
        yield session
