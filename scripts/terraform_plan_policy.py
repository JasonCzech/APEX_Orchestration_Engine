"""Reject unsafe changes to stateful infrastructure in locked environments."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ENVIRONMENT_ALIASES = {
    "dev": "dev",
    "staging": "staging",
    "stage": "staging",
    "prod": "prod",
    "production": "prod",
}
LOCKED_ENVIRONMENTS = frozenset({"staging", "prod"})
SUPPORTED_PLAN_FORMAT_MAJOR = "1"
KNOWN_ACTIONS = frozenset({"no-op", "create", "read", "update", "delete", "forget"})
PROTECTED_RESOURCE_TYPES = frozenset(
    {
        "azurerm_key_vault",
        "azurerm_key_vault_secret",
        "azurerm_kubernetes_cluster",
        "azurerm_log_analytics_workspace",
        "azurerm_management_lock",
        "azurerm_postgresql_flexible_server",
        "azurerm_postgresql_flexible_server_database",
        "azurerm_redis_cache",
        "azurerm_resource_group",
        "azurerm_storage_account",
        "azurerm_storage_container",
        "azurerm_storage_management_policy",
    }
)
IMMUTABLE_UPDATE_ADDRESSES = frozenset(
    {
        "azurerm_key_vault_secret.artifact_secret_key",
        "azurerm_key_vault_secret.database_role_claim",
    }
)
IMMUTABLE_REPLACEMENT_ADDRESSES = frozenset(
    {
        "random_password.database_role_claim",
    }
)


def destructive_protected_changes(plan: dict[str, Any], environment: str) -> list[str]:
    """Return protected addresses that a saved plan would delete, forget, or replace.

    Values are intentionally never included: Terraform plan JSON can contain
    credentials, so policy output is limited to resource addresses and actions.
    """

    supplied_environment = environment.strip().lower()
    try:
        normalized_environment = ENVIRONMENT_ALIASES[supplied_environment]
    except KeyError:
        supported = ", ".join(sorted(ENVIRONMENT_ALIASES))
        raise ValueError(f"environment must be one of: {supported}") from None
    if normalized_environment not in LOCKED_ENVIRONMENTS:
        return []
    format_version = plan.get("format_version")
    if not isinstance(format_version, str) or format_version.partition(".")[0] != (
        SUPPORTED_PLAN_FORMAT_MAJOR
    ):
        raise ValueError("Terraform plan format_version is absent or unsupported")
    if "resource_changes" not in plan:
        raise ValueError("Terraform plan resource_changes is absent")
    violations: list[str] = []
    resource_changes = plan["resource_changes"]
    if not isinstance(resource_changes, list):
        raise ValueError("Terraform plan resource_changes must be a list")
    for resource in resource_changes:
        if not isinstance(resource, dict):
            raise ValueError("Terraform plan resource change must be an object")
        resource_type = resource.get("type")
        if not isinstance(resource_type, str):
            raise ValueError("Terraform plan resource type must be a string")
        change = resource.get("change")
        if not isinstance(change, dict):
            raise ValueError("Terraform plan change must be an object")
        actions = change.get("actions")
        if (
            not isinstance(actions, list)
            or not actions
            or not all(isinstance(action, str) for action in actions)
        ):
            raise ValueError("Terraform plan change actions must be a list of strings")
        if any(action not in KNOWN_ACTIONS for action in actions):
            raise ValueError("Terraform plan contains an unsupported change action")
        address = resource.get("address")
        if not isinstance(address, str):
            address = "<unknown-address>"
        destructive = resource_type in PROTECTED_RESOURCE_TYPES and bool(
            {"delete", "forget"}.intersection(actions)
        )
        # Terraform appends a final instance key for count/for_each resources and
        # may prepend indexed module paths. Match the resource address by complete
        # dot-delimited suffix after removing only that final instance key.
        unindexed_address = re.sub(r"\[[^\]\r\n]*\]$", "", address)
        immutable_update = "update" in actions and any(
            unindexed_address == value or unindexed_address.endswith(f".{value}")
            for value in IMMUTABLE_UPDATE_ADDRESSES
        )
        immutable_replacement = bool({"delete", "forget", "update"}.intersection(actions)) and any(
            unindexed_address == value or unindexed_address.endswith(f".{value}")
            for value in IMMUTABLE_REPLACEMENT_ADDRESSES
        )
        if destructive or immutable_update or immutable_replacement:
            violations.append(f"{address} ({'/'.join(actions)})")
    return sorted(violations)


def enforce_plan_policy(plan: dict[str, Any], environment: str) -> None:
    violations = destructive_protected_changes(plan, environment)
    if not violations:
        return
    rendered = "\n  - ".join(violations)
    raise ValueError(
        "saved Terraform plan mutates protected stateful resources:\n"
        f"  - {rendered}\n"
        "Use a separately reviewed break-glass change; ordinary deployment is blocked."
    )


def _load_plan(path: str) -> dict[str, Any]:
    if path == "-":
        value = json.load(sys.stdin)
    else:
        with Path(path).open(encoding="utf-8") as stream:
            value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("Terraform plan JSON must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", help="dev, staging, or prod")
    parser.add_argument("plan_json", nargs="?", default="-", help="path or - for stdin")
    args = parser.parse_args(argv)
    try:
        enforce_plan_policy(_load_plan(args.plan_json), args.environment)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Terraform plan policy failed: {exc}", file=sys.stderr)
        return 3
    print("Terraform plan policy passed (no protected destructive/immutable actions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
