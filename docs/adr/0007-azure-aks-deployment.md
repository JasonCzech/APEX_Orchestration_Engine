# ADR-0007: Azure AKS deployment — Terraform, MinIO-on-AKS, Key Vault CSI, Gateway/Ingress

**Status:** accepted (2026-06-25)

## Decision
The turnkey Azure target provisions managed dependencies with **Terraform**
(`deploy/terraform/`, azurerm) and deploys the existing Helm chart unchanged in
shape. Choices:

- **IaC: Terraform** over Bicep — `terraform plan` previews, one toolchain across
  the deploy workflow, remote state in an Azure Storage backend bootstrapped by a
  tiny local-state module (`deploy/terraform/bootstrap/`).
- **AKS** with OIDC issuer + Workload Identity, Azure CNI Overlay + Cilium,
  Container Insights + Managed Prometheus, autoscaling system pool. The azurerm
  provider does not yet expose the managed **Automatic** SKU, so the stack
  provisions a Standard-tier cluster configured with the Automatic feature set;
  true AKS Automatic is created out of band (`az aks create --sku automatic`) and
  the chart targets either unchanged.
- **Artifact store: MinIO-on-AKS** (PVC-backed), not Azure Blob — the S3 adapter
  (`src/apex/adapters/s3/artifact_store.py`) signs S3 against a host:port and Blob
  is not S3-compatible. The Storage Account is the backup/DR target (`mc mirror`).
  A native `azure_blob` `ArtifactStorePort` adapter is a deferred follow-up.
- **Secrets: Azure Key Vault + Secrets Store CSI driver + Workload Identity.** The
  chart's `secretBackend.mode=secretsStoreCSI` emits a `SecretProviderClass` whose
  `secretObjects` synthesize the *same* K8s Secret names/keys the deployment
  already consumes — so the `existingSecret` env wiring is unchanged; only the
  producer differs. No app code reads Key Vault directly.
- **Routing: managed NGINX ingress** (app-routing add-on) with SSE-safe
  annotations (buffering off, long read timeouts), with a Gateway API HTTPRoute as
  an alternative (`gateway.enabled`). AGIC is avoided — App Gateway buffering
  fights SSE.
- **Migrate + bootstrap as Helm pre-upgrade hooks** so a single `helm upgrade`
  enforces migrate-then-roll (ADR-0005) and seeds the catalog + initial admin.

## Rationale
The connectivity contract is four secrets + an artifact key (ADR-0005). Mapping
each onto a managed Azure service and delivering them through CSI-synthesized
Secrets keeps the chart a pure consumer while making the deployment turnkey: one
command (`scripts/deploy.py aks-up` / the `deploy-aks` workflow) provisions,
builds + pushes to ACR (no pull secrets — AcrPull on the kubelet identity), runs
migrations + bootstrap, and rolls. Workload Identity removes long-lived
credentials end to end (CI uses OIDC federation; pods use a federated UAMI).

## Consequences
- **Postgres URI footgun:** psycopg/libpq (`DATABASE_URI`) uses
  `?sslmode=verify-full&sslrootcert=system`; asyncpg (`APEX_DATABASE__URI`) uses
  `?sslmode=verify-full`, which APEX maps to a hostname-verifying system-CA
  context — Terraform emits both.
- **License gate (ADR-0005) stands:** the server crash-loops without
  `LANGGRAPH_CLOUD_LICENSE_KEY`; it is stored in Key Vault and must be present
  before prod pods start. The identity-injecting-gateway fallback remains the
  pivot if the Enterprise license is declined.
- MinIO is a stateful in-cluster dependency to operate (PVC backups), the one
  exception to "external data services" — justified by zero app change.
- The `Automatic` SKU gap is a provider limitation, documented in the Terraform
  README; revisit when azurerm/azapi support lands.
