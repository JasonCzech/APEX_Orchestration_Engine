environment = "prod"
location    = "eastus2"

cluster_sku_tier       = "Standard"
system_node_vm_size    = "Standard_D8ds_v5"
system_node_min_count  = 3
system_node_max_count  = 8
acr_sku                = "Premium"
postgres_public_access = false
postgres_sku_name      = "GP_Standard_D4ds_v5"
postgres_storage_mb    = 131072
redis_sku              = "Premium"
redis_capacity         = 1
