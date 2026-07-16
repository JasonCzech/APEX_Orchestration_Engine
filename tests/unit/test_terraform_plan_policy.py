from __future__ import annotations

import io
from pathlib import Path

import pytest
from scripts import terraform_plan_policy
from scripts.terraform_plan_policy import (
    _load_plan,
    destructive_protected_changes,
    enforce_plan_policy,
)


def _plan(
    resource_type: str,
    actions: list[str],
    *,
    resource_name: str = "main",
) -> dict[str, object]:
    return {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": f"{resource_type}.{resource_name}",
                "type": resource_type,
                "name": resource_name,
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
    plan = _plan(
        "azurerm_key_vault_secret",
        ["update"],
        resource_name="artifact_secret_key",
    )
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
    plan = _plan(resource_type, actions, resource_name="database_role_claim")
    plan["resource_changes"][0]["address"] = address  # type: ignore[index]

    with pytest.raises(ValueError, match="database_role_claim"):
        enforce_plan_policy(plan, "prod")


def test_locked_environment_allows_initial_database_claim_key_creation() -> None:
    plan = _plan("random_password", ["create"], resource_name="database_role_claim")
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
        "azurerm_container_registry",
        "azurerm_key_vault_secret",
        "azurerm_log_analytics_workspace",
        "azurerm_postgresql_flexible_server_database",
        "azurerm_storage_management_policy",
    ],
)
def test_locked_environment_protects_additional_state_and_retention_controls(
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


def test_policy_diagnostics_never_echo_instance_keys_or_controls() -> None:
    canary = "resource-address-secret-canary"
    plan = _plan("azurerm_storage_account", ["delete"])
    plan["resource_changes"][0]["address"] = (  # type: ignore[index]
        f'module.storage["{canary}\n\x1b[31m"].azurerm_storage_account.main["instance"]'
    )

    with pytest.raises(ValueError) as caught:
        enforce_plan_policy(plan, "prod")

    rendered = str(caught.value)
    assert canary not in rendered
    assert rendered.count("\n") == 2
    assert "\x1b" not in rendered
    assert "azurerm_storage_account.main" in rendered


def test_policy_rejects_malformed_resource_type_without_reflection() -> None:
    canary = "resource-type-secret-canary"
    plan = _plan("azurerm_storage_account", ["delete"])
    plan["resource_changes"][0]["type"] = f"bad\n\x1b[31m{canary}"  # type: ignore[index]

    with pytest.raises(ValueError) as caught:
        enforce_plan_policy(plan, "prod")

    rendered = str(caught.value)
    assert canary not in rendered
    assert "\x1b" not in rendered
    assert "resource type is absent or invalid" in rendered


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


def test_plan_loader_rejects_oversized_stdin_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terraform_plan_policy, "MAX_PLAN_JSON_BYTES", 16)
    monkeypatch.setattr(terraform_plan_policy.sys, "stdin", io.StringIO("x" * 17))

    with pytest.raises(ValueError, match="exceeds the size limit") as caught:
        _load_plan("-")

    assert caught.value.__cause__ is None


def test_plan_loader_translates_file_and_json_errors_without_reflection(
    tmp_path: Path,
) -> None:
    canary = "credential-bearing-plan-name\n\x1b[31m"
    missing = tmp_path / canary

    with pytest.raises(ValueError, match="JSON is unavailable") as unavailable:
        _load_plan(str(missing))
    assert canary not in str(unavailable.value)
    assert unavailable.value.__cause__ is None

    malformed = tmp_path / "malformed.json"
    malformed.write_bytes(b'{"secret-canary":')
    with pytest.raises(ValueError, match="JSON is invalid") as invalid:
        _load_plan(str(malformed))
    assert "secret-canary" not in str(invalid.value)
    assert invalid.value.__cause__ is None


def test_policy_caps_resource_count_and_rendered_violations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _plan("azurerm_storage_account", ["delete"])["resource_changes"][0]  # type: ignore[index]
    second = _plan("azurerm_postgresql_flexible_server", ["delete"])["resource_changes"][0]  # type: ignore[index]
    plan = {"format_version": "1.2", "resource_changes": [first, second]}

    monkeypatch.setattr(terraform_plan_policy, "MAX_RESOURCE_CHANGES", 1)
    with pytest.raises(ValueError, match="too many resource changes"):
        enforce_plan_policy(plan, "prod")

    monkeypatch.setattr(terraform_plan_policy, "MAX_RESOURCE_CHANGES", 2)
    monkeypatch.setattr(terraform_plan_policy, "MAX_REPORTED_VIOLATIONS", 1)
    with pytest.raises(ValueError) as caught:
        enforce_plan_policy(plan, "prod")
    assert "1 additional protected mutations omitted" in str(caught.value)
