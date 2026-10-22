"""PostgreSQL-aware Alembic server-default comparison."""

from sqlalchemy import JSON, cast, literal_column, select
from sqlalchemy.dialects.postgresql import JSONB


def compare_server_default(
    migration_context,
    inspected_column,
    metadata_column,
    inspected_default,
    metadata_default,
    rendered_metadata_default,
) -> bool | None:
    # PostgreSQL JSON (unlike JSONB) has no equality operator. Alembic's default
    # comparison otherwise executes `json = json` and fails for the native-run
    # mirrors created by revision 0030. Compare their values as JSONB without
    # changing the published column types or suppressing actual default drift.
    if (
        migration_context.dialect.name == "postgresql"
        and isinstance(inspected_column.type, JSON)
        and not isinstance(inspected_column.type, JSONB)
        and isinstance(metadata_column.type, JSON)
        and inspected_default is not None
        and rendered_metadata_default is not None
    ):
        return bool(
            migration_context.connection.scalar(
                select(
                    cast(literal_column(inspected_default), JSONB).is_distinct_from(
                        cast(literal_column(rendered_metadata_default), JSONB)
                    )
                )
            )
        )
    return None
