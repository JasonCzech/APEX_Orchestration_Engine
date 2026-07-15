variable "environment" {
  type        = string
  description = "Deployment environment (dev | staging | prod). Drives naming + defaults."
}

variable "location" {
  type        = string
  description = "Azure region (e.g. eastus2)."
  default     = "eastus2"
}

variable "name_prefix" {
  type        = string
  description = "Short prefix for resource names (lowercase, <=10 chars)."
  default     = "apex"
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

# ── AKS ─────────────────────────────────────────────────────────────────────
variable "kubernetes_version" {
  type        = string
  description = "AKS Kubernetes version (null = AKS default)."
  default     = null
}

variable "cluster_sku_tier" {
  type        = string
  description = "AKS control-plane tier (Free | Standard | Premium)."
  default     = "Standard"
}

variable "system_node_vm_size" {
  type    = string
  default = "Standard_D4ds_v5"
}

variable "system_node_min_count" {
  type    = number
  default = 2
}

variable "system_node_max_count" {
  type    = number
  default = 5
}

# ── ACR ───────────────────────────────────────────────────────────────────────
variable "acr_sku" {
  type    = string
  default = "Standard" # Premium for private endpoint / geo-replication
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

# ── Redis ─────────────────────────────────────────────────────────────────────
variable "redis_sku" {
  type    = string
  default = "Standard"
}

variable "redis_capacity" {
  type    = number
  default = 1
}

# ── Key Vault / secrets ───────────────────────────────────────────────────────
variable "langgraph_license_key" {
  type        = string
  description = "Self-Hosted Enterprise LANGGRAPH_CLOUD_LICENSE_KEY. Stored in Key Vault."
  sensitive   = true
  default     = ""
}

variable "artifact_store_secret" {
  type        = string
  description = "Secret for the in-cluster MinIO artifact store (APEX_INTEGRATION_MINIO_SECRET_KEY)."
  sensitive   = true
  default     = ""
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
  type        = string
  description = "JSON array of former API-key hash peppers retained during rotation."
  sensitive   = true
  default     = "[]"

  validation {
    condition = can([
      for pepper in jsondecode(var.previous_api_key_hash_peppers) :
      pepper if length(pepper) >= 32
    ])
    error_message = "previous_api_key_hash_peppers must be a JSON array of strings at least 32 characters long."
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
}

variable "workload_service_account" {
  type        = string
  description = "Kubernetes ServiceAccount name federated to the workload identity."
  default     = "apex-orchestration-engine"
}

variable "workload_hook_service_account" {
  type        = string
  description = "Pre-install CSI hook ServiceAccount federated to the workload identity."
  default     = "apex-orchestration-engine-hooks"
}

# ── Monitoring ────────────────────────────────────────────────────────────────
variable "log_analytics_retention_days" {
  type    = number
  default = 30
}
