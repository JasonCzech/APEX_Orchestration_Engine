from collections.abc import Iterator

import pytest

from apex.settings import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    """Settings are cached process-wide; isolate tests from each other and local .env."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
