locals {
  suffix  = "${var.name_prefix}-${var.environment}"
  rg_name = "rg-${local.suffix}"
  # ACR (<=50 alnum) and Storage (<=24 alnum, lowercase) names disallow hyphens.
  acr_name = lower(replace("acr${var.name_prefix}${var.environment}", "-", ""))
  sa_name  = lower(replace("st${var.name_prefix}${var.environment}", "-", ""))
  kv_name  = "kv-${local.suffix}"

  tags = merge({
    application  = "apex-orchestration-engine"
    environment  = var.environment
    "managed-by" = "terraform"
  }, var.tags)

  deployer_object_id = var.deployer_object_id != "" ? var.deployer_object_id : data.azurerm_client_config.current.object_id
}

resource "azurerm_resource_group" "main" {
  name     = local.rg_name
  location = var.location
  tags     = local.tags
}
