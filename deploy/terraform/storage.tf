# Storage account as the MinIO artifact-store backup/DR target (`mc mirror`). The
# live artifact store is MinIO-on-AKS; Blob is not the runtime backend (the S3
# adapter is not Blob-compatible — see ADR-0007).
resource "azurerm_storage_account" "main" {
  name                     = local.sa_name
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "GRS"
  min_tls_version          = "TLS1_2"
  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = 30
    }

    container_delete_retention_policy {
      days = 30
    }
  }
  tags = local.tags
}

resource "azurerm_storage_container" "artifacts_backup" {
  name                  = "apex-artifacts-backup"
  storage_account_name  = azurerm_storage_account.main.name
  container_access_type = "private"
}
