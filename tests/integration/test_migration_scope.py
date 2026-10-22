"""Schema normalization fixes CI reflection without concealing real FK drift."""

import os
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Column, Connection, ForeignKeyConstraint, Integer, MetaData, Table, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import AddConstraint, DropConstraint

from apex.persistence.migration_scope import migration_schema_scope

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


def _engine():
    # Reproduce the initialized dialect cache in CI, where login role and schema
    # are both apex and PostgreSQL's default search_path starts with "$user".
    return create_async_engine(
        os.environ["APEX_TEST_DATABASE_URI"],
        connect_args={"server_settings": {"search_path": "apex, public"}},
    )


async def test_apex_default_schema_reflection_retains_real_foreign_key_drift() -> None:
    engine = _engine()
    try:
        async with engine.connect() as connection:
            try:
                await connection.run_sync(_verify_foreign_key_drift)
            finally:
                # All probe DDL, including public-schema tables, is transactional.
                await connection.rollback()
    finally:
        await engine.dispose()


def _verify_foreign_key_drift(connection: Connection) -> None:
    assert connection.dialect.default_schema_name == "apex"
    original_path = connection.scalar(text("SHOW search_path"))
    prefix = f"migration_scope_{uuid4().hex[:12]}"
    parent_name, child_name, fk_name = f"{prefix}_parent", f"{prefix}_child", f"{prefix}_fk"
    model = MetaData()
    parent = Table(parent_name, model, Column("id", Integer, primary_key=True), schema="apex")
    child = Table(
        child_name,
        model,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer),
        ForeignKeyConstraint(["parent_id"], [parent.c.id], name=fk_name, ondelete="CASCADE"),
        schema="apex",
    )
    foreign_key = next(iter(child.foreign_key_constraints))
    model.create_all(connection)
    other_schema = MetaData()
    Table(parent_name, other_schema, Column("id", Integer, primary_key=True), schema="public")
    other_schema.create_all(connection)

    def differences():
        migration = MigrationContext.configure(
            connection,
            opts={
                "include_schemas": True,
                "include_name": lambda name, kind, _parents: (
                    kind != "table" or name in {parent_name, child_name}
                ),
                # Match env.py's explicit apex-table filter, restricting this
                # probe to its own tables instead of the application's metadata.
                "include_object": lambda obj, name, kind, _reflected, _compare_to: (
                    kind != "table" or (obj.schema == "apex" and name in {parent_name, child_name})
                ),
            },
        )
        return compare_metadata(migration, model)

    def replace_foreign_key(schema: str, ondelete: str) -> None:
        connection.execute(DropConstraint(foreign_key))
        replacement_metadata = MetaData()
        replacement_parent = Table(
            parent_name,
            replacement_metadata,
            Column("id", Integer, primary_key=True),
            schema=schema,
        )
        replacement_child = Table(
            child_name,
            replacement_metadata,
            Column("id", Integer, primary_key=True),
            Column("parent_id", Integer),
            ForeignKeyConstraint(
                ["parent_id"], [replacement_parent.c.id], name=fk_name, ondelete=ondelete
            ),
            schema="apex",
        )
        connection.execute(AddConstraint(next(iter(replacement_child.foreign_key_constraints))))

    with migration_schema_scope(connection):
        assert connection.dialect.default_schema_name == "public"
        assert connection.scalar(text("SELECT current_schema()")) == "public"
        assert differences() == []

        # Keep the name, columns and cascade policy identical while changing
        # only the actual referenced schema. Blanket None->apex rewrites must
        # not turn this into a false successful check.
        replace_foreign_key("public", "CASCADE")
        drift = differences()
        assert {entry[0] for entry in drift} == {"remove_fk", "add_fk"}
        removed = next(entry[1] for entry in drift if entry[0] == "remove_fk")
        added = next(entry[1] for entry in drift if entry[0] == "add_fk")
        assert (removed.elements[0].column.table.schema or "public") == "public"
        assert added.elements[0].column.table.schema == "apex"

        replace_foreign_key("apex", "SET NULL")
        drift = differences()
        assert {entry[0] for entry in drift} == {"remove_fk", "add_fk"}
        assert next(entry[1].ondelete for entry in drift if entry[0] == "remove_fk") == "SET NULL"
        assert next(entry[1].ondelete for entry in drift if entry[0] == "add_fk") == "CASCADE"

        connection.execute(DropConstraint(foreign_key))
        assert [entry[0] for entry in differences()] == ["add_fk"]
        connection.execute(AddConstraint(foreign_key))
        assert differences() == []

    assert connection.dialect.default_schema_name == "apex"
    assert connection.scalar(text("SHOW search_path")) == original_path


async def test_migration_schema_scope_restores_caller_after_non_database_error() -> None:
    engine = _engine()
    try:
        async with engine.connect() as connection:

            def verify(sync_connection: Connection) -> None:
                original_path = sync_connection.scalar(text("SHOW search_path"))
                failure = RuntimeError("revision callback failed")
                with pytest.raises(RuntimeError) as caught:
                    with migration_schema_scope(sync_connection):
                        raise failure
                assert caught.value is failure
                assert sync_connection.dialect.default_schema_name == "apex"
                assert sync_connection.scalar(text("SHOW search_path")) == original_path

            await connection.run_sync(verify)
    finally:
        await engine.dispose()


async def test_failed_postgres_statement_preserves_original_error_and_rollback_path() -> None:
    engine = _engine()
    try:
        async with engine.connect() as connection:
            original_path = await connection.scalar(text("SHOW search_path"))

            def fail(sync_connection: Connection) -> None:
                with migration_schema_scope(sync_connection):
                    sync_connection.execute(text("SELECT 1 / 0"))

            with pytest.raises(DBAPIError) as caught:
                await connection.run_sync(fail)
            # Restoration fails with 25P02 in the aborted transaction; the
            # division-by-zero migration failure (22012) must remain primary.
            assert getattr(caught.value.orig, "sqlstate", None) == "22012"
            assert connection.dialect.default_schema_name == "apex"
            await connection.rollback()
            assert await connection.scalar(text("SHOW search_path")) == original_path
    finally:
        await engine.dispose()
