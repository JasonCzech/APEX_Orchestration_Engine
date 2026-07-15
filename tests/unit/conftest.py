"""Unit-only isolation for process startup dependencies."""

import pytest


@pytest.fixture(autouse=True)
def assume_database_schema_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep HTTP unit tests hermetic; schema behavior has dedicated tests."""

    async def schema_ready() -> None:
        return None

    monkeypatch.setattr("apex.app.lifespan.validate_schema_head", schema_ready)
