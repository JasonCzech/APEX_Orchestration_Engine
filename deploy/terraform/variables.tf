variable "environment" {
  type        = string
  description = "Deployment environment (dev | staging | prod). Drives naming + defaults."

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be exactly dev, staging, or prod."
  }
}

variable "location" {
  type        = string
  description = "Azure region (e.g. eastus2)."
  default     = "eastus2"
}

variable "name_prefix" {
  type        = string
  description = "Short prefix for resource names (lowercase, <=9 chars)."
  default     = "apex"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{0,8}$", var.name_prefix))
    error_message = "name_prefix must start with a letter and contain at most 9 lowercase alphanumeric characters."
  }
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to every resource."
  default     = {}
}

# ── networking ────────────────────────────────────────────────────────────────
variable "vnet_address_space" {
  type    = list(string)
  default = ["10.40.0.0/16"]
}

variable "aks_subnet_prefix" {
  type    = string
  default = "10.40.0.0/20"
}

variable "pg_subnet_prefix" {
  type    = string
  default = "10.40.16.0/24"
}

variable "private_endpoint_subnet_prefix" {
  type        = string
  description = "Dedicated subnet used by locked-environment private endpoints."
  default     = "10.40.17.0/24"
}

# ── AKS ─────────────────────────────────────────────────────────────────────
variable "kubernetes_version" {
  type        = string
  description = "Reviewed AKS Kubernetes version; required in staging and production."
  default     = null

  validation {
    condition = (
      var.kubernetes_version == null ||
      can(regex("^1\\.[0-9]+(\\.[0-9]+)?$", var.kubernetes_version))
      ) && (
      var.environment == "dev" || var.kubernetes_version != null
    )
    error_message = "kubernetes_version must be an explicit 1.x minor/patch in staging and production."
  }
}

variable "cluster_sku_tier" {
  type        = string
  description = "AKS control-plane tier (Free | Standard | Premium)."
  default     = "Standard"

  validation {
    condition = (
      contains(["Free", "Standard", "Premium"], var.cluster_sku_tier) &&
      (var.environment != "prod" || var.cluster_sku_tier != "Free")
    )
    error_message = "cluster_sku_tier must be Free, Standard, or Premium; production cannot use Free."
  }
}

variable "system_node_vm_size" {
  type    = string
  default = "Standard_D4ds_v5"
}

variable "system_node_min_count" {
  type    = number
  default = 2

  validation {
    condition = (
      var.system_node_min_count >= 1 &&
      floor(var.system_node_min_count) == var.system_node_min_count &&
      (var.environment != "prod" || var.system_node_min_count >= 3)
    )
    error_message = "system_node_min_count must be a positive integer and at least 3 in production."
  }
}

variable "system_node_max_count" {
  type    = number
  default = 5

  validation {
    condition = (
      floor(var.system_node_max_count) == var.system_node_max_count &&
      var.system_node_max_count >= var.system_node_min_count
    )
    error_message = "system_node_max_count must be an integer no smaller than system_node_min_count."
  }
}

variable "system_node_zones" {
  type        = list(string)
  description = "Availability zones for the AKS system pool; set multiple zones in production."
  default     = []

  validation {
    condition = (
      length(var.system_node_zones) == length(toset(var.system_node_zones)) &&
      alltrue([for zone in var.system_node_zones : contains(["1", "2", "3"], zone)]) &&
      (var.environment != "prod" || length(var.system_node_zones) >= 2)
    )
    error_message = "system_node_zones must contain unique Azure zones 1-3 and at least two zones in production."
  }
}

variable "system_node_pool_temporary_name" {
  type        = string
  description = "Temporary AKS system-pool name used for safe node-pool rotations (including zone changes)."
  default     = "systemtmp"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{0,11}$", var.system_node_pool_temporary_name))
    error_message = "system_node_pool_temporary_name must be 1-12 lowercase alphanumeric characters and start with a letter."
  }
}

variable "aks_private_cluster_enabled" {
  type        = bool
  description = "Expose the AKS control plane only through its private VNet endpoint."
  default     = false

  validation {
    condition     = var.environment == "dev" || var.aks_private_cluster_enabled
    error_message = "aks_private_cluster_enabled must be true in staging and production."
  }
}

variable "aks_admin_group_object_ids" {
  type        = list(string)
  description = "Optional Entra group object IDs granted AKS admin access in addition to the Terraform deployer."
  default     = []
}

# ── ACR ───────────────────────────────────────────────────────────────────────
variable "acr_sku" {
  type    = string
  default = "Standard" # Premium for private endpoint / geo-replication

  validation {
    condition = (
      contains(["Basic", "Standard", "Premium"], var.acr_sku) &&
      (var.environment != "prod" || var.acr_sku == "Premium")
    )
    error_message = "acr_sku must be Basic, Standard, or Premium; production requires Premium."
  }
}

# ── PostgreSQL ────────────────────────────────────────────────────────────────
variable "postgres_public_access" {
  type        = bool
  description = "true = public endpoint + firewall (dev); false = private VNet-integrated (secure default)."
  default     = false

  validation {
    condition     = !var.postgres_public_access || lower(var.environment) == "dev"
    error_message = "postgres_public_access=true is allowed only when environment is dev."
  }
}

variable "postgres_sku_name" {
  type    = string
  default = "GP_Standard_D2ds_v5"
}

variable "postgres_storage_mb" {
  type    = number
  default = 32768
}

variable "postgres_backup_retention_days" {
  type        = number
  description = "Point-in-time restore retention window in days."
  default     = 7

  validation {
    condition = (
      var.postgres_backup_retention_days >= 7 &&
      var.postgres_backup_retention_days <= 35 &&
      (!contains(["prod", "production"], lower(var.environment)) || var.postgres_backup_retention_days >= 30)
    )
    error_message = "postgres_backup_retention_days must be 7-35 days and at least 30 days in production."
  }
}

variable "postgres_geo_redundant_backup_enabled" {
  type        = bool
  description = "Replicate PostgreSQL backups to the paired Azure region. Required in production."
  default     = false

  validation {
    condition = (
      !contains(["prod", "production"], lower(var.environment)) ||
      var.postgres_geo_redundant_backup_enabled
    )
    error_message = "postgres_geo_redundant_backup_enabled must be true in production."
  }
}

variable "postgres_admin_login" {
  type    = string
  default = "apexadmin"
}

variable "postgres_admin_password" {
  type        = string
  description = "PostgreSQL admin password (supply via TF_VAR_postgres_admin_password / Key Vault)."
  sensitive   = true
}

variable "postgres_database" {
  type    = string
  default = "apex"
}

variable "postgres_zone" {
  type        = string
  description = "Primary PostgreSQL availability zone; empty lets Azure choose."
  default     = ""
}

variable "postgres_high_availability_mode" {
  type        = string
  description = "PostgreSQL HA mode: empty, SameZone, or ZoneRedundant."
  default     = ""

  validation {
    condition = (
      contains(["", "SameZone", "ZoneRedundant"], var.postgres_high_availability_mode) &&
      (var.environment != "prod" || var.postgres_high_availability_mode == "ZoneRedundant")
    )
    error_message = "postgres_high_availability_mode must be empty, SameZone, or ZoneRedundant; production requires ZoneRedundant."
  }
}

variable "postgres_standby_zone" {
  type        = string
  description = "Standby PostgreSQL zone for ZoneRedundant HA."
  default     = ""

  validation {
    condition = (
      var.postgres_high_availability_mode != "ZoneRedundant" ||
      (
        trimspace(var.postgres_zone) != "" &&
        trimspace(var.postgres_standby_zone) != "" &&
        var.postgres_standby_zone != var.postgres_zone
      )
    )
    error_message = "ZoneRedundant PostgreSQL requires explicit, distinct primary and standby zones."
  }
}

# ── Redis ─────────────────────────────────────────────────────────────────────
variable "redis_sku" {
  type    = string
  default = "Standard"

  validation {
    condition = (
      contains(["Basic", "Standard", "Premium"], var.redis_sku) &&
      (var.environment != "prod" || var.redis_sku == "Premium")
    )
    error_message = "redis_sku must be Basic, Standard, or Premium; production requires Premium."
  }
}

variable "redis_capacity" {
  type    = number
  default = 1

  validation {
    condition = (
      floor(var.redis_capacity) == var.redis_capacity &&
      (
        (contains(["Basic", "Standard"], var.redis_sku) && var.redis_capacity >= 0 && var.redis_capacity <= 6) ||
        (var.redis_sku == "Premium" && var.redis_capacity >= 1 && var.redis_capacity <= 5)
      )
    )
    error_message = "redis_capacity must be an integer 0-6 for Basic/Standard or 1-5 for Premium."
  }
}

variable "redis_public_network_access_enabled" {
  type        = bool
  description = "Expose Redis on its public data-plane endpoint. Allowed only in dev."
  default     = false

  validation {
    condition     = !var.redis_public_network_access_enabled || lower(var.environment) == "dev"
    error_message = "redis_public_network_access_enabled=true is allowed only when environment is dev."
  }
}

# ── Key Vault / secrets ───────────────────────────────────────────────────────
variable "langgraph_license_key" {
  type        = string
  description = "Required Self-Hosted Enterprise LANGGRAPH_CLOUD_LICENSE_KEY. Stored in Key Vault."
  sensitive   = true
  nullable    = false

  validation {
    condition     = length(trimspace(var.langgraph_license_key)) > 0
    error_message = "langgraph_license_key is required for the AKS standalone server."
  }
}

variable "artifact_store_secret" {
  type        = string
  description = "Required secret for the in-cluster MinIO artifact store (APEX_INTEGRATION_MINIO_SECRET_KEY)."
  sensitive   = true
  nullable    = false

  validation {
    condition     = length(var.artifact_store_secret) >= 24
    error_message = "artifact_store_secret must be at least 24 characters."
  }
}

variable "bootstrap_admin_key" {
  type        = string
  description = "Initial admin API key. Empty generates a random value stored only in state/Key Vault."
  sensitive   = true
  default     = ""

  validation {
    condition     = var.bootstrap_admin_key == "" || length(var.bootstrap_admin_key) >= 24
    error_message = "bootstrap_admin_key must be empty (generate) or at least 24 characters."
  }
}

variable "api_key_hash_pepper" {
  type        = string
  description = "HMAC pepper for API-key hashes. Empty generates a random value stored only in state/Key Vault."
  sensitive   = true
  default     = ""

  validation {
    condition     = var.api_key_hash_pepper == "" || length(var.api_key_hash_pepper) >= 32
    error_message = "api_key_hash_pepper must be empty (generate) or at least 32 characters."
  }
}

variable "previous_api_key_hash_peppers" {
  type        = list(string)
  description = "Former API-key hash peppers retained during rotation. Pass a JSON array via TF_VAR_previous_api_key_hash_peppers."
  sensitive   = true
  nullable    = false
  default     = []

  validation {
    condition = (
      length(var.previous_api_key_hash_peppers) <= 16 &&
      length(distinct(var.previous_api_key_hash_peppers)) == length(var.previous_api_key_hash_peppers) &&
      alltrue([for pepper in var.previous_api_key_hash_peppers : length(pepper) >= 32])
    )
    error_message = "previous_api_key_hash_peppers must contain at most 16 unique values of at least 32 characters and must not include the current pepper."
  }
}

variable "deployer_object_id" {
  type        = string
  description = "AAD object id granted Key Vault Secrets Officer to write secrets (defaults to the caller)."
  default     = ""
}

# ── Workload identity federation ──────────────────────────────────────────────
variable "workload_namespace" {
  type        = string
  description = "Kubernetes namespace the workload runs in."
  default     = "apex"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.workload_namespace))
    error_message = "workload_namespace must be a 1-63 character Kubernetes DNS label."
  }
}

variable "workload_service_account" {
  type        = string
  description = "Kubernetes ServiceAccount name federated to the workload identity."
  default     = "apex-orchestration-engine"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.workload_service_account))
    error_message = "workload_service_account must be a 1-63 character Kubernetes DNS label."
  }
}

variable "workload_hook_service_account" {
  type        = string
  description = "Pre-install CSI hook ServiceAccount federated to the workload identity."
  default     = "apex-orchestration-engine-hooks"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.workload_hook_service_account))
    error_message = "workload_hook_service_account must be a 1-63 character Kubernetes DNS label."
  }
}

# ── Monitoring ────────────────────────────────────────────────────────────────
variable "log_analytics_retention_days" {
  type    = number
  default = 30
}

variable "backup_monitoring_enabled" {
  type        = bool
  description = "Create Azure Monitor alerts for failed or missing MinIO backup Jobs."
  default     = true
}

variable "backup_alert_action_group_ids" {
  type        = list(string)
  description = "Azure Monitor Action Group resource IDs notified by backup alerts; required in staging/prod when monitoring is enabled."
  default     = []

  validation {
    condition = alltrue([
      for id in var.backup_alert_action_group_ids : can(regex(
        "^/subscriptions/[^/]+/resourcegroups/[^/]+/providers/microsoft\\.insights/actiongroups/[^/]+$",
        lower(trimspace(id)),
      ))
    ])
    error_message = "backup_alert_action_group_ids must contain complete Azure Monitor Action Group resource IDs."
  }
}
