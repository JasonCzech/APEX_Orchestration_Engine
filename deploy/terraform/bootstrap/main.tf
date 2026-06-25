# Bootstrap the Terraform remote-state backend (chicken-and-egg: this uses LOCAL
# state). Run once per environment before the main stack:
#   cd deploy/terraform/bootstrap
#   terraform init && terraform apply -var environment=dev
# Then init the main stack with the outputs (see ../README.md).
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
}

variable "name_prefix" {
  type    = string
  default = "apex"
}

variable "environment" {
  type = string
}

variable "location" {
  type    = string
  default = "eastus2"
}

locals {
  sa_name = lower(replace("tfstate${var.name_prefix}${var.environment}", "-", ""))
}

resource "azurerm_resource_group" "tfstate" {
  name     = "rg-tfstate-${var.name_prefix}-${var.environment}"
  location = var.location
}

resource "azurerm_storage_account" "tfstate" {
  name                     = local.sa_name
  resource_group_name      = azurerm_resource_group.tfstate.name
  location                 = azurerm_resource_group.tfstate.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"

  blob_properties {
    versioning_enabled = true
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_name  = azurerm_storage_account.tfstate.name
  container_access_type = "private"
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
