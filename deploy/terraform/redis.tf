resource "azurerm_redis_cache" "main" {
  name                          = "redis-${local.suffix}"
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  capacity                      = var.redis_capacity
  family                        = var.redis_sku == "Premium" ? "P" : "C"
  sku_name                      = var.redis_sku
  non_ssl_port_enabled          = false
  minimum_tls_version           = "1.2"
  public_network_access_enabled = var.redis_public_network_access_enabled
  tags                          = local.tags
}

resource "azurerm_private_endpoint" "redis" {
  count               = var.redis_public_network_access_enabled ? 0 : 1
  name                = "pe-redis-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints.id
  tags                = local.tags

  private_service_connection {
    name                           = "redis-private-link"
    private_connection_resource_id = azurerm_redis_cache.main.id
    is_manual_connection           = false
    subresource_names              = ["redisCache"]
  }

  private_dns_zone_group {
    name                 = "redis-private-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.redis[0].id]
  }
}
