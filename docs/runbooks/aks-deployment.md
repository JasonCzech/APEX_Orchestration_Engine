# AKS deployment runbook

Turnkey deploy of APEX (server + dashboard) to Azure AKS. Topology: ADR-0005
(stateless replicas, external data services) + ADR-0007 (Terraform, MinIO-on-AKS,
Key Vault CSI, managed NGINX). IaC: `deploy/terraform/`. Chart:
`deploy/helm/apex-orchestration-engine/` with overlay `deploy/azure/helm/values-azure.yaml`.

## Prerequisites
- Azure subscription; rights to create resource groups + role assignments.
- `az`, `kubectl`, `helm`, `terraform`, `uv`, Docker on PATH (or use the
  `deploy-aks` GitHub workflow, which provides them).
- A Self-Hosted Enterprise **`LANGGRAPH_CLOUD_LICENSE_KEY`** (the server does not
  boot without it — ADR-0001/0005). Without it, stop and use the gateway fallback.
- Chosen region and a strong Postgres admin password.

## What gets provisioned (Terraform)
Resource group, VNet (+ delegated PG/private-endpoint subnets and private DNS), AKS (OIDC + Workload
Identity, Cilium overlay, Container Insights + Managed Prometheus, autoscaling),
ACR (+ AcrPull to the kubelet identity), PostgreSQL Flexible Server 16 with an
explicit PITR/geo-backup policy, Azure Cache for Redis (TLS-only and private
outside dev), an independently stateful/locked GRS backup account, separate runtime and
hook Key Vaults, and independent runtime/hook/backup managed identities federated
to their least-privilege ServiceAccounts. Production spans the AKS pool across
three zones, enables zone-redundant PostgreSQL HA, and retains geo-redundant
database backups for 35 days. See
`deploy/terraform/README.md`.

## Operator inputs
| Input | Where |
|---|---|
| subscription / region / naming | `az login`; `deploy/terraform/env/<env>.tfvars` |
| reviewed AKS version | `APEX_KUBERNETES_VERSION` locally or environment variable `AZURE_KUBERNETES_VERSION` in the workflow; required for staging/prod |
| Postgres admin password | `TF_VAR_postgres_admin_password` (env / CI secret) |
| Enterprise license (required) | `TF_VAR_langgraph_license_key` → runtime Key Vault `langgraph-license` |
| MinIO secret (required, 24+ chars) | `TF_VAR_artifact_store_secret` → runtime Key Vault `artifact-secret-key` |
| initial admin / API-key pepper | optional inputs; secure values are generated when omitted and stored in the hook/runtime vaults |
| ingress host / CORS origins | `deploy/azure/helm/values-azure.yaml` (`REPLACE_ME`) |
| backup alert destinations | `TF_VAR_backup_alert_action_group_ids`; at least one Action Group is required in staging/prod while monitoring is enabled |
| dynamic chart values (ACR, KV, client id) | injected by `deploy.py` / workflow from TF outputs |

## One-command bootstrap
```bash
# State backend (once per env). For staging/prod, pass the private-endpoint
# subnet, DNS-linked VNet, and GitHub OIDC principal object ID used by the
# apex-aks runner (see Terraform README):
cd deploy/terraform/bootstrap && terraform init && terraform apply -var environment=dev
# Initialize both main and backup state keys with use_azuread_auth=true, then:
export APEX_ENV=dev APEX_TAG=v0.6.0
export APEX_KUBERNETES_VERSION=1.x.y # choose a currently supported, reviewed AKS version
export APEX_HOSTNAME=apex.example.com APEX_TLS_SECRET=apex-tls
# Fresh namespaces: provide a matching PEM pair; later runs may reuse the Secret.
export APEX_TLS_CERTIFICATE_FILE=/secure/path/tls.crt
export APEX_TLS_PRIVATE_KEY_FILE=/secure/path/tls.key
export TF_VAR_postgres_admin_password=... TF_VAR_langgraph_license_key=... TF_VAR_artifact_store_secret=...
make aks-up        # saved-plan policy/apply -> protected backup state -> images/Helm -> smoke
```
CI equivalent: run the **deploy-aks** workflow (`workflow_dispatch`, pick the
environment). Configure `APEX_TLS_CERTIFICATE` and `APEX_TLS_PRIVATE_KEY` in the
GitHub Environment for a first deployment, plus a unique 32+-character
`APEX_TERRAFORM_PLAN_PASSPHRASE` used to encrypt the short-lived binary plan
artifact. Terraform plan/apply and the deploy
job run on an
`apex-aks` self-hosted runner group with private DNS/VNet reachability,
`kubectl`, and `kubelogin`; OIDC + Entra/Azure RBAC replaces static kubeconfig
credentials. Configure required reviewers: planning produces a redacted summary
and SHA-256; a later environment-gated job downloads, verifies, and applies that
exact one-day plan artifact. The backup root receives its own plan/approval and
state key. Never approve a plan containing a protected delete/replace—the policy
normally rejects it before approval.

## Secret and identity flow
The public server identity can read only the runtime vault. Its
`SecretProviderClass` synchronizes non-database runtime Secrets (`apex-redis`,
`apex-langgraph-license`, `apex-auth`, `apex-minio`) while the same pod mounts
that CSI volume. Database candidates live only in the hook vault: after creating
and connecting with a versioned login, the role hook promotes both URIs into the
separate `apex-database-active` Secret. The Blob backup gets no Key Vault access:
Azure Workload Identity and a container-scoped data role replace storage account
keys. Rotation triggers a generation-annotated pod rollout for environment vars.

The MinIO root credential is the exception: MinIO cannot overlap two root
passwords, so Terraform freezes `artifact-secret-key` during ordinary applies.
Rotate it only in a maintenance window with writers stopped, then force CSI
sync, restart MinIO and all APEX clients, and pass artifact read/write plus
backup smoke tests before restoring traffic.

> **psycopg vs asyncpg TLS (the #1 footgun):** both connection paths require CA
> and hostname verification. `DATABASE_URI` (psycopg/libpq) uses
> `?sslmode=verify-full&sslrootcert=system`; `APEX_DATABASE__URI` (asyncpg) uses
> `?sslmode=verify-full`, which APEX maps to Python's verifying system-CA context.
> Terraform emits both correctly — preserve the distinction if you hand-edit.
> `REDIS_URI` uses `rediss://`; redis-py verifies the certificate and hostname by
> default, and locked environments reject query parameters that weaken either check.

## Migrate-then-roll
The first hook uses the admin URI to create stable NOLOGIN owner roles and
password-generation login roles (`apex_runtime_v*`, `apex_migration_v*`). The
new login is proven before rollout while the previous login remains valid. A
pod-template generation token makes Helm finish that rollout before a post-hook
retires stale logins, so password replacement is blue/green rather than an
in-place `ALTER ROLE` outage. A failed pre-hook leaves the previous active Secret
untouched, including for crash-restarted old pods. Alembic then runs as the migration/schema owner,
post-migration grants/default privileges run through that same role, and
bootstrap runs with runtime DML credentials. On the first role-split upgrade,
the admin hook transfers only non-extension relations in `apex` to the migration
owner and in `public` to the LangGraph runtime owner; no database-wide CREATE or
admin grant remains. A hook failure aborts the release. The Deployment then runs
the exact-image `schema-readiness` init container, and the app lifespan repeats
the same Alembic-head/DB check before starting reconcilers. A stale or missing
`apex.alembic_version` therefore keeps the pod unready even when migrations are
managed out of band. See `deployment.md`.

External Secrets Operator requires a two-stage first install: install with
`databaseRoleProvisioning.enabled=false`, `migrations.enabled=false`, and
`bootstrap.enabled=false`; wait for all generated Secrets to be Ready, then
upgrade with the hooks enabled. The chart rejects the unsafe first-install
combination. On the transition upgrade away from CSI, keep those three hooks
disabled and leave `secretBackend.csi.cleanup.enabled=true`; after ESO is Ready,
reenable the hooks in a second upgrade and disable CSI cleanup.
When retaining the Azure overlay, configure ESO targets for
`apex-database-active`, `apex-database-admin`, `apex-database-bootstrap`, and
`apex-database-migration` with the keys declared in `values-azure.yaml`.

## Ingress + SSE
Managed NGINX (`ingress.className: webapprouting.kubernetes.azure.com`) with
`proxy-buffering: "off"` and long read/send timeouts. SSE stalls almost always mean
a buffering hop — check every proxy in the path. Both deploy paths wait for an
ingress address, assert that exact class, and call the public `https://<host>/ok`
endpoint after the in-cluster Helm test. Gateway API is the alternative
(`gateway.enabled`); verify its BackendTrafficPolicy disables buffering too.

## Artifact store
MinIO-on-AKS (`deploy/azure/k8s/minio/minio.yaml` plus the post-Helm
`networkpolicy.yaml`), reached via the bootstrapped
`minio-artifacts` connection at `apex-minio:9000`. The archived MinIO OSS process
binds S3 and console listeners only to pod loopback. A non-root gateway is the
only Service target and rejects unsigned-trailer uploads (CVE-2026-41145) and S3
Select POSTs (CVE-2026-39414), the complete reverse-proxy workarounds from the
[write-bypass advisory](https://github.com/minio/minio/security/advisories/GHSA-hv4r-mvr4-25vw)
and [memory-exhaustion advisory](https://github.com/minio/minio/security/advisories/GHSA-h749-fxx7-pwpg).
There is no console Service, and NetworkPolicy permits gateway ingress only from
the `apex` chart release and the backup job. Keep these controls until the store
is migrated to a supported patched S3 implementation; never expose the raw OSS
listener.

A CronJob uses rclone plus Azure Workload Identity to sync into the private Blob
container without a shared
storage key; its account name comes from `apex-minio-backup-config`. The target
is in a separate Terraform state and delete-locked resource group. Every deploy
writes a sentinel, runs the CronJob immediately, and verifies that object through
the Blob data plane before reporting success. Restore the bucket alongside the
DB so references resolve (see `deployment.md` Backup). The six-hour CronJob has
a 15-minute scheduling deadline, a 90-minute Job deadline, bounded rclone
connection/operation timeouts, and finite retries, so one hung copy cannot block
the next scheduled run. A deletion cap fails closed on a surprising source purge;
Blob soft delete/versioning preserves recovery points, and the management policy
expires superseded versions at the configured retention boundary.

### Artifact backup alerts

Azure Monitor evaluates two severity-1 rules against Container Insights:

- any `apex-minio-backup-*` pod entering `Failed` in the last 30 minutes;
- no backup pod logs in seven hours (the schedule is every six hours).

Set `backup_alert_action_group_ids` to page the owning team; staging/prod plans
fail closed when monitoring is enabled without a destination. When either alert
fires, inspect `kubectl -n apex get jobs`, describe the latest Job, and read its
`mirror` logs. Do not delete the MinIO PVC. Correct identity/network/capacity,
create a one-off Job from the CronJob, wait for completion, and verify a sentinel
through `az storage blob exists --auth-mode login` before resolving the alert.
Development may intentionally leave the list empty for portal-only evaluation.

## Scaling / HA
`autoscaling.enabled` (HPA), the node autoscaler, the PDB (`minAvailable: 1`), and
soft zone topology spread. Mid-run rolling restarts are safe (checkpoint-durable).

## Day-2
- **Rollback:** `helm rollback <release>` or an older-image Helm upgrade is safe
  to a lineage-aware release (additive migrations and registered descendant
  heads); its migration runner proves compatibility and skips instead of asking
  old Alembic code to resolve a future revision. Never downgrade the
  production schema, and never roll back across the pre-lineage adoption boundary;
  roll forward with a fixed image instead.
- **Teardown:** `make aks-down` creates a destroy plan. Staging/prod require the
  exact `destroy:<environment>:<full-plan-sha256>` token through
  `APEX_TERRAFORM_DESTROY_CONFIRM`; the command applies that saved plan without
  `-auto-approve`. The independently stateful backup account is not destroyed.
- **Cost:** dev uses Free tier + Basic Redis + public PG; prod uses Standard tier +
  Premium Redis + private PG (`env/*.tfvars`).

## Troubleshooting
- **Pods crash-loop at boot:** missing/invalid `LANGGRAPH_CLOUD_LICENSE_KEY`.
- **Pod stuck in `schema-readiness`:** inspect `apex.alembic_version` and
  `apex.alembic_revision_lineage`, then rerun the migration hook from the trusted
  target image. A packaged head or registered descendant is accepted; behind,
  divergent, mutated, or unknown lineage is intentionally rejected. Never bypass
  the init check.
- **Secrets empty:** the CSI volume must be mounted for `secretObjects` to sync;
  confirm the SecretProviderClass and the workload identity client id.
- **PG unreachable from a laptop:** private PG — run `alembic`/`bootstrap` in-cluster
  (the hooks do) or use a peered jumpbox; or set `postgres_public_access=true` (dev).
- **Terraform state unreachable:** locked state has no public/shared-key path.
  Run from the configured management VNet, verify `privatelink.blob.core.windows.net`
  DNS, and authenticate with OIDC/AzureAD Blob Data Contributor.
- **Redis unreachable:** staging/prod resolve the normal Redis hostname through
  `privatelink.redis.cache.windows.net`; verify the private DNS link and endpoint.
- **SSE stalls:** a buffering hop (ingress/gateway/proxy).
