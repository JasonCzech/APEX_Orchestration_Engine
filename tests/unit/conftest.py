"""Unit-only isolation for process startup dependencies."""

import pytest


@pytest.fixture(autouse=True)
def assume_database_schema_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep HTTP unit tests hermetic; schema behavior has dedicated tests."""

    async def schema_ready() -> None:
        return None

    monkeypatch.setattr("apex.app.lifespan.validate_schema_head", schema_ready)


@pytest.fixture(autouse=True)
def isolate_pricing_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pricing services have database-backed tests; HTTP startup is hermetic."""

    async def ready() -> None:
        pass

    async def worker(stop, heartbeat=None) -> None:
        if heartbeat:
            heartbeat()
        await stop.wait()

    monkeypatch.setattr("apex.services.model_pricing.initialize_pricing", ready)
    monkeypatch.setattr("apex.services.model_pricing.run_pricing_worker", worker)
