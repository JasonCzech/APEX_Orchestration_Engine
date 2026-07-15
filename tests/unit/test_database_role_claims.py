"""Adversarial verification of deploy-hook database ownership claims."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from apex.persistence.database_role_claims import (
    DatabaseRoleClaimContext,
    DatabaseRoleClaimError,
    claim_context_from_environment,
    verify_database_role_claims,
)


class FakeClaimConnection:
    def __init__(
        self,
        *,
        roles: dict[str, dict[str, Any]],
        memberships: dict[str, set[str]],
        schema: dict[str, Any] | None,
    ) -> None:
        self.roles = roles
        self.memberships = memberships
        self.schema = schema

    async def execute(self, query: str, *args: object) -> None:
        return None

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        if "FROM pg_namespace" in query:
            return self.schema
        if "FROM pg_roles" in query and "WHERE rolname = $1" in query:
            return self.roles.get(str(args[0]))
        raise AssertionError(f"unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *args: object) -> list[dict[str, str]]:
        role_name = str(args[0])
        if "WHERE parent.rolname = $1" in query:
            return [{"rolname": child} for child in sorted(self.memberships.get(role_name, set()))]
        if "WHERE member.rolname = $1" in query:
            return [
                {"rolname": parent}
                for parent, children in sorted(self.memberships.items())
                if role_name in children
            ]
        raise AssertionError(f"unexpected fetch query: {query}")


def _context(*, key: bytes = b"k" * 64) -> DatabaseRoleClaimContext:
    return DatabaseRoleClaimContext(
        owner_id="apex-system/apex",
        claim_key=key,
        runtime_owner="apex_runtime",
        migration_owner="apex_migration",
        runtime_login="apex_runtime_vcurrent",
        migration_login="apex_migration_vcurrent",
        migration_uri="postgresql+asyncpg://migration:secret@db.internal/apex",
    )


def _safe_role(*, login: bool, marker: str) -> dict[str, Any]:
    return {
        "rolsuper": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolreplication": False,
        "rolbypassrls": False,
        "rolinherit": True,
        "rolcanlogin": login,
        "rolconnlimit": -1,
        "rolvaliduntil": None,
        "marker": marker,
    }


def _valid_state() -> tuple[
    DatabaseRoleClaimContext,
    dict[str, dict[str, Any]],
    dict[str, set[str]],
    dict[str, Any],
]:
    context = _context()
    roles = {
        context.runtime_owner: _safe_role(
            login=False,
            marker=context.marker("runtime-owner", context.runtime_owner),
        ),
        context.migration_owner: _safe_role(
            login=False,
            marker=context.marker("migration-owner", context.migration_owner),
        ),
        context.runtime_login: _safe_role(
            login=True,
            marker=context.marker("runtime-generation", context.runtime_login),
        ),
        context.migration_login: _safe_role(
            login=True,
            marker=context.marker("migration-generation", context.migration_login),
        ),
        "apex_runtime_vstale": _safe_role(
            login=True,
            marker=context.marker("runtime-generation", "apex_runtime_vstale"),
        ),
    }
    memberships = {
        context.runtime_owner: {context.runtime_login, "apex_runtime_vstale"},
        context.migration_owner: {context.migration_login},
    }
    schema = {
        "owner_name": context.migration_owner,
        "marker": context.marker("schema", "apex"),
    }
    return context, roles, memberships, schema


async def test_database_role_claims_accept_only_exact_direct_claims() -> None:
    context, roles, memberships, schema = _valid_state()

    await verify_database_role_claims(
        FakeClaimConnection(
            roles=roles,
            memberships=memberships,
            schema=schema,
        ),
        context,
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("forged_schema_comment", "schema claim"),
        ("foreign_schema_owner", "schema claim"),
        ("stale_generation_comment", "unsafe or forged"),
        ("transitive_generation_member", "unexpected members"),
        ("current_not_direct", "direct owner member"),
    ],
)
async def test_database_role_claims_reject_adversarial_state(
    mutation: str,
    message: str,
) -> None:
    context, original_roles, original_memberships, original_schema = _valid_state()
    roles = deepcopy(original_roles)
    memberships = deepcopy(original_memberships)
    schema = deepcopy(original_schema)

    if mutation == "forged_schema_comment":
        schema["marker"] = "apex-role-claim-v2:apex-system/apex:schema:forged"
    elif mutation == "foreign_schema_owner":
        schema["owner_name"] = "unrelated_owner"
    elif mutation == "stale_generation_comment":
        roles["apex_runtime_vstale"]["marker"] = context.marker(
            "runtime-generation",
            "apex_runtime_vdifferent",
        )
    elif mutation == "transitive_generation_member":
        memberships[context.runtime_login] = {"unrelated_login"}
    elif mutation == "current_not_direct":
        memberships[context.runtime_owner].remove(context.runtime_login)
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(mutation)

    with pytest.raises(DatabaseRoleClaimError, match=message):
        await verify_database_role_claims(
            FakeClaimConnection(
                roles=roles,
                memberships=memberships,
                schema=schema,
            ),
            context,
        )


def test_claim_context_requires_dedicated_stable_key_and_one_database() -> None:
    base = {
        "APEX_DATABASE_ROLE_CLAIM_KEY": "x" * 64,
        "APEX_DATABASE_ROLE_OWNER_ID": "apex-system/apex",
        "APEX_RUNTIME_OWNER_ROLE": "apex_runtime",
        "APEX_MIGRATION_OWNER_ROLE": "apex_migration",
        "APEX_RUNTIME_DATABASE_URI": (
            "postgresql+asyncpg://apex_runtime_vcurrent:runtime-secret@db.internal/apex"
        ),
        "APEX_MIGRATION_DATABASE_URI": (
            "postgresql+asyncpg://apex_migration_vcurrent:migration-secret@db.internal/apex"
        ),
    }

    context = claim_context_from_environment(base)
    assert context.runtime_login == "apex_runtime_vcurrent"

    for mutation in (
        {"APEX_DATABASE_ROLE_CLAIM_KEY": "short"},
        {"APEX_DATABASE_ROLE_OWNER_ID": "apex-system:apex"},
        {
            "APEX_MIGRATION_DATABASE_URI": (
                "postgresql+asyncpg://apex_migration_vcurrent:migration-secret@other/apex"
            )
        },
    ):
        invalid = base | mutation
        with pytest.raises(DatabaseRoleClaimError):
            claim_context_from_environment(invalid)
