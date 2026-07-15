output "resource_group_name" {
  value = azurerm_resource_group.backup.name
}

output "storage_account_name" {
  value = azurerm_storage_account.artifacts.name
}

output "container_name" {
  value = azurerm_storage_container.artifacts.name
}
