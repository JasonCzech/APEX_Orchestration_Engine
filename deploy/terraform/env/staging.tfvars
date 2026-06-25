environment = "staging"
location    = "eastus2"

cluster_sku_tier       = "Standard"
system_node_min_count  = 2
system_node_max_count  = 5
acr_sku                = "Standard"
postgres_public_access = false
postgres_sku_name      = "GP_Standard_D2ds_v5"
redis_sku              = "Standard"
redis_capacity         = 1
