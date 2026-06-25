# Remote state in Azure Storage. Provisioned first by ./bootstrap (local state),
# then wired here via `terraform init -backend-config=...` (see README / deploy.sh):
#
#   terraform init \
#     -backend-config="resource_group_name=$TFSTATE_RG" \
#     -backend-config="storage_account_name=$TFSTATE_SA" \
#     -backend-config="container_name=tfstate" \
#     -backend-config="key=apex-$ENV.tfstate"
terraform {
  backend "azurerm" {}
}
