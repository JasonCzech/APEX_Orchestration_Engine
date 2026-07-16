"""Compatibility-aware production migration entry point."""

from __future__ import annotations

import asyncio
import os
import sys
from importlib.resources import files
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from apex.persistence.database_role_claims import (
    DATABASE_ROLE_LOCK_KEY,
    claim_context_from_environment,
    verify_database_role_claims,
)
from apex.persistence.db import dispose_engine_instance_definitively
from apex.persistence.schema_readiness import SchemaNotReadyError, validate_schema_head
from apex.settings import (
    database_asyncpg_uri,
    database_ssl_connect_args,
    database_uri_has_safe_transport,
    get_settings,
)

_DATABASE_ROLE_CLAIM_ENV_NAMES = (
    "APEX_DATABASE_ROLE_CLAIM_KEY",
    "APEX_DATABASE_ROLE_OWNER_ID",
    "APEX_RUNTIME_OWNER_ROLE",
    "APEX_MIGRATION_OWNER_ROLE",
    "APEX_RUNTIME_DATABASE_URI",
    "APEX_MIGRATION_DATABASE_URI",
)


async def _schema_is_compatible_async(database_uri: str | None = None) -> bool:
    """Check with a one-shot engine so separate CLI event loops never share a pool."""

    database = get_settings().database
    uri = database.uri if database_uri is None else database_uri
    if not database_uri_has_safe_transport(uri, database.ssl_mode):
        raise ValueError("database transport must authenticate every remote server")
    engine = create_async_engine(
        database_asyncpg_uri(uri),
        poolclass=NullPool,
        connect_args=database_ssl_connect_args(uri, database.ssl_mode),
    )
    try:
        await validate_schema_head(engine)
    except SchemaNotReadyError:
        return False
    finally:
        await dispose_engine_instance_definitively(engine)
    return True


def _schema_is_compatible() -> bool:
    return asyncio.run(_schema_is_compatible_async())


class _OwnershipVerificationFailed(RuntimeError):
    pass


class _ClaimedCompatibilityCheckFailed(RuntimeError):
    pass


class _ClaimedUpgradeFailed(RuntimeError):
    pass


def _upgrade_to_packaged_head(connection: Connection | None = None) -> None:
    config = Config()
    config.set_main_option(
        "script_location",
        str(files("apex.persistence") / "migrations"),
    )
    if connection is not None:
        # migrations/env.py consumes this exact synchronous facade rather than
        # opening another engine. The session-level role-claim lock therefore
        # remains held on the DDL connection throughout the upgrade.
        config.attributes["connection"] = connection
    command.upgrade(config, "head")


async def _run_claimed_migration() -> tuple[bool, bool]:
    """Verify, migrate, and revalidate while one DB session owns the role lock."""

    setup_failed = False
    context = None
    engine = None
    try:
        context = claim_context_from_environment()
        database = get_settings().database
        if not database_uri_has_safe_transport(context.migration_uri, database.ssl_mode):
            raise ValueError("database transport must authenticate every remote server")
        engine = create_async_engine(
            database_asyncpg_uri(context.migration_uri),
            poolclass=NullPool,
            connect_args=database_ssl_connect_args(context.migration_uri, database.ssl_mode),
        )
    except Exception:
        setup_failed = True
    if setup_failed:
        raise _OwnershipVerificationFailed
    assert context is not None
    assert engine is not None
    claims_verified = False
    terminal_failure: type[RuntimeError] | None = None
    outcome: tuple[bool, bool] | None = None
    try:
        async with engine.connect() as connection:
            raw_connection = await connection.get_raw_connection()
            driver_connection: Any = raw_connection.driver_connection
            await driver_connection.execute(
                "SELECT pg_advisory_lock($1::bigint)",
                DATABASE_ROLE_LOCK_KEY,
            )
            await verify_database_role_claims(driver_connection, context)
            claims_verified = True

            compatibility_failed = False
            try:
                initially_compatible = await _schema_is_compatible_async(context.migration_uri)
            except Exception:
                compatibility_failed = True
                initially_compatible = False
            if compatibility_failed:
                raise _ClaimedCompatibilityCheckFailed
            if initially_compatible:
                outcome = (True, True)
            else:
                upgrade_failed = False
                try:
                    await connection.run_sync(_upgrade_to_packaged_head)
                    await connection.commit()
                except Exception:
                    upgrade_failed = True
                if upgrade_failed:
                    raise _ClaimedUpgradeFailed

                compatibility_failed = False
                try:
                    finally_compatible = await _schema_is_compatible_async(context.migration_uri)
                except Exception:
                    compatibility_failed = True
                    finally_compatible = False
                if compatibility_failed:
                    raise _ClaimedCompatibilityCheckFailed
                outcome = (False, finally_compatible)
    except (_ClaimedCompatibilityCheckFailed, _ClaimedUpgradeFailed):
        raise
    except Exception:
        if claims_verified:
            terminal_failure = _ClaimedUpgradeFailed
        else:
            terminal_failure = _OwnershipVerificationFailed
    finally:
        dispose_failed = False
        try:
            await dispose_engine_instance_definitively(engine)
        except Exception:
            dispose_failed = True
        if dispose_failed:
            raise _ClaimedUpgradeFailed
    if terminal_failure is not None:
        raise terminal_failure
    if outcome is None:
        raise AssertionError("claimed migration ended without an outcome")
    return outcome


def main() -> int:
    """Upgrade behind schemas while treating a proven newer schema as a no-op."""

    if any(name in os.environ for name in _DATABASE_ROLE_CLAIM_ENV_NAMES):
        try:
            initially_compatible, finally_compatible = asyncio.run(_run_claimed_migration())
        except _OwnershipVerificationFailed:
            print("APEX database ownership verification failed.", file=sys.stderr)
            return 1
        except _ClaimedCompatibilityCheckFailed:
            print("APEX database schema compatibility check failed.", file=sys.stderr)
            return 1
        except _ClaimedUpgradeFailed:
            print("APEX database migration failed.", file=sys.stderr)
            return 1
        if initially_compatible:
            print("APEX database schema is already compatible; no migration is required.")
            return 0
        if not finally_compatible:
            print(
                "APEX database migration completed but schema compatibility could not be proven.",
                file=sys.stderr,
            )
            return 1
        print("APEX database schema migrated to the packaged head.")
        return 0

    try:
        compatible = _schema_is_compatible()
    except Exception:
        print("APEX database schema compatibility check failed.", file=sys.stderr)
        return 1
    if compatible:
        print("APEX database schema is already compatible; no migration is required.")
        return 0

    try:
        _upgrade_to_packaged_head()
    except Exception:
        # Do not echo driver exceptions here: deployment logs must not expose a
        # database URI or credentials. Alembic/SQLAlchemy still log diagnostics
        # through their configured logger before this boundary when available.
        print("APEX database migration failed.", file=sys.stderr)
        return 1

    try:
        compatible = _schema_is_compatible()
    except Exception:
        print("APEX database schema compatibility check failed.", file=sys.stderr)
        return 1
    if not compatible:
        print(
            "APEX database migration completed but schema compatibility could not be proven.",
            file=sys.stderr,
        )
        return 1

    print("APEX database schema migrated to the packaged head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
