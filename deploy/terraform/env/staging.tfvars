environment = "staging"
location    = "eastus2"

cluster_sku_tier                    = "Standard"
aks_private_cluster_enabled         = true
system_node_min_count               = 2
system_node_max_count               = 5
acr_sku                             = "Standard"
postgres_public_access              = false
postgres_sku_name                   = "GP_Standard_D2ds_v5"
postgres_backup_retention_days      = 14
redis_sku                           = "Standard"
redis_capacity                      = 1
redis_public_network_access_enabled = false
