# Bootstrap the Terraform remote-state backend (chicken-and-egg: this uses LOCAL
# state). Run once per environment before the main stack:
#   cd deploy/terraform/bootstrap
#   terraform init && terraform apply -var environment=dev
# Then init the main stack with the outputs (see ../README.md).
terraform {
  # Cross-variable validation below requires Terraform 1.9 or newer.
  required_version = ">= 1.9, < 2.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  storage_use_azuread = true
  features {}
}

data "azurerm_client_config" "current" {}

variable "name_prefix" {
  type    = string
  default = "apex"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{0,8}$", var.name_prefix))
    error_message = "name_prefix must start with a letter and contain at most 9 lowercase alphanumeric characters."
  }
}

variable "environment" {
  type = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be exactly dev, staging, or prod."
  }
}

variable "location" {
  type    = string
  default = "eastus2"
}

variable "state_private_endpoint_subnet_id" {
  type        = string
  description = "Existing management-runner subnet for the locked state private endpoint."
  default     = ""

  validation {
    condition = (
      !contains(["staging", "prod"], lower(var.environment)) ||
      trimspace(var.state_private_endpoint_subnet_id) != ""
    )
    error_message = "state_private_endpoint_subnet_id is required for staging and prod."
  }
}

variable "state_private_dns_vnet_id" {
  type        = string
  description = "VNet used by the deployment runner; linked to the state Blob private DNS zone."
  default     = ""

  validation {
    condition = (
      !contains(["staging", "prod"], lower(var.environment)) ||
      trimspace(var.state_private_dns_vnet_id) != ""
    )
    error_message = "state_private_dns_vnet_id is required for staging and prod."
  }
}

variable "state_blob_data_principal_ids" {
  type        = set(string)
  description = "Additional Entra object IDs (for example the GitHub OIDC deployer) granted Blob Data Contributor on state."
  default     = []

  validation {
    condition = (
      !contains(["staging", "prod"], lower(var.environment)) ||
      length(var.state_blob_data_principal_ids) > 0
    )
    error_message = "state_blob_data_principal_ids must include the deployment OIDC principal in staging and prod."
  }
}

locals {
  sa_name            = lower(replace("tfstate${var.name_prefix}${var.environment}", "-", ""))
  locked_environment = contains(["staging", "prod"], lower(var.environment))
  state_blob_data_principal_ids = setunion(
    toset([data.azurerm_client_config.current.object_id]),
    var.state_blob_data_principal_ids,
  )
}

resource "azurerm_resource_group" "tfstate" {
  name     = "rg-tfstate-${var.name_prefix}-${var.environment}"
  location = var.location
}

resource "azurerm_storage_account" "tfstate" {
  name                            = local.sa_name
  resource_group_name             = azurerm_resource_group.tfstate.name
  location                        = azurerm_resource_group.tfstate.location
  account_tier                    = "Standard"
  account_replication_type        = local.locked_environment ? "GRS" : "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  public_network_access_enabled   = !local.locked_environment
  allow_nested_items_to_be_public = false

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = local.locked_environment ? 30 : 7
    }

    container_delete_retention_policy {
      days = local.locked_environment ? 30 : 7
    }
  }
}

resource "azurerm_role_assignment" "tfstate_deployer" {
  for_each             = local.state_blob_data_principal_ids
  scope                = azurerm_storage_account.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = each.value
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"

  depends_on = [
    azurerm_private_dns_zone_virtual_network_link.blob,
    azurerm_private_endpoint.tfstate,
    azurerm_role_assignment.tfstate_deployer,
  ]
}

resource "azurerm_private_dns_zone" "blob" {
  count               = local.locked_environment ? 1 : 0
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = azurerm_resource_group.tfstate.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "blob" {
  count                 = local.locked_environment ? 1 : 0
  name                  = "tfstate-blob-dns-link"
  resource_group_name   = azurerm_resource_group.tfstate.name
  private_dns_zone_name = azurerm_private_dns_zone.blob[0].name
  virtual_network_id    = var.state_private_dns_vnet_id
}

resource "azurerm_private_endpoint" "tfstate" {
  count               = local.locked_environment ? 1 : 0
  name                = "pe-tfstate-${var.name_prefix}-${var.environment}"
  location            = azurerm_resource_group.tfstate.location
  resource_group_name = azurerm_resource_group.tfstate.name
  subnet_id           = var.state_private_endpoint_subnet_id

  private_service_connection {
    name                           = "tfstate-blob-private-link"
    private_connection_resource_id = azurerm_storage_account.tfstate.id
    is_manual_connection           = false
    subresource_names              = ["blob"]
  }

  private_dns_zone_group {
    name                 = "tfstate-private-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.blob[0].id]
  }
}

# Locked state cannot be removed through an ordinary management-plane delete.
# The deploy plan policy also refuses to delete or replace this resource.
resource "azurerm_management_lock" "tfstate" {
  count      = local.locked_environment ? 1 : 0
  name       = "protect-terraform-state"
  scope      = azurerm_storage_account.tfstate.id
  lock_level = "CanNotDelete"
  notes      = "Break-glass removal requires an independently reviewed recovery change."
}

output "resource_group_name" {
  value = azurerm_resource_group.tfstate.name
}

output "storage_account_name" {
  value = azurerm_storage_account.tfstate.name
}

output "container_name" {
  value = azurerm_storage_container.tfstate.name
}
