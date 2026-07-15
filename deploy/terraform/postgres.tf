resource "azurerm_postgresql_flexible_server" "main" {
  name                = "psql-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  version             = "16"

  administrator_login    = var.postgres_admin_login
  administrator_password = var.postgres_admin_password

  sku_name   = var.postgres_sku_name
  storage_mb = var.postgres_storage_mb
  zone       = var.postgres_zone != "" ? var.postgres_zone : null

  backup_retention_days        = var.postgres_backup_retention_days
  geo_redundant_backup_enabled = var.postgres_geo_redundant_backup_enabled

  dynamic "high_availability" {
    for_each = var.postgres_high_availability_mode == "" ? [] : [var.postgres_high_availability_mode]
    content {
      mode                      = high_availability.value
      standby_availability_zone = var.postgres_standby_zone != "" ? var.postgres_standby_zone : null
    }
  }

  public_network_access_enabled = var.postgres_public_access
  delegated_subnet_id           = var.postgres_public_access ? null : azurerm_subnet.postgres.id
  private_dns_zone_id           = var.postgres_public_access ? null : azurerm_private_dns_zone.postgres[0].id

  tags = local.tags

  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres]
}

resource "azurerm_postgresql_flexible_server_database" "apex" {
  name      = var.postgres_database
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# Dev-only: allow other Azure services in (0.0.0.0) when running with public access.
# Tighten to specific egress IPs for anything beyond dev.
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure" {
  count            = var.postgres_public_access && lower(var.environment) == "dev" ? 1 : 0
  name             = "allow-azure-services"
  server_id        = azurerm_postgresql_flexible_server.main.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}
