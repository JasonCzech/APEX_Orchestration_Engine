"""Fail-closed startup validation for the application-owned database schema."""

import asyncio
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from apex.persistence.db import get_engine
from apex.persistence.migration_lineage import (
    LINEAGE_TABLE,
    database_heads_descend_from_packaged_heads,
    packaged_revision_lineage,
    revision_graph,
)

APEX_VERSION_QUERY = text("SELECT version_num FROM apex.alembic_version")
APEX_LINEAGE_QUERY = text(f"SELECT revision_num, parent_revision_num FROM {LINEAGE_TABLE}")


class SchemaNotReadyError(RuntimeError):
    """Raised when the database is unreachable or schema compatibility is unproven."""


@lru_cache
def packaged_schema_scripts() -> ScriptDirectory:
    migration_dir = Path(__file__).resolve().parent / "migrations"
    return ScriptDirectory(str(migration_dir))


@lru_cache
def packaged_schema_heads() -> frozenset[str]:
    """Return every Alembic head bundled with this application image."""

    heads = frozenset(packaged_schema_scripts().get_heads())
    if not heads:
        raise SchemaNotReadyError("the application image contains no Alembic schema head")
    return heads


async def validate_schema_head(engine: Any | None = None) -> None:
    """Prove DB connectivity and require an exact or trusted descendant schema.

    This deliberately runs before background reconcilers start or the FastAPI
    lifespan yields. Consequently LangGraph's `/ok` probe cannot report a pod
    ready when the APEX schema is absent or stale.
    """

    expected = packaged_schema_heads()
    packaged_graph = revision_graph(packaged_revision_lineage(packaged_schema_scripts()))
    schema_error = False
    try:
        database = engine or get_engine()
        async with database.connect() as connection:
            result = await connection.execute(APEX_VERSION_QUERY)
            current = frozenset(str(value) for value in result.scalars().all())
            lineage_result = await connection.execute(APEX_LINEAGE_QUERY)
            database_graph = revision_graph(
                (str(row[0]), str(row[1])) for row in lineage_result.all()
            )
    except (OSError, SQLAlchemyError, TypeError, ValueError):
        schema_error = True
        current = frozenset()
        database_graph = {}
    if schema_error:
        raise SchemaNotReadyError(
            "APEX database schema is unavailable; run `alembic upgrade head` before rollout"
        )

    if not database_heads_descend_from_packaged_heads(
        current_heads=current,
        packaged_heads=expected,
        database_graph=database_graph,
        packaged_graph=packaged_graph,
    ):
        expected_label = ",".join(sorted(expected))
        raise SchemaNotReadyError(
            f"APEX database schema is incompatible; packaged head is {expected_label}, "
            "but trusted descendant lineage could not be proven. Run `alembic upgrade head` "
            "from a trusted application image before rollout."
        )


def main() -> int:
    """CLI used by the Helm init container without exposing DB exception details."""

    try:
        asyncio.run(validate_schema_head())
    except SchemaNotReadyError as exc:
        print(f"Schema readiness failed: {exc}", file=sys.stderr)
        return 1
    print("APEX schema is compatible with the packaged Alembic head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
