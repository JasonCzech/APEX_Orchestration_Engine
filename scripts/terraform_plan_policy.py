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
MAX_PLAN_JSON_BYTES = 64 * 1024 * 1024
MAX_RESOURCE_CHANGES = 50_000
MAX_REPORTED_VIOLATIONS = 100
KNOWN_ACTIONS = frozenset({"no-op", "create", "read", "update", "delete", "forget"})
PROTECTED_RESOURCE_TYPES = frozenset(
    {
        "azurerm_container_registry",
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
_TERRAFORM_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,255}")


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
    if len(resource_changes) > MAX_RESOURCE_CHANGES:
        raise ValueError("Terraform plan contains too many resource changes")
    for resource in resource_changes:
        if not isinstance(resource, dict):
            raise ValueError("Terraform plan resource change must be an object")
        resource_type = resource.get("type")
        if (
            not isinstance(resource_type, str)
            or _TERRAFORM_IDENTIFIER.fullmatch(resource_type) is None
        ):
            raise ValueError("Terraform plan resource type is absent or invalid")
        resource_name = resource.get("name")
        if (
            not isinstance(resource_name, str)
            or _TERRAFORM_IDENTIFIER.fullmatch(resource_name) is None
        ):
            # Resource addresses may contain arbitrary for_each keys. Validate
            # the dedicated logical name field and never echo raw addresses or
            # instance keys into CI diagnostics.
            raise ValueError("Terraform plan resource name is absent or invalid")
        logical_address = f"{resource_type}.{resource_name}"
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
            raise ValueError("Terraform plan resource address must be a string")
        destructive = resource_type in PROTECTED_RESOURCE_TYPES and bool(
            {"delete", "forget"}.intersection(actions)
        )
        # `type` + `name` is stable across modules, count, and for_each, and
        # cannot be confused by crafted instance-key syntax in `address`.
        immutable_update = "update" in actions and logical_address in IMMUTABLE_UPDATE_ADDRESSES
        immutable_replacement = (
            bool({"delete", "forget", "update"}.intersection(actions))
            and logical_address in IMMUTABLE_REPLACEMENT_ADDRESSES
        )
        if destructive or immutable_update or immutable_replacement:
            violations.append(f"{logical_address} ({'/'.join(actions)})")
    return sorted(violations)


def enforce_plan_policy(plan: dict[str, Any], environment: str) -> None:
    violations = destructive_protected_changes(plan, environment)
    if not violations:
        return
    rendered = "\n  - ".join(violations[:MAX_REPORTED_VIOLATIONS])
    omitted = len(violations) - MAX_REPORTED_VIOLATIONS
    suffix = f"\n  - ... {omitted} additional protected mutations omitted" if omitted > 0 else ""
    raise ValueError(
        "saved Terraform plan mutates protected stateful resources:\n"
        f"  - {rendered}{suffix}\n"
        "Use a separately reviewed break-glass change; ordinary deployment is blocked."
    )


def _load_plan(path: str) -> dict[str, Any]:
    try:
        if path == "-":
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            payload = stream.read(MAX_PLAN_JSON_BYTES + 1)
        else:
            with Path(path).open("rb") as stream:
                payload = stream.read(MAX_PLAN_JSON_BYTES + 1)
    except OSError:
        # Paths are operator-controlled and may contain credentials or terminal
        # controls. Keep the CLI boundary stable and do not retain the cause.
        raise ValueError("Terraform plan JSON is unavailable") from None

    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if len(payload) > MAX_PLAN_JSON_BYTES:
        raise ValueError("Terraform plan JSON exceeds the size limit")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Terraform plan JSON is invalid") from None
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
    except ValueError as exc:
        print(f"Terraform plan policy failed: {exc}", file=sys.stderr)
        return 3
    print("Terraform plan policy passed (no protected destructive/immutable actions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
