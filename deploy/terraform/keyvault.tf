locals {
  pg_fqdn = azurerm_postgresql_flexible_server.main.fqdn

  # IMPORTANT: psycopg uses ?sslmode=require; asyncpg does NOT accept sslmode — it
  # uses ?ssl=true. Two query strings for the same server (the #1 footgun).
  postgres_userinfo = "${urlencode(var.postgres_admin_login)}:${urlencode(var.postgres_admin_password)}"
  database_uri      = "postgresql://${local.postgres_userinfo}@${local.pg_fqdn}:5432/${var.postgres_database}?sslmode=require"
  apex_database_uri = "postgresql+asyncpg://${local.postgres_userinfo}@${local.pg_fqdn}:5432/${var.postgres_database}?ssl=true"
  redis_uri         = "rediss://:${urlencode(azurerm_redis_cache.main.primary_access_key)}@${azurerm_redis_cache.main.hostname}:${azurerm_redis_cache.main.ssl_port}/0"

  bootstrap_admin_key = var.bootstrap_admin_key != "" ? var.bootstrap_admin_key : random_password.bootstrap_admin_key.result
  api_key_hash_pepper = var.api_key_hash_pepper != "" ? var.api_key_hash_pepper : random_password.api_key_hash_pepper.result
}

resource "random_password" "bootstrap_admin_key" {
  length  = 48
  special = false
}

resource "random_password" "api_key_hash_pepper" {
  length  = 64
  special = false
}

resource "azurerm_key_vault" "main" {
  name                       = local.kv_name
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  tags                       = local.tags
}

# The TF caller (or a named deployer) can write secrets.
resource "azurerm_role_assignment" "kv_deployer" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = local.deployer_object_id
}

# The workload identity (CSI driver / app) can read secrets.
resource "azurerm_role_assignment" "kv_workload" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
}

resource "azurerm_key_vault_secret" "database_uri" {
  name         = "database-uri"
  value        = local.database_uri
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "apex_database_uri" {
  name         = "apex-database-uri"
  value        = local.apex_database_uri
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "redis_uri" {
  name         = "redis-uri"
  value        = local.redis_uri
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "langgraph_license" {
  count        = var.langgraph_license_key != "" ? 1 : 0
  name         = "langgraph-license"
  value        = var.langgraph_license_key
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "artifact_secret_key" {
  count        = var.artifact_store_secret != "" ? 1 : 0
  name         = "artifact-secret-key"
  value        = var.artifact_store_secret
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "artifact_backup_account" {
  name         = "artifact-backup-account"
  value        = azurerm_storage_account.main.name
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "artifact_backup_key" {
  name         = "artifact-backup-key"
  value        = azurerm_storage_account.main.primary_access_key
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "bootstrap_admin_key" {
  name         = "bootstrap-admin-key"
  value        = local.bootstrap_admin_key
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "api_key_hash_pepper" {
  name         = "api-key-hash-pepper"
  value        = local.api_key_hash_pepper
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "previous_api_key_hash_peppers" {
  name         = "previous-api-key-hash-peppers"
  value        = var.previous_api_key_hash_peppers
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}
