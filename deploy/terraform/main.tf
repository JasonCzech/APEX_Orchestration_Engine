locals {
  suffix  = "${var.name_prefix}-${var.environment}"
  rg_name = "rg-${local.suffix}"
  # ACR (<=50 alnum) and Storage (<=24 alnum, lowercase) names disallow hyphens.
  acr_name     = lower(replace("acr${var.name_prefix}${var.environment}", "-", ""))
  kv_name      = "kv-${local.suffix}"
  hook_kv_name = substr(lower(replace("kvh-${local.suffix}", "_", "-")), 0, 24)

  tags = merge({
    application  = "apex-orchestration-engine"
    environment  = var.environment
    "managed-by" = "terraform"
  }, var.tags)

  deployer_object_id = var.deployer_object_id != "" ? var.deployer_object_id : data.azurerm_client_config.current.object_id
}

# A provider-free target used by tests and recovery tooling to evaluate every
# environment/topology validation without planning Azure resources.
resource "terraform_data" "deployment_topology_contract" {
  input = {
    environment                     = var.environment
    name_prefix                     = var.name_prefix
    kubernetes_version              = var.kubernetes_version
    acr_sku                         = var.acr_sku
    cluster_sku_tier                = var.cluster_sku_tier
    system_node_min_count           = var.system_node_min_count
    system_node_max_count           = var.system_node_max_count
    system_node_zones               = var.system_node_zones
    aks_private_cluster_enabled     = var.aks_private_cluster_enabled
    postgres_zone                   = var.postgres_zone
    postgres_high_availability_mode = var.postgres_high_availability_mode
    postgres_standby_zone           = var.postgres_standby_zone
    redis_sku                       = var.redis_sku
    redis_capacity                  = var.redis_capacity
    workload_namespace              = var.workload_namespace
    workload_service_account        = var.workload_service_account
    workload_hook_service_account   = var.workload_hook_service_account
  }
}

resource "azurerm_resource_group" "main" {
  name     = local.rg_name
  location = var.location
  tags     = local.tags
}
