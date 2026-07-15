# Artifact backups deliberately live in deploy/terraform/backup. That root uses
# a separate state key and resource group so destroying or replacing the live
# AKS stack cannot delete its recovery copy.
