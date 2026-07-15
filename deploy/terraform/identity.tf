# Separate user-assigned identities keep public runtime, privileged install
# hooks, and the Blob backup job in independent trust domains.
resource "azurerm_user_assigned_identity" "workload" {
  name                = "id-${local.suffix}-workload"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "workload" {
  name                = "apex-workload"
  resource_group_name = azurerm_resource_group.main.name
  parent_id           = azurerm_user_assigned_identity.workload.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:${var.workload_namespace}:${var.workload_service_account}"
}

resource "azurerm_federated_identity_credential" "workload_hooks" {
  name                = "apex-workload-hooks"
  resource_group_name = azurerm_resource_group.main.name
  parent_id           = azurerm_user_assigned_identity.hooks.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:${var.workload_namespace}:${var.workload_hook_service_account}"
}

resource "azurerm_user_assigned_identity" "hooks" {
  name                = "id-${local.suffix}-hooks"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_user_assigned_identity" "backup" {
  name                = "id-${local.suffix}-backup"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "backup" {
  name                = "apex-artifact-backup"
  resource_group_name = azurerm_resource_group.main.name
  parent_id           = azurerm_user_assigned_identity.backup.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:${var.workload_namespace}:apex-minio-backup"
}
