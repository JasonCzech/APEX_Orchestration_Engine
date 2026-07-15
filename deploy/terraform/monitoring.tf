resource "azurerm_log_analytics_workspace" "main" {
  name                = "law-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_analytics_retention_days
  tags                = local.tags
}

# An alert without a destination does not protect recovery. Development may
# deliberately use portal-only alerts; staging and production fail planning
# unless monitoring is disabled explicitly or at least one Action Group pages.
resource "terraform_data" "backup_monitoring_contract" {
  input = {
    environment        = lower(var.environment)
    monitoring_enabled = var.backup_monitoring_enabled
    destination_count  = length(var.backup_alert_action_group_ids)
  }

  lifecycle {
    precondition {
      condition = (
        !var.backup_monitoring_enabled ||
        lower(var.environment) == "dev" ||
        length(var.backup_alert_action_group_ids) > 0
      )
      error_message = "backup_alert_action_group_ids requires at least one destination when backup monitoring is enabled in staging or prod."
    }
  }
}

# Container Insights supplies KubePodInventory/ContainerLogV2. Query validation
# is skipped because those tables appear only after the AKS monitoring agent has
# sent its first records; the rules become active as soon as data arrives.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "backup_failed" {
  count                 = var.backup_monitoring_enabled ? 1 : 0
  name                  = "alert-${local.suffix}-artifact-backup-failed"
  resource_group_name   = azurerm_resource_group.main.name
  location              = azurerm_resource_group.main.location
  scopes                = [azurerm_log_analytics_workspace.main.id]
  description           = "APEX MinIO artifact backup Job entered Failed state."
  severity              = 1
  enabled               = true
  evaluation_frequency  = "PT5M"
  window_duration       = "PT30M"
  skip_query_validation = true
  tags                  = local.tags

  criteria {
    query                   = <<-KQL
      KubePodInventory
      | where TimeGenerated > ago(30m)
      | where Namespace == "${var.workload_namespace}"
      | where Name startswith "apex-minio-backup-"
      | where PodStatus == "Failed"
      | summarize FailedPods = dcount(PodUid)
    KQL
    metric_measure_column   = "FailedPods"
    operator                = "GreaterThan"
    threshold               = 0
    time_aggregation_method = "Maximum"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  dynamic "action" {
    for_each = length(var.backup_alert_action_group_ids) > 0 ? [1] : []
    content {
      action_groups = var.backup_alert_action_group_ids
      custom_properties = {
        environment = var.environment
        runbook     = "docs/runbooks/aks-deployment.md#artifact-backup-alerts"
      }
    }
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "backup_missing" {
  count                     = var.backup_monitoring_enabled ? 1 : 0
  name                      = "alert-${local.suffix}-artifact-backup-missing"
  resource_group_name       = azurerm_resource_group.main.name
  location                  = azurerm_resource_group.main.location
  scopes                    = [azurerm_log_analytics_workspace.main.id]
  description               = "No APEX MinIO artifact backup pod has emitted logs in seven hours."
  severity                  = 1
  enabled                   = true
  evaluation_frequency      = "PT15M"
  window_duration           = "PT15M"
  query_time_range_override = "P1D"
  skip_query_validation     = true
  tags                      = local.tags

  criteria {
    query                   = <<-KQL
      let BackupLogs = ContainerLogV2
        | where TimeGenerated > ago(7h)
        | where PodNamespace == "${var.workload_namespace}"
        | where PodName startswith "apex-minio-backup-"
        | summarize Seen = count();
      print MissingBackup = iff(toscalar(BackupLogs) == 0, 1, 0)
    KQL
    metric_measure_column   = "MissingBackup"
    operator                = "GreaterThan"
    threshold               = 0
    time_aggregation_method = "Maximum"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 2
      number_of_evaluation_periods             = 2
    }
  }

  dynamic "action" {
    for_each = length(var.backup_alert_action_group_ids) > 0 ? [1] : []
    content {
      action_groups = var.backup_alert_action_group_ids
      custom_properties = {
        environment = var.environment
        runbook     = "docs/runbooks/aks-deployment.md#artifact-backup-alerts"
      }
    }
  }
}
