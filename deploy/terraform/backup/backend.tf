# Initialize with the same hardened backend account as the live stack but a
# distinct key (`apex-<environment>-backup.tfstate`). The live stack has no
# ownership relationship with this state.
terraform {
  backend "azurerm" {}
}
