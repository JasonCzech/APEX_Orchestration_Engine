# AKS cluster configured with the feature set AKS Automatic provides — OIDC issuer
# + Workload Identity, Azure CNI Overlay + Cilium, Container Insights + Managed
# Prometheus, and an autoscaling system pool. NOTE: the azurerm provider does not
# yet expose the managed "Automatic" SKU; to run true AKS Automatic, create the
# cluster with `az aks create --sku automatic` (see README) — this resource is the
# portable Standard-tier equivalent that the Helm chart targets unchanged.
resource "azurerm_kubernetes_cluster" "main" {
  name                = "aks-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  dns_prefix          = "${var.name_prefix}${var.environment}"
  kubernetes_version  = var.kubernetes_version
  sku_tier            = var.cluster_sku_tier

  # Entra/Azure RBAC credentials are short-lived and auditable. Static local
  # cluster-admin certificates are disabled entirely.
  local_account_disabled              = true
  private_cluster_enabled             = var.aks_private_cluster_enabled
  private_cluster_public_fqdn_enabled = false
  private_dns_zone_id                 = var.aks_private_cluster_enabled ? "System" : null

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled     = true
    admin_group_object_ids = var.aks_admin_group_object_ids
  }

  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  # Install the managed ingress controller used by the Azure Helm overlay.
  web_app_routing {
    dns_zone_ids = []
  }

  # Azure Key Vault Secrets Provider (Secrets Store CSI driver) for secretsStoreCSI.
  key_vault_secrets_provider {
    secret_rotation_enabled = true
  }

  # Managed Prometheus metrics + Container Insights -> Log Analytics.
  monitor_metrics {}
  oms_agent {
    log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  }

  default_node_pool {
    name                        = "system"
    vm_size                     = var.system_node_vm_size
    vnet_subnet_id              = azurerm_subnet.aks.id
    auto_scaling_enabled        = true
    min_count                   = var.system_node_min_count
    max_count                   = var.system_node_max_count
    orchestrator_version        = var.kubernetes_version
    zones                       = var.system_node_zones
    temporary_name_for_rotation = var.system_node_pool_temporary_name
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_data_plane  = "cilium"
    network_policy      = "cilium"
    load_balancer_sku   = "standard"
  }

  tags = local.tags
}

# `get-credentials` and Kubernetes authorization are separate Azure actions.
# Grant both explicitly to the OIDC deployer used by the workflow/script.
resource "azurerm_role_assignment" "aks_deployer_cluster_user" {
  scope                = azurerm_kubernetes_cluster.main.id
  role_definition_name = "Azure Kubernetes Service Cluster User Role"
  principal_id         = local.deployer_object_id
}

resource "azurerm_role_assignment" "aks_deployer_rbac_admin" {
  scope                = azurerm_kubernetes_cluster.main.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = local.deployer_object_id
}
