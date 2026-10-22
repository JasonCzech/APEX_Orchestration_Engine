"""Keep explicit application schemas stable during PostgreSQL reflection."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, text

NORMALIZED_SEARCH_PATH_SQL = "SET LOCAL search_path TO public"


@contextmanager
def migration_schema_scope(connection: Connection) -> Iterator[None]:
    """Normalize reflection without changing the caller's connection policy.

    PostgreSQL omits FK target schemas found on search_path. Alembic separately
    normalizes the dialect's cached default schema to None. Both must agree on
    a fixed default outside ``apex``; changing only search_path leaves an engine
    first connected as the ``apex`` role with a stale default-schema cache.
    """
    if connection.dialect.name != "postgresql":
        yield
        return

    previous_path = connection.scalar(text("SHOW search_path"))
    previous_default = connection.dialect.default_schema_name
    original_error: BaseException | None = None
    try:
        connection.execute(text(NORMALIZED_SEARCH_PATH_SQL))
        connection.dialect.default_schema_name = "public"
        yield
    except BaseException as exc:
        original_error = exc
        raise
    finally:
        try:
            connection.execute(
                text("SELECT pg_catalog.set_config('search_path', :search_path, true)"),
                {"search_path": previous_path},
            )
        except BaseException:
            # A failed PostgreSQL statement aborts its transaction, so even
            # restoring search_path then fails. Preserve the migration error;
            # rollback restores SET LOCAL without committing caller-owned work.
            if original_error is None:
                raise
        finally:
            connection.dialect.default_schema_name = previous_default
