variable "environment" {
  type        = string
  description = "Deployment environment whose artifacts are protected."

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be exactly dev, staging, or prod."
  }
}

variable "location" {
  type        = string
  description = "Primary Azure region. GRS keeps the backup copy in its paired region."
  default     = "eastus2"
}

variable "name_prefix" {
  type        = string
  description = "Short lowercase resource-name prefix."
  default     = "apex"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{0,8}$", var.name_prefix))
    error_message = "name_prefix must start with a letter and contain at most 9 lowercase alphanumeric characters."
  }
}

variable "backup_identity_principal_id" {
  type        = string
  description = "Object ID of the AKS artifact-backup workload identity."

  validation {
    condition     = trimspace(var.backup_identity_principal_id) != ""
    error_message = "backup_identity_principal_id is required."
  }
}

variable "deployer_object_id" {
  type        = string
  description = "Stable object ID of the OIDC/local deployer that reconciles this state and verifies backup sentinels."

  validation {
    condition     = trimspace(var.deployer_object_id) != ""
    error_message = "deployer_object_id is required."
  }
}

variable "retention_days" {
  type        = number
  description = "Soft-delete retention for blobs and the backup container."
  default     = 35

  validation {
    condition = (
      var.retention_days >= 7 && var.retention_days <= 365 &&
      (lower(var.environment) != "prod" || var.retention_days >= 30)
    )
    error_message = "retention_days must be 7-365 and at least 30 in production."
  }
}

variable "tags" {
  type        = map(string)
  description = "Additional resource tags."
  default     = {}
}
