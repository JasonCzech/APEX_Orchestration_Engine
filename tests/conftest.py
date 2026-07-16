from collections.abc import Iterator

import pytest

import apex.auth.service as auth_service
from apex.auth.service import IdentityResolver
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

    async def no_persisted_dev_key_collision(_api_key: str) -> bool:
        # Unit tests deliberately have no authoritative credential store. Model
        # that verified-empty state only for the synthetic dev-key shortcut;
        # ordinary API keys still hit the unreachable DB and fail closed.
        return False

    monkeypatch.setenv("APEX_DATABASE__URI", UNREACHABLE_DB)
    monkeypatch.setattr(
        auth_service,
        "default_resolver",
        IdentityResolver(dev_key_collision_check=no_persisted_dev_key_collision),
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
