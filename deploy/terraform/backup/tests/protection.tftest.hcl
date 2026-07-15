mock_provider "azurerm" {}

run "reject_case_variant_environment" {
  command = plan

  variables {
    environment                  = "PROD"
    backup_identity_principal_id = "00000000-0000-0000-0000-000000000001"
    deployer_object_id           = "00000000-0000-0000-0000-000000000002"
  }

  expect_failures = [var.environment]
}

run "production_backup_is_independent_and_protected" {
  command = plan

  variables {
    environment                  = "prod"
    location                     = "eastus2"
    backup_identity_principal_id = "00000000-0000-0000-0000-000000000001"
    deployer_object_id           = "00000000-0000-0000-0000-000000000002"
    retention_days               = 35
  }

  assert {
    condition     = azurerm_resource_group.backup.name == "rg-backup-apex-prod"
    error_message = "artifact backups must use a dedicated resource group"
  }

  assert {
    condition     = azurerm_storage_account.artifacts.account_replication_type == "GRS"
    error_message = "artifact backups must be geo-redundant"
  }

  assert {
    condition     = !azurerm_storage_account.artifacts.shared_access_key_enabled
    error_message = "artifact backups must reject shared-key authentication"
  }

  assert {
    condition     = azurerm_storage_account.artifacts.blob_properties[0].delete_retention_policy[0].days == 35
    error_message = "artifact backups must retain deleted blobs for 35 days"
  }

  assert {
    condition     = azurerm_management_lock.backup.lock_level == "CanNotDelete"
    error_message = "artifact backup resource group must carry a delete lock"
  }

  assert {
    condition = (
      azurerm_storage_management_policy.artifact_versions.rule[0].actions[0].version[0].delete_after_days_since_creation
      == 35
    )
    error_message = "superseded artifact versions must expire at the reviewed retention boundary"
  }
}
