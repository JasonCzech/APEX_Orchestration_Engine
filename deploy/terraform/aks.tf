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

  oidc_issuer_enabled       = true
  workload_identity_enabled = true

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
    name                 = "system"
    vm_size              = var.system_node_vm_size
    vnet_subnet_id       = azurerm_subnet.aks.id
    auto_scaling_enabled = true
    min_count            = var.system_node_min_count
    max_count            = var.system_node_max_count
    orchestrator_version = var.kubernetes_version
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
