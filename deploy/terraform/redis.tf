resource "azurerm_redis_cache" "main" {
  name                 = "redis-${local.suffix}"
  resource_group_name  = azurerm_resource_group.main.name
  location             = azurerm_resource_group.main.location
  capacity             = var.redis_capacity
  family               = var.redis_sku == "Premium" ? "P" : "C"
  sku_name             = var.redis_sku
  non_ssl_port_enabled = false
  minimum_tls_version  = "1.2"
  tags                 = local.tags
}
