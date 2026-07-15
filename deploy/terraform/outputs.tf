output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "location" {
  value = azurerm_resource_group.main.location
}

output "deployer_object_id" {
  description = "Stable object ID used for backup-state data-plane reconciliation and smoke verification."
  value       = local.deployer_object_id
}

output "acr_login_server" {
  value = azurerm_container_registry.main.login_server
}

output "aks_cluster_name" {
  value = azurerm_kubernetes_cluster.main.name
}

output "aks_oidc_issuer_url" {
  value = azurerm_kubernetes_cluster.main.oidc_issuer_url
}

output "key_vault_name" {
  value = azurerm_key_vault.main.name
}

output "hook_key_vault_name" {
  value = azurerm_key_vault.hooks.name
}

output "workload_identity_client_id" {
  value = azurerm_user_assigned_identity.workload.client_id
}

output "hook_identity_client_id" {
  value = azurerm_user_assigned_identity.hooks.client_id
}

output "backup_identity_client_id" {
  value = azurerm_user_assigned_identity.backup.client_id
}

output "backup_identity_principal_id" {
  description = "Object ID passed to the independently stateful backup stack."
  value       = azurerm_user_assigned_identity.backup.principal_id
}

output "workload_service_account" {
  value = var.workload_service_account
}

output "workload_hook_service_account" {
  value = var.workload_hook_service_account
}

output "backup_service_account" {
  value = "apex-minio-backup"
}

output "tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}

output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.main.fqdn
}

output "redis_hostname" {
  value = azurerm_redis_cache.main.hostname
}

output "database_credential_generation" {
  description = "Non-secret fingerprint of both login role names used to gate rollout and retirement."
  value       = local.postgres_credential_generation
}

output "database_runtime_role_name" {
  description = "Current blue/green PostgreSQL runtime login role."
  value       = local.postgres_runtime_login
}

output "database_migration_role_name" {
  description = "Current blue/green PostgreSQL migration login role."
  value       = local.postgres_migration_login
}
