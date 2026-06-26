from collections.abc import Iterator
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from apex.persistence import db
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
