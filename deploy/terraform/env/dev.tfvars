environment = "dev"
location    = "eastus2"

# Dev: cheaper + simpler ops. Public PG lets you run alembic from a laptop.
cluster_sku_tier                    = "Free"
system_node_min_count               = 1
system_node_max_count               = 3
acr_sku                             = "Standard"
postgres_public_access              = true
postgres_sku_name                   = "B_Standard_B1ms"
redis_sku                           = "Basic"
redis_capacity                      = 0
redis_public_network_access_enabled = true
