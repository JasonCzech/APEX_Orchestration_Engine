"""Compare legacy JSON defaults without hiding real schema drift."""

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, Column
from sqlalchemy.ext.asyncio import create_async_engine

from apex.persistence.migration_compare import compare_server_default

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


async def test_json_default_comparison_preserves_semantic_drift_detection() -> None:
    engine = create_async_engine(os.environ["APEX_TEST_DATABASE_URI"])
    try:
        async with engine.connect() as connection:

            def verify(sync_connection):
                context = SimpleNamespace(
                    dialect=sync_connection.dialect, connection=sync_connection
                )
                column = Column("snapshot", JSON())
                assert (
                    compare_server_default(
                        context,
                        column,
                        column,
                        "'{\"ready\":true}'::json",
                        None,
                        "'{ \"ready\": true }'",
                    )
                    is False
                )
                assert (
                    compare_server_default(
                        context,
                        column,
                        column,
                        "'{\"ready\":true}'::json",
                        None,
                        "'{\"ready\":false}'",
                    )
                    is True
                )

            await connection.run_sync(verify)
    finally:
        await engine.dispose()
