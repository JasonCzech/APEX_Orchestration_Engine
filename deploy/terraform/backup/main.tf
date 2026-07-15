locals {
  suffix  = "${var.name_prefix}-${var.environment}"
  sa_name = lower("st${var.name_prefix}${var.environment}backup")
  tags = merge({
    application  = "apex-orchestration-engine"
    environment  = var.environment
    purpose      = "artifact-backup-dr"
    "managed-by" = "terraform-backup-state"
  }, var.tags)
}

resource "azurerm_resource_group" "backup" {
  name     = "rg-backup-${local.suffix}"
  location = var.location
  tags     = local.tags
}

# This account is intentionally absent from the live-stack state. GRS,
# versioning, soft delete, shared-key denial, and the inherited RG lock provide
# independent recovery and deletion domains for MinIO artifacts.
resource "azurerm_storage_account" "artifacts" {
  name                            = local.sa_name
  resource_group_name             = azurerm_resource_group.backup.name
  location                        = azurerm_resource_group.backup.location
  account_tier                    = "Standard"
  account_replication_type        = "GRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = var.retention_days
    }

    container_delete_retention_policy {
      days = var.retention_days
    }
  }

  tags = local.tags
}

# AzureRM uses the storage data plane for containers. Grant the OIDC deployer
# only the data-plane access required to reconcile this backup state.
resource "azurerm_role_assignment" "deployer_blob_data" {
  scope                = azurerm_storage_account.artifacts.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.deployer_object_id
}

resource "azurerm_storage_container" "artifacts" {
  name                  = "apex-artifacts-backup"
  storage_account_id    = azurerm_storage_account.artifacts.id
  container_access_type = "private"

  depends_on = [azurerm_role_assignment.deployer_blob_data]
}

# `rclone sync` maintains the current mirror while account versioning/soft
# delete preserve recovery points. Expire non-current versions at the same
# reviewed retention boundary so repeated overwrites cannot grow without bound.
resource "azurerm_storage_management_policy" "artifact_versions" {
  storage_account_id = azurerm_storage_account.artifacts.id

  rule {
    name    = "expire-superseded-artifact-versions"
    enabled = true

    filters {
      prefix_match = ["${azurerm_storage_container.artifacts.name}/"]
      blob_types   = ["blockBlob"]
    }

    actions {
      version {
        delete_after_days_since_creation = var.retention_days
      }
    }
  }
}

resource "azurerm_role_assignment" "backup_blob_data" {
  scope                = "${azurerm_storage_account.artifacts.id}/blobServices/default/containers/${azurerm_storage_container.artifacts.name}"
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.backup_identity_principal_id
}

resource "azurerm_management_lock" "backup" {
  name       = "protect-artifact-backups"
  scope      = azurerm_resource_group.backup.id
  lock_level = "CanNotDelete"
  notes      = "Remove only through the documented, independently approved break-glass recovery flow."

  depends_on = [
    azurerm_role_assignment.backup_blob_data,
    azurerm_role_assignment.deployer_blob_data,
    azurerm_storage_container.artifacts,
    azurerm_storage_management_policy.artifact_versions,
  ]
}
