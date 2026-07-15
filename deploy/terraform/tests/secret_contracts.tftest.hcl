mock_provider "azurerm" {}
mock_provider "random" {}

# Keep every production-focused test on the same safe HA baseline. Individual
# runs override the one input whose failure they are exercising.
variables {
  kubernetes_version              = "1.32"
  acr_sku                         = "Premium"
  aks_private_cluster_enabled     = true
  system_node_min_count           = 3
  system_node_zones               = ["1", "2", "3"]
  postgres_zone                   = "1"
  postgres_high_availability_mode = "ZoneRedundant"
  postgres_standby_zone           = "2"
  redis_sku                       = "Premium"
}

run "reject_unknown_environment" {
  command = plan

  variables {
    environment             = "prodd"
    postgres_admin_password = "unit-test-postgres-password"
    langgraph_license_key   = "unit-test-enterprise-license"
    artifact_store_secret   = "unit-test-artifact-secret-long-enough"
  }

  plan_options {
    target = [terraform_data.deployment_topology_contract]
  }

  expect_failures = [var.environment]
}

run "reject_production_without_declared_ha" {
  command = plan

  variables {
    environment                           = "prod"
    postgres_admin_password               = "unit-test-postgres-password"
    langgraph_license_key                 = "unit-test-enterprise-license"
    artifact_store_secret                 = "unit-test-artifact-secret-long-enough"
    postgres_backup_retention_days        = 35
    postgres_geo_redundant_backup_enabled = true
    aks_private_cluster_enabled           = false
    system_node_min_count                 = 1
    system_node_zones                     = []
    postgres_zone                         = ""
    postgres_high_availability_mode       = ""
    postgres_standby_zone                 = ""
    redis_sku                             = "Standard"
  }

  plan_options {
    target = [terraform_data.deployment_topology_contract]
  }

  expect_failures = [
    var.aks_private_cluster_enabled,
    var.system_node_min_count,
    var.system_node_zones,
    var.postgres_high_availability_mode,
    var.redis_sku,
  ]
}

run "reject_missing_langgraph_license" {
  command = plan

  variables {
    environment             = "dev"
    postgres_admin_password = "unit-test-postgres-password"
    langgraph_license_key   = ""
    artifact_store_secret   = "unit-test-artifact-secret-long-enough"
  }

  plan_options {
    target = [terraform_data.deployment_secret_contract]
  }

  expect_failures = [var.langgraph_license_key]
}

run "reject_weak_artifact_store_secret" {
  command = plan

  variables {
    environment             = "dev"
    postgres_admin_password = "unit-test-postgres-password"
    langgraph_license_key   = "unit-test-enterprise-license"
    artifact_store_secret   = "too-short"
  }

  plan_options {
    target = [terraform_data.deployment_secret_contract]
  }

  expect_failures = [var.artifact_store_secret]
}

run "reject_unrouted_production_backup_alerts" {
  command = plan

  variables {
    environment                   = "prod"
    postgres_admin_password       = "unit-test-postgres-password"
    langgraph_license_key         = "unit-test-enterprise-license"
    artifact_store_secret         = "unit-test-artifact-secret-long-enough"
    backup_monitoring_enabled     = true
    backup_alert_action_group_ids = []
  }

  plan_options {
    target = [terraform_data.backup_monitoring_contract]
  }

  expect_failures = [terraform_data.backup_monitoring_contract]
}

run "reject_blank_backup_alert_destination" {
  command = plan

  variables {
    environment                   = "dev"
    postgres_admin_password       = "unit-test-postgres-password"
    langgraph_license_key         = "unit-test-enterprise-license"
    artifact_store_secret         = "unit-test-artifact-secret-long-enough"
    backup_alert_action_group_ids = ["  "]
  }

  plan_options {
    target = [terraform_data.backup_monitoring_contract]
  }

  expect_failures = [var.backup_alert_action_group_ids]
}

run "reject_current_pepper_in_rotation_history" {
  command = plan

  variables {
    environment                   = "dev"
    postgres_admin_password       = "unit-test-postgres-password"
    langgraph_license_key         = "unit-test-enterprise-license"
    artifact_store_secret         = "unit-test-artifact-secret-long-enough"
    api_key_hash_pepper           = "unit-test-current-pepper-32-characters"
    previous_api_key_hash_peppers = ["unit-test-current-pepper-32-characters"]
  }

  plan_options {
    target = [terraform_data.api_key_pepper_contract]
  }

  expect_failures = [terraform_data.api_key_pepper_contract]
}

run "database_credentials_use_versioned_roles" {
  command = apply

  variables {
    environment             = "dev"
    postgres_admin_password = "unit-test-postgres-password"
    langgraph_license_key   = "unit-test-enterprise-license"
    artifact_store_secret   = "unit-test-artifact-secret-long-enough"
  }

  plan_options {
    target = [random_password.postgres_runtime, random_password.postgres_migration]
  }

  assert {
    condition     = startswith(output.database_runtime_role_name, "apex_runtime_v")
    error_message = "runtime password generations must map to versioned login roles"
  }

  assert {
    condition     = startswith(output.database_migration_role_name, "apex_migration_v")
    error_message = "migration password generations must map to versioned login roles"
  }

  assert {
    condition     = can(regex("^[a-f0-9]{16}$", output.database_credential_generation))
    error_message = "the rollout fingerprint must bind both non-secret login role names"
  }
}

run "reject_unpinned_locked_kubernetes_version" {
  command = plan

  variables {
    environment             = "staging"
    kubernetes_version      = null
    postgres_admin_password = "unit-test-postgres-password"
    langgraph_license_key   = "unit-test-enterprise-license"
    artifact_store_secret   = "unit-test-artifact-secret-long-enough"
  }

  plan_options {
    target = [terraform_data.deployment_topology_contract]
  }

  expect_failures = [var.kubernetes_version]
}

run "reject_invalid_resource_identifiers_and_redis_capacity" {
  command = plan

  variables {
    environment                   = "dev"
    name_prefix                   = "Apex-invalid"
    workload_namespace            = "tenant/escape"
    workload_service_account      = "UPPER"
    workload_hook_service_account = "hook_underscore"
    redis_sku                     = "Premium"
    redis_capacity                = 0
    postgres_admin_password       = "unit-test-postgres-password"
    langgraph_license_key         = "unit-test-enterprise-license"
    artifact_store_secret         = "unit-test-artifact-secret-long-enough"
  }

  plan_options {
    target = [terraform_data.deployment_topology_contract]
  }

  expect_failures = [
    var.name_prefix,
    var.workload_namespace,
    var.workload_service_account,
    var.workload_hook_service_account,
    var.redis_capacity,
  ]
}

run "production_aks_disables_local_accounts_and_rotates_node_pools_safely" {
  command = plan

  variables {
    environment                           = "prod"
    postgres_admin_password               = "unit-test-postgres-password"
    langgraph_license_key                 = "unit-test-enterprise-license"
    artifact_store_secret                 = "unit-test-artifact-secret-long-enough"
    aks_private_cluster_enabled           = true
    postgres_backup_retention_days        = 35
    postgres_geo_redundant_backup_enabled = true
    system_node_zones                     = ["1", "2", "3"]
    system_node_pool_temporary_name       = "systemtmp"
  }

  plan_options {
    target = [azurerm_kubernetes_cluster.main]
  }

  assert {
    condition     = azurerm_kubernetes_cluster.main.local_account_disabled
    error_message = "AKS local certificate accounts must remain disabled"
  }

  assert {
    condition     = azurerm_kubernetes_cluster.main.private_cluster_enabled
    error_message = "production AKS must keep a private control-plane endpoint"
  }

  assert {
    condition = (
      azurerm_kubernetes_cluster.main.default_node_pool[0].temporary_name_for_rotation
      == "systemtmp"
    )
    error_message = "AKS node-pool changes require a temporary rotation pool"
  }
}

run "production_data_services_have_private_network_and_recovery" {
  command = plan

  variables {
    environment                           = "prod"
    postgres_admin_password               = "unit-test-postgres-password"
    langgraph_license_key                 = "unit-test-enterprise-license"
    artifact_store_secret                 = "unit-test-artifact-secret-long-enough"
    postgres_backup_retention_days        = 35
    postgres_geo_redundant_backup_enabled = true
    redis_sku                             = "Premium"
    redis_public_network_access_enabled   = false
  }

  plan_options {
    target = [
      azurerm_postgresql_flexible_server.main,
      azurerm_redis_cache.main,
      azurerm_private_endpoint.redis,
    ]
  }

  assert {
    condition     = azurerm_postgresql_flexible_server.main.backup_retention_days == 35
    error_message = "production PostgreSQL must keep a 35-day PITR window"
  }

  assert {
    condition     = azurerm_postgresql_flexible_server.main.geo_redundant_backup_enabled
    error_message = "production PostgreSQL backups must be geo-redundant"
  }

  assert {
    condition     = !azurerm_redis_cache.main.public_network_access_enabled
    error_message = "production Redis must disable its public endpoint"
  }

  assert {
    condition     = length(azurerm_private_endpoint.redis) == 1
    error_message = "production Redis must have one private endpoint"
  }
}

run "reject_public_production_redis" {
  command = plan

  variables {
    environment                           = "prod"
    postgres_admin_password               = "unit-test-postgres-password"
    langgraph_license_key                 = "unit-test-enterprise-license"
    artifact_store_secret                 = "unit-test-artifact-secret-long-enough"
    postgres_backup_retention_days        = 35
    postgres_geo_redundant_backup_enabled = true
    redis_public_network_access_enabled   = true
  }

  plan_options {
    target = [azurerm_redis_cache.main]
  }

  expect_failures = [var.redis_public_network_access_enabled]
}

run "reject_short_non_geo_production_database_backups" {
  command = plan

  variables {
    environment                           = "prod"
    postgres_admin_password               = "unit-test-postgres-password"
    langgraph_license_key                 = "unit-test-enterprise-license"
    artifact_store_secret                 = "unit-test-artifact-secret-long-enough"
    postgres_backup_retention_days        = 7
    postgres_geo_redundant_backup_enabled = false
  }

  plan_options {
    target = [azurerm_postgresql_flexible_server.main]
  }

  expect_failures = [
    var.postgres_backup_retention_days,
    var.postgres_geo_redundant_backup_enabled,
  ]
}
