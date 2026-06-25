# APEX AKS infrastructure (Terraform)

Provisions the Azure-managed dependencies the Helm chart consumes, mapped to its
secret contract:

| Resource | Produces |
|---|---|
| AKS (OIDC + Workload Identity, CNI Overlay/Cilium, Container Insights + Managed Prometheus) | the cluster |
| Azure Container Registry (+ AcrPull to the kubelet identity) | image registry, no pull secrets |
| PostgreSQL Flexible Server 16 (private by default) | `DATABASE_URI` (psycopg, `sslmode=require`) + `APEX_DATABASE__URI` (asyncpg, `ssl=true`) |
| Azure Cache for Redis (TLS-only) | `REDIS_URI` (`rediss://…:6380`) |
| Key Vault (RBAC) | all secrets above + `langgraph-license` + `artifact-secret-key` |
| User-assigned identity + federated credential | workload identity for the SA `apex/apex-orchestration-engine` |
| Storage Account | MinIO artifact-store backup target |
| Log Analytics | logs/metrics sink |

> The live artifact store is **MinIO-on-AKS** (deployed via the chart/manifests),
> not Blob — the S3 adapter is not Blob-compatible (ADR-0007). The Storage Account
> is the backup/DR target.

## AKS Automatic

The azurerm provider does not yet expose the managed **Automatic** SKU. This stack
provisions a Standard-tier cluster configured with the Automatic feature set
(OIDC + Workload Identity, Cilium overlay, monitoring, autoscaling). To run true
AKS Automatic, create the cluster out of band with `az aks create --sku automatic`
and import it, or use the `azapi` provider — the Helm chart targets either unchanged.

## Usage

```bash
# 1) One-time: provision the remote-state backend (local state).
cd deploy/terraform/bootstrap
terraform init
terraform apply -var environment=dev
TFSTATE_RG=$(terraform output -raw resource_group_name)
TFSTATE_SA=$(terraform output -raw storage_account_name)

# 2) Main stack.
cd ..
terraform init \
  -backend-config="resource_group_name=$TFSTATE_RG" \
  -backend-config="storage_account_name=$TFSTATE_SA" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=apex-dev.tfstate"

export TF_VAR_postgres_admin_password='<strong-password>'
export TF_VAR_langgraph_license_key='<enterprise-license>'   # optional; stored in Key Vault
export TF_VAR_artifact_store_secret='<minio-secret>'

terraform apply -var-file=env/dev.tfvars
```

Outputs (`acr_login_server`, `aks_cluster_name`, `key_vault_name`,
`workload_identity_client_id`, `tenant_id`, …) feed the Helm `values-azure.yaml`
overlay and the `deploy-aks` workflow. `scripts/deploy.py aks-up` wires this end to end.

Credentials come from the environment (`az login` or `ARM_*` / OIDC in CI) — no
secrets are stored in these files. `terraform plan` previews every change.
