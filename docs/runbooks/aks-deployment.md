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
Resource group, VNet (+ delegated PG subnet, private DNS), AKS (OIDC + Workload
Identity, Cilium overlay, Container Insights + Managed Prometheus, autoscaling),
ACR (+ AcrPull to the kubelet identity), PostgreSQL Flexible Server 16, Azure Cache
for Redis (TLS-only), Storage Account (MinIO backup target), Key Vault (+ the
secrets), a user-assigned identity federated to the workload ServiceAccount, and
Log Analytics. See `deploy/terraform/README.md`.

## Operator inputs
| Input | Where |
|---|---|
| subscription / region / naming | `az login`; `deploy/terraform/env/<env>.tfvars` |
| Postgres admin password | `TF_VAR_postgres_admin_password` (env / CI secret) |
| Enterprise license | `TF_VAR_langgraph_license_key` → Key Vault `langgraph-license` |
| MinIO secret | `TF_VAR_artifact_store_secret` → Key Vault `artifact-secret-key` |
| ingress host / CORS origins | `deploy/azure/helm/values-azure.yaml` (`REPLACE_ME`) |
| dynamic chart values (ACR, KV, client id) | injected by `deploy.py` / workflow from TF outputs |

## One-command bootstrap
```bash
# State backend (once per env):
cd deploy/terraform/bootstrap && terraform init && terraform apply -var environment=dev
# Main stack init (see deploy/terraform/README.md for backend-config), then:
export APEX_ENV=dev APEX_TAG=v0.6.0
export TF_VAR_postgres_admin_password=... TF_VAR_langgraph_license_key=... TF_VAR_artifact_store_secret=...
make aks-up        # terraform apply -> build/push to ACR -> deploy MinIO -> helm upgrade (+hooks) -> smoke
```
CI equivalent: run the **deploy-aks** workflow (`workflow_dispatch`, pick the
environment). It uses OIDC federated login — no stored cloud credentials.

## Secret flow
Key Vault → `SecretProviderClass` (`secretObjects`) → native K8s Secrets
(`apex-database`, `apex-redis`, `apex-langgraph-license`, `apex-minio`) → pod env.
The pod mounts the CSI volume to trigger the sync; rotation = update the KV secret,
then restart the pods.

> **psycopg vs asyncpg SSL (the #1 footgun):** `DATABASE_URI` (psycopg) uses
> `?sslmode=require`; `APEX_DATABASE__URI` (asyncpg) uses `?ssl=true`. Terraform
> emits both correctly — preserve the distinction if you hand-edit.

## Migrate-then-roll
`migrations.enabled=true` runs `alembic upgrade head` as a pre-upgrade hook before
pods roll; `bootstrap.enabled=true` then seeds the catalog + initial admin (key
from the `apex-admin` Secret). A hook failure aborts the release — new pods never
start against an un-migrated schema. See `deployment.md`.

## Ingress + SSE
Managed NGINX (`ingress.className: webapprouting.kubernetes.io`) with
`proxy-buffering: "off"` and long read/send timeouts. SSE stalls almost always mean
a buffering hop — check every proxy in the path. Gateway API is the alternative
(`gateway.enabled`); verify its BackendTrafficPolicy disables buffering too.

## Artifact store
MinIO-on-AKS (`deploy/azure/k8s/minio/minio.yaml`), reached via the bootstrapped
`minio-artifacts` connection at `apex-minio.<ns>.svc.cluster.local:9000`. Back up
to the Storage Account with `mc mirror`; restore alongside the DB so references
resolve (see `deployment.md` Backup).

## Scaling / HA
`autoscaling.enabled` (HPA), the node autoscaler, the PDB (`minAvailable: 1`), and
soft zone topology spread. Mid-run rolling restarts are safe (checkpoint-durable).

## Day-2
- **Rollback:** `helm rollback <release>` (additive migrations → old code is safe).
- **Teardown:** `make aks-down` (`terraform destroy`).
- **Cost:** dev uses Free tier + Basic Redis + public PG; prod uses Standard tier +
  Premium Redis + private PG (`env/*.tfvars`).

## Troubleshooting
- **Pods crash-loop at boot:** missing/invalid `LANGGRAPH_CLOUD_LICENSE_KEY`.
- **Secrets empty:** the CSI volume must be mounted for `secretObjects` to sync;
  confirm the SecretProviderClass and the workload identity client id.
- **PG unreachable from a laptop:** private PG — run `alembic`/`bootstrap` in-cluster
  (the hooks do) or use a peered jumpbox; or set `postgres_public_access=true` (dev).
- **SSE stalls:** a buffering hop (ingress/gateway/proxy).
