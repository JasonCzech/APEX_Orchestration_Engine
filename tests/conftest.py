from collections.abc import Iterator

import pytest

from apex.settings import get_settings

# Unit tests must be hermetic: the dev Postgres (if running) may hold seeded
# connections (e.g. the MinIO artifact store) that would otherwise hijack
# adapter resolution. An unreachable DB URI makes every DB-backed lookup fail
# fast and fall back to the deterministic static stubs. DB-touching integration
# tests opt in explicitly via APEX_TEST_DATABASE_URI.
UNREACHABLE_DB = "postgresql+asyncpg://apex:apex@127.0.0.1:1/apex"


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate tests from local .env, the settings cache, and the dev database."""
    monkeypatch.setenv("APEX_DATABASE__URI", UNREACHABLE_DB)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
