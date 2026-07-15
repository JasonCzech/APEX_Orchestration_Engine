from collections.abc import Iterator
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apex.persistence import db
from apex.persistence.models import ApiConsumer
from apex.settings import DatabaseSettings


@pytest.fixture(autouse=True)
def reset_engine() -> Iterator[None]:
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


class SettingsStub:
    def __init__(self, database: DatabaseSettings) -> None:
        self.database = database


def _captured_engine_kwargs(
    monkeypatch: pytest.MonkeyPatch, database: DatabaseSettings
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_get_settings() -> SettingsStub:
        return SettingsStub(database)

    def fake_create_async_engine(uri: str, **kwargs: object) -> AsyncEngine:
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        return cast(AsyncEngine, object())

    monkeypatch.setattr(db, "get_settings", fake_get_settings)
    monkeypatch.setattr(db, "create_async_engine", fake_create_async_engine)

    db.get_engine()

    assert captured["uri"] == database.uri
    return cast(dict[str, object], captured["kwargs"])


def test_get_engine_enables_ssl_for_remote_postgres_without_explicit_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _captured_engine_kwargs(
        monkeypatch,
        DatabaseSettings(uri="postgresql+asyncpg://u:p@db.example.com:5432/apex"),
    )

    assert kwargs["connect_args"] == {"ssl": True}


def test_get_engine_leaves_local_postgres_without_ssl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _captured_engine_kwargs(
        monkeypatch,
        DatabaseSettings(uri="postgresql+asyncpg://u:p@localhost:5432/apex"),
    )

    assert "connect_args" not in kwargs


def test_get_engine_respects_explicit_disabled_ssl_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _captured_engine_kwargs(
        monkeypatch,
        DatabaseSettings(
            uri="postgresql+asyncpg://u:p@db.example.com:5432/apex",
            ssl_mode="disable",
        ),
    )

    assert "connect_args" not in kwargs


async def test_release_read_transactions_deduplicates_wrapped_session() -> None:
    session = AsyncSession(expire_on_commit=False)

    class Repository:
        def __init__(self) -> None:
            self._session = session

    class Service:
        def __init__(self) -> None:
            self._store = Repository()

    try:
        await session.begin()
        assert session.in_transaction()

        await db.release_read_transactions(Repository(), Service(), session)

        assert not session.in_transaction()
    finally:
        await session.close()


async def test_release_read_transactions_never_commits_an_untracked_write() -> None:
    class TrackingSession(AsyncSession):
        def __init__(self) -> None:
            super().__init__(expire_on_commit=False)
            self.rollback_called = False

        def in_transaction(self) -> bool:
            return True

        async def rollback(self) -> None:
            self.rollback_called = True

        async def commit(self) -> None:
            raise AssertionError("a read-release boundary must never commit")

    session = TrackingSession()
    try:
        await db.release_read_transactions(session)

        assert session.rollback_called is True
    finally:
        await session.close()


async def test_release_read_transactions_rejects_pending_mutation() -> None:
    session = AsyncSession(expire_on_commit=False)
    try:
        session.add(
            ApiConsumer(
                id="pending",
                name="pending",
                key_hash="a" * 64,
                consumer_type="headless",
                role="viewer",
                enabled=True,
            )
        )

        with pytest.raises(RuntimeError, match="pending mutations"):
            await db.release_read_transactions(session)
    finally:
        await session.rollback()
        await session.close()


async def test_dispose_engine_closes_pool_and_resets_cached_factories() -> None:
    class Engine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    db._engine = cast(AsyncEngine, engine)
    db._sessionmaker = cast(async_sessionmaker[AsyncSession], object())

    await db.dispose_engine()

    assert engine.disposed is True
    assert db._engine is None
    assert db._sessionmaker is None
