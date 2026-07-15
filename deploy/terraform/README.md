# APEX AKS infrastructure (Terraform)

Provisions the Azure-managed dependencies the Helm chart consumes, mapped to its
secret contract:

| Resource | Produces |
|---|---|
| AKS (Entra/Azure RBAC, local accounts disabled, private API in staging/prod, OIDC + Workload Identity, CNI Overlay/Cilium, monitoring) | the cluster |
| Azure Container Registry (+ AcrPull to the kubelet identity) | image registry, no pull secrets |
| PostgreSQL Flexible Server 16 (private; zone-redundant HA plus 35-day geo-backups in prod) | separate runtime, migration, and hook-only admin URIs with TLS |
| Azure Cache for Redis (TLS-only; private endpoint/DNS in staging/prod) | `REDIS_URI` (`rediss://…:6380`) |
| Runtime Key Vault (RBAC) | Redis/license/artifact credentials, API-key peppers, and frozen legacy DB URIs used only for the first migration |
| Hook Key Vault (RBAC) | candidate runtime/admin/migration DB credentials, stable DB-role HMAC claim key, initial admin key, and hook pepper copy |
| Three user-assigned identities + federated credentials | independent runtime, privileged hook, and Blob-backup ServiceAccounts |
| Independent backup Terraform root/state + locked resource group (GRS, versioning, shared keys disabled) | MinIO DR target; backup identity receives container-scoped Blob Data Contributor |
| Log Analytics + scheduled-query alerts | logs/metrics sink and failed/missing artifact-backup detection |

> The live artifact store is **MinIO-on-AKS** (deployed via the chart/manifests),
> not Blob — the S3 adapter is not Blob-compatible (ADR-0007). The backup account
> lives under `deploy/terraform/backup` with its own state key and resource group;
> destroying the live stack does not own or delete that recovery copy.

## AKS Automatic

The azurerm provider does not yet expose the managed **Automatic** SKU. This stack
provisions a Standard-tier cluster configured with the Automatic feature set
(OIDC + Workload Identity, Cilium overlay, monitoring, autoscaling). To run true
AKS Automatic, create the cluster out of band with `az aks create --sku automatic`
and import it, or use the `azapi` provider — the Helm chart targets either unchanged.

## Usage

Terraform 1.9 or newer is required because the safety contracts validate related
environment and high-availability inputs together.

```bash
# 1) One-time: provision the remote-state backend (local state). Shared keys are
# disabled. For staging/prod, run from the management runner VNet and also pass:
#   -var state_private_endpoint_subnet_id=<runner-private-endpoint-subnet-id>
#   -var state_private_dns_vnet_id=<runner-vnet-id>
#   -var 'state_blob_data_principal_ids=["<github-oidc-object-id>"]'
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
  -backend-config="key=apex-dev.tfstate" \
  -backend-config="use_azuread_auth=true"

export TF_VAR_postgres_admin_password='<strong-password>'
export TF_VAR_langgraph_license_key='<enterprise-license>'   # required; stored in runtime Key Vault
export TF_VAR_artifact_store_secret='<at-least-24-character-minio-secret>'
export TF_VAR_api_key_hash_pepper='<at-least-32-characters>'
export TF_VAR_previous_api_key_hash_peppers='[]' # JSON string array; every entry must be >=32 chars
export TF_VAR_backup_alert_action_group_ids='["/subscriptions/.../actionGroups/apex-oncall"]' # required in staging/prod
export APEX_KUBERNETES_VERSION='<reviewed-1.x-minor-or-patch>' # required for staging/prod

# The deploy wrapper creates a binary plan, enforces the protected-resource
# policy, and applies that exact file. Staging/prod additionally require a
# SHA-256-bound confirmation token printed by the command.
export APEX_ENV=dev APEX_HOSTNAME=apex.example.com APEX_TLS_SECRET=apex-tls
uv run python scripts/deploy.py aks-up
```

Initialize the independent backup root against the same state account before a
local `aks-up`; the workflow does this automatically:

```bash
terraform -chdir=backup init \
  -backend-config="resource_group_name=$TFSTATE_RG" \
  -backend-config="storage_account_name=$TFSTATE_SA" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=apex-dev-backup.tfstate" \
  -backend-config="use_azuread_auth=true"
```

An empty license or short/missing artifact-store secret fails Terraform validation,
before an undeployable CSI configuration can reach AKS. Outputs include both Key
Vault names and all three identity client IDs/ServiceAccounts, in addition to
`acr_login_server`, `aks_cluster_name`, and `tenant_id`. The deploy script and
`deploy-aks` workflow wire the complete output set into `values-azure.yaml`.

The generated `database-role-claim` is a durable control-plane secret, not a
login credential. Keep the Terraform state and hook Key Vault recovery-safe: the
Helm hooks use this stable key to prove PostgreSQL owner/generation roles belong
to the release across admin-password and versioned-login rotations. If it is
lost or replaced, role provisioning and cleanup intentionally stop; restore the
original Key Vault version before another rollout. Never copy a claim key between
Helm releases or edit the HMAC role comments during normal operations. A first
upgrade from the legacy predictable role comment, or an intentional claim-key
rotation, requires the exclusive, fail-closed database-administrator procedure
in `docs/runbooks/deployment.md`; it validates every direct membership and
updates all comments transactionally before cleanup is permitted. The ordinary
staging/production saved-plan policy rejects updates to the Key Vault claim
Secret and replacements of its `random_password`; use the reviewed break-glass
maintenance path rather than bypassing that guard inside a deployment run.

The MinIO root secret is intentionally immutable during ordinary Terraform
applies: MinIO has no dual-credential overlap, so changing the Key Vault value
and restarting consumers is not atomic. Changing `TF_VAR_artifact_store_secret`
after initial creation does not rotate the live credential. Treat rotation as a
reviewed maintenance operation: stop writers, update Key Vault, force CSI sync,
restart `deployment/apex-minio`, restart every APEX client, and verify read/write
and backup smoke tests before reopening traffic.

Production selects three AKS system-pool zones and PostgreSQL ZoneRedundant HA
with primary/standby in distinct zones. Verify the chosen region/SKU supports
those zones before applying `env/prod.tfvars`. `temporary_name_for_rotation`
keeps zone/SKU pool updates on AKS's supported rotation path; still review the
plan and workload PDBs before applying a production pool rotation.

Staging and production use private AKS, Redis, PostgreSQL, and Terraform-state
endpoints. Run Terraform/Helm/kubectl from a host (including the workflow's
`apex-aks` self-hosted runner group) with VNet and private-DNS reachability.
`az aks get-credentials --format exec` plus `kubelogin` uses the OIDC deployer's
Entra session; Terraform grants that identity the AKS Cluster User and Azure RBAC
Cluster Admin roles and disables local accounts. Backend calls set both
`ARM_USE_OIDC=true` and `ARM_USE_AZUREAD=true`; account-key lookup is unavailable.

The deploy workflow separates plan and apply into environment-gated jobs. The
reviewer sees the redacted plan summary and SHA-256 before approving the apply
job; the one-day binary-plan artifact is encrypted with the environment's
32+-character `APEX_TERRAFORM_PLAN_PASSPHRASE`, then decrypted, hash-verified,
and applied without re-planning. The same sequence is repeated for the independent backup state.
`scripts/terraform_plan_policy.py` blocks delete/replace actions for stateful
resources in staging/prod.

### Existing backup-account migration

Stacks created before the independent backup root still have
`azurerm_storage_account.main` in live state. Migrate once before the first new
workflow run:

1. Apply `deploy/terraform/backup` with the existing backup identity principal.
2. Run the backup smoke Job and verify the sentinel in the new account.
3. Remove the old storage account, container, and backup role assignments from
   the *live Terraform state only*. Keep the old GRS
   account untouched for at least its retention window.
4. Run the normal saved-plan workflow. It must show no deletion of the old
   account. Decommission that now-unmanaged account only through a separately
   reviewed recovery change after the new account passes a restore drill.

The ordinary plan policy intentionally blocks step 3 if it is skipped; it will
not trade backup continuity for an automated resource replacement.

After exporting a timestamped encrypted state backup, the exact legacy addresses
are:

```bash
terraform state rm \
  azurerm_role_assignment.backup_blob_data \
  azurerm_role_assignment.backup_verifier_blob_data \
  azurerm_storage_container.artifacts_backup \
  azurerm_storage_account.main
```

`state rm` does not delete Azure resources. Confirm with a following saved plan;
if any legacy account deletion remains, stop and restore the state backup.

### Existing locked-state and PostgreSQL migration

Do not disable a staging/prod state account's public endpoint until the private
path and Entra roles work. For an existing backend, apply only the Blob private
DNS zone/link, private endpoint, and `tfstate_deployer` role assignments from
`deploy/terraform/bootstrap` first. From the management runner, verify the state
hostname resolves to the private IP and that `az storage blob list --auth-mode
login` succeeds. Only then run the full bootstrap apply that disables public and
shared-key access. The supplied endpoint subnet must already permit private
endpoints.

Azure may require replacement to turn on geo-redundant backup for an existing
PostgreSQL Flexible Server. The saved-plan policy will reject that replacement.
Use a separately approved database migration instead: restore/create a new
geo-backup-enabled server, run Alembic and validation against it, stop writes,
perform the final delta/cutover, rotate both runtime and migration URIs, then
import the new server into state. Never bypass the policy and let an ordinary
deploy replace the only durable database.

Credentials come from the environment (`az login` or `ARM_*` / OIDC in CI) — no
secrets are stored in these files. `terraform plan` previews every change.
