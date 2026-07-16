"""Credential-bound PostgreSQL role/schema claim verification for deploy hooks."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

import asyncpg

from apex.settings import (
    database_asyncpg_uri,
    database_ssl_connect_args,
    database_uri_has_safe_transport,
)

CLAIM_PREFIX = "apex-role-claim-v2"
DATABASE_ROLE_LOCK_KEY = 4706337856242535493
_ROLE_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class DatabaseRoleClaimError(RuntimeError):
    """A deploy hook can no longer prove its database trust boundary."""


class _Connection(Protocol):
    async def execute(self, query: str, *args: object) -> Any: ...

    async def fetch(self, query: str, *args: object) -> Sequence[Mapping[str, Any]]: ...

    async def fetchrow(self, query: str, *args: object) -> Mapping[str, Any] | None: ...


class _ClosableConnection(Protocol):
    async def close(self) -> Any: ...


async def _close_connection_definitively(connection: _ClosableConnection) -> None:
    """Settle one privileged connection close despite repeated cancellation."""

    close_task = asyncio.create_task(connection.close())
    interrupted = False
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            interrupted = True
        except BaseException:
            break
    error: BaseException | None = None
    try:
        close_task.result()
    except BaseException as exc:
        # Retrieve the child outcome before preserving caller cancellation.
        error = exc
    if interrupted:
        raise asyncio.CancelledError from None
    if error is not None:
        raise error


@dataclass(frozen=True, slots=True)
class DatabaseRoleClaimContext:
    owner_id: str
    claim_key: bytes
    runtime_owner: str
    migration_owner: str
    runtime_login: str
    migration_login: str
    migration_uri: str

    def marker(self, kind: str, name: str) -> str:
        payload = f"{CLAIM_PREFIX}:{self.owner_id}:{kind}:{name}".encode()
        digest = hmac.new(self.claim_key, payload, hashlib.sha256).hexdigest()
        return f"{CLAIM_PREFIX}:{self.owner_id}:{kind}:{digest}"


def claim_context_from_environment(
    environment: Mapping[str, str] | None = None,
) -> DatabaseRoleClaimContext:
    """Build and validate the short-lived claim context used by one hook."""

    env = os.environ if environment is None else environment
    raw_key = env.get("APEX_DATABASE_ROLE_CLAIM_KEY")
    owner_id = env.get("APEX_DATABASE_ROLE_OWNER_ID")
    runtime_owner = env.get("APEX_RUNTIME_OWNER_ROLE")
    migration_owner = env.get("APEX_MIGRATION_OWNER_ROLE")
    runtime_uri = env.get("APEX_RUNTIME_DATABASE_URI")
    migration_uri = env.get("APEX_MIGRATION_DATABASE_URI") or env.get("APEX_DATABASE__URI")
    if any(
        value is None
        for value in (
            raw_key,
            owner_id,
            runtime_owner,
            migration_owner,
            runtime_uri,
            migration_uri,
        )
    ):
        raise DatabaseRoleClaimError("database role claim configuration is incomplete")
    assert raw_key is not None
    assert owner_id is not None
    assert runtime_owner is not None
    assert migration_owner is not None
    assert runtime_uri is not None
    assert migration_uri is not None

    claim_key: bytes | None = None
    try:
        claim_key = raw_key.encode("utf-8")
    except UnicodeEncodeError:
        pass
    if claim_key is None or len(claim_key) < 32 or raw_key != raw_key.strip():
        raise DatabaseRoleClaimError(
            "database role claim key must be a trimmed value of at least 32 bytes"
        )
    if (
        not owner_id
        or owner_id.count("/") != 1
        or ":" in owner_id
        or any(ord(character) < 0x21 for character in owner_id)
    ):
        raise DatabaseRoleClaimError("database role owner id is invalid")
    for label, role_name in (
        ("runtime owner", runtime_owner),
        ("migration owner", migration_owner),
    ):
        if _ROLE_PATTERN.fullmatch(role_name) is None:
            raise DatabaseRoleClaimError(f"{label} is not a valid PostgreSQL role name")
    if runtime_owner == migration_owner:
        raise DatabaseRoleClaimError("runtime and migration owners must be distinct")

    runtime_login, runtime_target = _uri_identity("runtime", runtime_uri)
    migration_login, migration_target = _uri_identity("migration", migration_uri)
    if not database_uri_has_safe_transport(
        runtime_uri, None
    ) or not database_uri_has_safe_transport(migration_uri, None):
        raise DatabaseRoleClaimError("database role URI transport is unsafe")
    if runtime_target != migration_target:
        raise DatabaseRoleClaimError(
            "runtime and migration credentials target different database endpoints"
        )
    if not runtime_login.startswith(f"{runtime_owner}_v"):
        raise DatabaseRoleClaimError("runtime login is not an owner generation")
    if not migration_login.startswith(f"{migration_owner}_v"):
        raise DatabaseRoleClaimError("migration login is not an owner generation")

    return DatabaseRoleClaimContext(
        owner_id=owner_id,
        claim_key=claim_key,
        runtime_owner=runtime_owner,
        migration_owner=migration_owner,
        runtime_login=runtime_login,
        migration_login=migration_login,
        migration_uri=migration_uri,
    )


def _uri_identity(label: str, uri: str) -> tuple[str, tuple[str, int, str]]:
    parsed = None
    username = ""
    database = ""
    hostname = ""
    port = 0
    try:
        parsed = urlsplit(uri)
        username = unquote(parsed.username or "")
        # Match asyncpg exactly: it removes one URI path separator, not every
        # leading/trailing slash. Otherwise `/apex` and `/apex/` compare equal
        # here while selecting distinct PostgreSQL database names.
        database_path = parsed.path[1:] if parsed.path.startswith("/") else parsed.path
        database = unquote(database_path)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        parsed_port = parsed.port
        port = 5432 if parsed_port is None else parsed_port
    except (ValueError, UnicodeError):
        parsed = None
    if parsed is None:
        raise DatabaseRoleClaimError(f"{label} database URI is invalid")
    if (
        not username
        or _ROLE_PATTERN.fullmatch(username) is None
        or parsed.password is None
        or not hostname
        or not database
        or not 1 <= port <= 65_535
    ):
        raise DatabaseRoleClaimError(f"{label} database URI is incomplete")
    return username, (hostname, port, database)


async def verify_database_role_claims(
    connection: _Connection,
    context: DatabaseRoleClaimContext,
) -> None:
    """Fail closed unless every direct managed role and schema claim is exact."""

    owner_members: dict[str, set[str]] = {}
    for role_kind, owner_name, generation_kind, expected_prefix in (
        (
            "runtime-owner",
            context.runtime_owner,
            "runtime-generation",
            f"{context.runtime_owner}_v",
        ),
        (
            "migration-owner",
            context.migration_owner,
            "migration-generation",
            f"{context.migration_owner}_v",
        ),
    ):
        await _verify_role(
            connection,
            context,
            owner_name,
            kind=role_kind,
            login=False,
            expected_parents=set(),
            require_no_children=False,
        )
        members = await _children(connection, owner_name)
        if any(not member.startswith(expected_prefix) for member in members):
            raise DatabaseRoleClaimError("database owner has an unexpected direct member")
        for member_name in sorted(members):
            await _verify_role(
                connection,
                context,
                member_name,
                kind=generation_kind,
                login=True,
                expected_parents={owner_name},
                require_no_children=True,
            )
        owner_members[owner_name] = members

    if context.runtime_login not in owner_members[context.runtime_owner]:
        raise DatabaseRoleClaimError("runtime login is not a direct owner member")
    if context.migration_login not in owner_members[context.migration_owner]:
        raise DatabaseRoleClaimError("migration login is not a direct owner member")

    schema = await connection.fetchrow(
        """
        SELECT owner.rolname AS owner_name,
               obj_description(n.oid, 'pg_namespace') AS marker
        FROM pg_namespace AS n
        JOIN pg_roles AS owner ON owner.oid = n.nspowner
        WHERE n.nspname = 'apex'
        """
    )
    if (
        schema is None
        or schema["owner_name"] != context.migration_owner
        or not hmac.compare_digest(
            str(schema["marker"] or ""),
            context.marker("schema", "apex"),
        )
    ):
        raise DatabaseRoleClaimError("application schema claim is invalid")


async def _verify_role(
    connection: _Connection,
    context: DatabaseRoleClaimContext,
    role_name: str,
    *,
    kind: str,
    login: bool,
    expected_parents: set[str],
    require_no_children: bool,
) -> None:
    state = await connection.fetchrow(
        """
        SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication,
               rolbypassrls, rolinherit, rolcanlogin, rolconnlimit,
               rolvaliduntil, shobj_description(oid, 'pg_authid') AS marker
        FROM pg_roles
        WHERE rolname = $1
        """,
        role_name,
    )
    if state is None:
        raise DatabaseRoleClaimError("a claimed database role is absent")
    dangerous = any(
        bool(state[key])
        for key in (
            "rolsuper",
            "rolcreaterole",
            "rolcreatedb",
            "rolreplication",
            "rolbypassrls",
        )
    )
    if (
        dangerous
        or not bool(state["rolinherit"])
        or bool(state["rolcanlogin"]) != login
        or state["rolconnlimit"] != -1
        or state["rolvaliduntil"] is not None
        or not hmac.compare_digest(
            str(state["marker"] or ""),
            context.marker(kind, role_name),
        )
    ):
        raise DatabaseRoleClaimError("a claimed database role is unsafe or forged")
    if await _parents(connection, role_name) != expected_parents:
        raise DatabaseRoleClaimError("a claimed database role has unexpected parents")
    if require_no_children and await _children(connection, role_name):
        raise DatabaseRoleClaimError("a claimed login role has unexpected members")


async def _parents(connection: _Connection, role_name: str) -> set[str]:
    rows = await connection.fetch(
        """
        SELECT parent.rolname, membership.admin_option AS admin_option
        FROM pg_auth_members AS membership
        JOIN pg_roles AS parent ON parent.oid = membership.roleid
        JOIN pg_roles AS member ON member.oid = membership.member
        WHERE member.rolname = $1
        """,
        role_name,
    )
    if any(bool(row["admin_option"]) for row in rows):
        raise DatabaseRoleClaimError(
            "a claimed database role has an administrable parent membership"
        )
    return {str(row["rolname"]) for row in rows}


async def _children(connection: _Connection, role_name: str) -> set[str]:
    rows = await connection.fetch(
        """
        SELECT member.rolname
        FROM pg_auth_members AS membership
        JOIN pg_roles AS parent ON parent.oid = membership.roleid
        JOIN pg_roles AS member ON member.oid = membership.member
        WHERE parent.rolname = $1
        """,
        role_name,
    )
    return {str(row["rolname"]) for row in rows}


async def verify_database_role_claims_from_environment(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Verify claims over the migration credential immediately before a hook."""

    context = claim_context_from_environment(environment)
    uri = database_asyncpg_uri(context.migration_uri).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    connect_args: dict[str, Any] = database_ssl_connect_args(context.migration_uri, None)
    connection = await asyncpg.connect(
        uri,
        **connect_args,
    )
    try:
        await connection.execute(
            "SELECT pg_advisory_lock($1::bigint)",
            DATABASE_ROLE_LOCK_KEY,
        )
        await verify_database_role_claims(connection, context)
    finally:
        await _close_connection_definitively(connection)


def main() -> int:
    try:
        asyncio.run(verify_database_role_claims_from_environment())
    except Exception:
        # Driver exceptions may contain credentials or endpoints. The hook's
        # exit status is authoritative; keep its public deployment log opaque.
        print("APEX database ownership verification failed.", file=sys.stderr)
        return 1
    print("APEX database ownership claims verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
