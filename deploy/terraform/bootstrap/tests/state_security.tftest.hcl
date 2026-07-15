mock_provider "azurerm" {}

run "reject_case_variant_environment_and_invalid_prefix" {
  command = plan

  variables {
    environment = "DEV"
    name_prefix = "bad-prefix"
  }

  expect_failures = [var.environment, var.name_prefix]
}

run "production_state_denies_shared_keys_and_public_network" {
  command = plan

  variables {
    environment                      = "prod"
    state_private_endpoint_subnet_id = "/subscriptions/000/resourceGroups/runner/providers/Microsoft.Network/virtualNetworks/runner/subnets/private-endpoints"
    state_private_dns_vnet_id        = "/subscriptions/000/resourceGroups/runner/providers/Microsoft.Network/virtualNetworks/runner"
    state_blob_data_principal_ids    = ["00000000-0000-0000-0000-000000000001"]
  }

  assert {
    condition     = !azurerm_storage_account.tfstate.shared_access_key_enabled
    error_message = "Terraform state must reject shared-key authentication"
  }

  assert {
    condition     = !azurerm_storage_account.tfstate.public_network_access_enabled
    error_message = "production Terraform state must disable public networking"
  }

  assert {
    condition     = azurerm_storage_account.tfstate.account_replication_type == "GRS"
    error_message = "production Terraform state must be geo-redundant"
  }

  assert {
    condition     = length(azurerm_private_endpoint.tfstate) == 1
    error_message = "production Terraform state must use a private endpoint"
  }

  assert {
    condition     = azurerm_management_lock.tfstate[0].lock_level == "CanNotDelete"
    error_message = "production Terraform state must carry a delete lock"
  }
}
