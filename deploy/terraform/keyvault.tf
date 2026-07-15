locals {
  pg_fqdn = azurerm_postgresql_flexible_server.main.fqdn

  # Runtime and migration use password-derived generation roles. Replacing a
  # random_password therefore creates a new login rather than overwriting the
  # password used by live pods. Helm provisions the new generation before the
  # rollout and retires old generations only after the Deployment is Ready.
  postgres_runtime_generation   = nonsensitive(substr(sha256(random_password.postgres_runtime.result), 0, 12))
  postgres_migration_generation = nonsensitive(substr(sha256(random_password.postgres_migration.result), 0, 12))
  postgres_runtime_login        = "apex_runtime_v${local.postgres_runtime_generation}"
  postgres_migration_login      = "apex_migration_v${local.postgres_migration_generation}"
  postgres_credential_generation = nonsensitive(substr(sha256(
    "${local.postgres_runtime_login}:${local.postgres_migration_login}"
  ), 0, 16))
  postgres_admin_userinfo     = "${urlencode(var.postgres_admin_login)}:${urlencode(var.postgres_admin_password)}"
  postgres_runtime_userinfo   = "${urlencode(local.postgres_runtime_login)}:${urlencode(random_password.postgres_runtime.result)}"
  postgres_migration_userinfo = "${urlencode(local.postgres_migration_login)}:${urlencode(random_password.postgres_migration.result)}"

  # Azure PostgreSQL presents a publicly trusted certificate. Require both CA
  # and hostname verification on every generated credential. LangGraph uses
  # psycopg/libpq and therefore opts into libpq's system trust store explicitly;
  # APEX maps asyncpg's verify-full mode to Python's verifying system-CA context.
  database_admin_uri          = "postgresql+asyncpg://${local.postgres_admin_userinfo}@${local.pg_fqdn}:5432/${var.postgres_database}?sslmode=verify-full"
  database_uri                = "postgresql://${local.postgres_runtime_userinfo}@${local.pg_fqdn}:5432/${var.postgres_database}?sslmode=verify-full&sslrootcert=system"
  apex_database_uri           = "postgresql+asyncpg://${local.postgres_runtime_userinfo}@${local.pg_fqdn}:5432/${var.postgres_database}?sslmode=verify-full"
  apex_database_migration_uri = "postgresql+asyncpg://${local.postgres_migration_userinfo}@${local.pg_fqdn}:5432/${var.postgres_database}?sslmode=verify-full"
  redis_uri                   = "rediss://:${urlencode(azurerm_redis_cache.main.primary_access_key)}@${azurerm_redis_cache.main.hostname}:${azurerm_redis_cache.main.ssl_port}/0"

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

resource "random_password" "postgres_runtime" {
  length  = 48
  special = false
}

resource "random_password" "postgres_migration" {
  length  = 48
  special = false
}

# Stable across database-admin and versioned-login rotations. PostgreSQL role
# comments store only HMACs derived from this hook-only key.
resource "random_password" "database_role_claim" {
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

resource "azurerm_key_vault" "hooks" {
  name                       = local.hook_kv_name
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

resource "azurerm_role_assignment" "hook_kv_deployer" {
  scope                = azurerm_key_vault.hooks.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = local.deployer_object_id
}

# Public runtime identity can read only the runtime vault.
resource "azurerm_role_assignment" "kv_workload" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
}


# Pre-install role/migration/bootstrap identity can read only the hook vault.
resource "azurerm_role_assignment" "kv_hooks" {
  scope                = azurerm_key_vault.hooks.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.hooks.principal_id
}

# The privileged hook identity may also read runtime secrets so its pre-upgrade
# sync Pod can refresh native runtime Secrets before a credential-generation
# rollout. The inverse remains forbidden: the public workload cannot read hooks.
resource "azurerm_role_assignment" "kv_runtime_hooks" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.hooks.principal_id
}

resource "azurerm_key_vault_secret" "database_uri" {
  name         = "database-uri"
  value        = local.database_uri
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]

  # Migration bridge: the old CSI-owned Secret may still feed old pods during
  # the first blue/green rollout. Never publish a candidate password here.
  lifecycle {
    ignore_changes = [value]
  }
}

resource "azurerm_key_vault_secret" "apex_database_uri" {
  name         = "apex-database-uri"
  value        = local.apex_database_uri
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]

  lifecycle {
    ignore_changes = [value]
  }
}

resource "azurerm_key_vault_secret" "redis_uri" {
  name         = "redis-uri"
  value        = local.redis_uri
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "langgraph_license" {
  name         = "langgraph-license"
  value        = var.langgraph_license_key
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "artifact_secret_key" {
  name         = "artifact-secret-key"
  value        = var.artifact_store_secret
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]

  # MinIO supports only one root credential, while the CSI-synced Kubernetes
  # Secret and the MinIO/app pods cannot switch atomically. Ordinary applies
  # therefore freeze this value; rotate it only through the maintenance-window
  # procedure that updates Key Vault and restarts MinIO and every client.
  lifecycle {
    ignore_changes = [value]
  }
}

moved {
  from = azurerm_key_vault_secret.langgraph_license[0]
  to   = azurerm_key_vault_secret.langgraph_license
}

moved {
  from = azurerm_key_vault_secret.artifact_secret_key[0]
  to   = azurerm_key_vault_secret.artifact_secret_key
}

resource "azurerm_key_vault_secret" "bootstrap_admin_key" {
  name         = "bootstrap-admin-key"
  value        = local.bootstrap_admin_key
  key_vault_id = azurerm_key_vault.hooks.id
  depends_on   = [azurerm_role_assignment.hook_kv_deployer]
}

resource "azurerm_key_vault_secret" "api_key_hash_pepper" {
  name         = "api-key-hash-pepper"
  value        = local.api_key_hash_pepper
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

resource "azurerm_key_vault_secret" "previous_api_key_hash_peppers" {
  name         = "previous-api-key-hash-peppers"
  value        = jsonencode(var.previous_api_key_hash_peppers)
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

# A resource lifecycle precondition is a hard plan/apply failure (unlike a
# top-level check, whose failed assertion is diagnostic-only). Keep only the
# count in state; the pepper values remain in sensitive inputs/Key Vault state.
resource "terraform_data" "api_key_pepper_contract" {
  input = length(var.previous_api_key_hash_peppers)

  lifecycle {
    precondition {
      condition     = !contains(var.previous_api_key_hash_peppers, local.api_key_hash_pepper)
      error_message = "previous_api_key_hash_peppers must not contain the current pepper."
    }
  }
}

# Exposes only booleans/counts to state while giving Terraform tests a
# provider-free target for the boot-critical deployment-secret contract.
resource "terraform_data" "deployment_secret_contract" {
  input = {
    license_present       = length(trimspace(var.langgraph_license_key)) > 0
    artifact_secret_chars = length(var.artifact_store_secret)
  }

  lifecycle {
    precondition {
      condition     = length(trimspace(var.langgraph_license_key)) > 0
      error_message = "langgraph_license_key is required for the AKS standalone server."
    }
    precondition {
      condition     = length(var.artifact_store_secret) >= 24
      error_message = "artifact_store_secret must be at least 24 characters."
    }
  }
}

resource "azurerm_key_vault_secret" "database_admin_uri" {
  name         = "database-admin-uri"
  value        = local.database_admin_uri
  key_vault_id = azurerm_key_vault.hooks.id
  depends_on   = [azurerm_role_assignment.hook_kv_deployer]
}

resource "azurerm_key_vault_secret" "database_role_claim" {
  name         = "database-role-claim"
  value        = random_password.database_role_claim.result
  key_vault_id = azurerm_key_vault.hooks.id
  depends_on   = [azurerm_role_assignment.hook_kv_deployer]
}

resource "azurerm_key_vault_secret" "apex_database_runtime_uri_hook" {
  name         = "apex-database-runtime-uri"
  value        = local.apex_database_uri
  key_vault_id = azurerm_key_vault.hooks.id
  depends_on   = [azurerm_role_assignment.hook_kv_deployer]
}

resource "azurerm_key_vault_secret" "database_runtime_uri_hook" {
  name         = "database-runtime-uri"
  value        = local.database_uri
  key_vault_id = azurerm_key_vault.hooks.id
  depends_on   = [azurerm_role_assignment.hook_kv_deployer]
}

resource "azurerm_key_vault_secret" "apex_database_migration_uri" {
  name         = "apex-database-migration-uri"
  value        = local.apex_database_migration_uri
  key_vault_id = azurerm_key_vault.hooks.id
  depends_on   = [azurerm_role_assignment.hook_kv_deployer]
}

resource "azurerm_key_vault_secret" "hook_api_key_hash_pepper" {
  name         = "api-key-hash-pepper"
  value        = local.api_key_hash_pepper
  key_vault_id = azurerm_key_vault.hooks.id
  depends_on   = [azurerm_role_assignment.hook_kv_deployer]
}

resource "azurerm_key_vault_secret" "hook_previous_api_key_hash_peppers" {
  name         = "previous-api-key-hash-peppers"
  value        = jsonencode(var.previous_api_key_hash_peppers)
  key_vault_id = azurerm_key_vault.hooks.id
  depends_on   = [azurerm_role_assignment.hook_kv_deployer]
}
