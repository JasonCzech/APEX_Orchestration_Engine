provider "azurerm" {
  # subscription_id comes from ARM_SUBSCRIPTION_ID (or `az login` default).
  features {
    key_vault {
      # Let `terraform destroy` actually remove vaults/secrets in non-prod.
      purge_soft_delete_on_destroy    = true
      recover_soft_deleted_key_vaults = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
}

provider "random" {}

data "azurerm_client_config" "current" {}
