from __future__ import annotations

import pytest
from scripts.terraform_plan_policy import (
    destructive_protected_changes,
    enforce_plan_policy,
)


def _plan(resource_type: str, actions: list[str]) -> dict[str, object]:
    return {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": f"{resource_type}.main",
                "type": resource_type,
                "change": {
                    "actions": actions,
                    # A policy failure must never render these sensitive values.
                    "before": {"password": "old-secret"},
                    "after": {"password": "new-secret"},
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "actions",
    [["delete"], ["delete", "create"], ["create", "delete"], ["forget"]],
)
def test_locked_environment_rejects_stateful_deletes_and_replacements(
    actions: list[str],
) -> None:
    with pytest.raises(ValueError) as caught:
        enforce_plan_policy(_plan("azurerm_postgresql_flexible_server", actions), "prod")

    assert "azurerm_postgresql_flexible_server.main" in str(caught.value)
    assert "secret" not in str(caught.value)


def test_locked_environment_allows_nondestructive_stateful_update() -> None:
    assert (
        destructive_protected_changes(
            _plan("azurerm_postgresql_flexible_server", ["update"]), "staging"
        )
        == []
    )


@pytest.mark.parametrize(
    "address",
    [
        "azurerm_key_vault_secret.artifact_secret_key",
        'azurerm_key_vault_secret.artifact_secret_key["current"]',
        "module.security[0].azurerm_key_vault_secret.artifact_secret_key",
    ],
)
def test_locked_environment_rejects_non_atomic_minio_root_secret_rotation(
    address: str,
) -> None:
    plan = _plan("azurerm_key_vault_secret", ["update"])
    plan["resource_changes"][0]["address"] = address  # type: ignore[index]

    with pytest.raises(ValueError, match="artifact_secret_key"):
        enforce_plan_policy(plan, "prod")


@pytest.mark.parametrize(
    ("resource_type", "address", "actions"),
    [
        (
            "azurerm_key_vault_secret",
            "azurerm_key_vault_secret.database_role_claim",
            ["update"],
        ),
        (
            "random_password",
            "random_password.database_role_claim",
            ["delete", "create"],
        ),
        (
            "random_password",
            "module.database[0].random_password.database_role_claim",
            ["forget"],
        ),
    ],
)
def test_locked_environment_rejects_ordinary_database_claim_key_rotation(
    resource_type: str,
    address: str,
    actions: list[str],
) -> None:
    plan = _plan(resource_type, actions)
    plan["resource_changes"][0]["address"] = address  # type: ignore[index]

    with pytest.raises(ValueError, match="database_role_claim"):
        enforce_plan_policy(plan, "prod")


def test_locked_environment_allows_initial_database_claim_key_creation() -> None:
    plan = _plan("random_password", ["create"])
    plan["resource_changes"][0]["address"] = (  # type: ignore[index]
        "random_password.database_role_claim"
    )

    enforce_plan_policy(plan, "prod")


def test_dev_can_intentionally_replace_ephemeral_resources() -> None:
    assert (
        destructive_protected_changes(
            _plan("azurerm_postgresql_flexible_server", ["delete", "create"]), "dev"
        )
        == []
    )


def test_unknown_environment_cannot_bypass_protected_resource_policy() -> None:
    with pytest.raises(ValueError, match="environment must be one of"):
        destructive_protected_changes(
            _plan("azurerm_postgresql_flexible_server", ["delete", "create"]),
            "prodd",
        )


def test_locked_environment_allows_deleting_stateless_resource() -> None:
    assert destructive_protected_changes(_plan("azurerm_role_assignment", ["delete"]), "prod") == []


def test_locked_environment_rejects_removing_deletion_lock() -> None:
    with pytest.raises(ValueError, match="azurerm_management_lock.main"):
        enforce_plan_policy(_plan("azurerm_management_lock", ["delete"]), "prod")


@pytest.mark.parametrize(
    "resource_type",
    [
        "azurerm_key_vault_secret",
        "azurerm_log_analytics_workspace",
        "azurerm_postgresql_flexible_server_database",
        "azurerm_storage_management_policy",
    ],
)
def test_locked_environment_protects_credential_and_retention_state(
    resource_type: str,
) -> None:
    with pytest.raises(ValueError, match=resource_type):
        enforce_plan_policy(_plan(resource_type, ["delete"]), "prod")


def test_malformed_actions_cannot_be_rendered_into_policy_output() -> None:
    plan = _plan("azurerm_storage_account", ["delete"])
    resource = plan["resource_changes"][0]  # type: ignore[index]
    resource["change"]["actions"] = [{"secret": "do-not-leak"}]  # type: ignore[index]

    with pytest.raises(ValueError) as caught:
        enforce_plan_policy(plan, "prod")

    assert "do-not-leak" not in str(caught.value)


@pytest.mark.parametrize(
    "plan",
    [
        {},
        {"format_version": "2.0", "resource_changes": []},
        {"format_version": "1.2"},
        {"format_version": "1.2", "resource_changes": ["malformed"]},
        {
            "format_version": "1.2",
            "resource_changes": [
                {"address": "azurerm_storage_account.main", "type": "azurerm_storage_account"}
            ],
        },
    ],
)
def test_locked_policy_fails_closed_for_unknown_plan_shapes(plan: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        enforce_plan_policy(plan, "prod")


def test_locked_policy_rejects_future_unknown_actions() -> None:
    with pytest.raises(ValueError, match="unsupported change action"):
        enforce_plan_policy(_plan("azurerm_storage_account", ["replace"]), "prod")
