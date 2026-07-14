# Deployment runbook

Topology: ADR-0005 (one standalone image, external data services, N stateless
replicas). Kubernetes chart: `deploy/helm/apex-orchestration-engine/`. HA soak
rig: `deploy/compose-ha/`.

## Standalone server requirements

| Dependency | Purpose | Notes |
|---|---|---|
| **Postgres 16+** | LangGraph runtime tables (checkpoints/threads/runs) **and** the `apex` schema | Both `DATABASE_URI` (psycopg-style) and `APEX_DATABASE__URI` (asyncpg) usually point at the same database |
| **Redis 7** | Stream pub-sub between replicas (resumable SSE) | No durable data; safe to flush |
| **S3/MinIO** | Artifact store | Reached via catalog `connections` rows + `secret_ref` env vars, not core config |
| **`LANGGRAPH_CLOUD_LICENSE_KEY`** | The standalone server **does not boot without it**; custom auth needs Self-Hosted Enterprise | ADR-0001, ADR-0003. Fallback if declined: identity-injecting gateway (ADR-0005) |

Image: `uv run langgraph build -t apex-orchestration-engine:<tag>` — bundles
graphs, the `/v1` app, and auth handlers per `langgraph.json`. The release
workflow (`.github/workflows/release.yaml`) builds it on every `v*` tag.

## Migration procedure — alembic BEFORE rollout

The `apex` schema is migrated by Alembic and **must be at head before new pods
roll out** (new code may select new columns; old code never breaks on additive
migrations, which is the project convention).

```bash
APEX_DATABASE__URI=postgresql+asyncpg://user:pass@host:5432/apex \
  uv run alembic upgrade head
```

Order of a release rollout:

1. `alembic upgrade head` (run from CI/a job pod against the prod DB).
2. `helm upgrade <release> deploy/helm/apex-orchestration-engine --set image.tag=<tag> ...`
3. Watch the rollout: surge-first strategy (`maxUnavailable: 0`) keeps
   `replicaCount` serving throughout.
4. Smoke: `/ok`, then `GET /v1/system/info` with a real key.

The LangGraph runtime manages its own tables — first boot of a new server
version applies its internal migrations automatically.

Rollback: `helm rollback <release>` is safe for code; we do not write `alembic
downgrade` paths for prod (migrations are additive — old code runs fine on a
newer schema).

## Rolling restart safety

Mid-run restarts are safe by design, not by luck:

- Graph progress is checkpointed to Postgres; the execution phase's poll loop
  resumes from the last committed checkpoint after a restart.
- A write-ahead idempotency key in graph state plus the engines'
  **get-or-create provision contract** make a restart unable to double-start
  external load. Multi-replica deployments enable
  `APEX_DISTRIBUTED_REMOTE_CREATION_LOCK=true`, which serializes each provider
  key through PostgreSQL; APEX Load also receives its native
  `Idempotency-Key` header.
- Proof: `tests/integration/test_restart_survival.py` SIGKILLs a server
  process mid-poll and asserts the run completes with **exactly one
  `external_run_id`** across the crash boundary (run it with
  `APEX_TEST_DATABASE_URI=...`; also exercised end-to-end by the
  `deploy/compose-ha` soak).
- The PDB (`minAvailable: 1`) and surge-first update strategy keep at least
  one replica serving API/SSE during voluntary disruptions; SSE clients
  re-join streams (see `incident.md`).

So: `kubectl rollout restart deployment/<name>` is an any-time operation.
Expect at most one lost poll cycle per restarted replica; if a hard kill
leaves a thread without an active run, re-invoke the thread with empty input
to resume it (see the soak README, step 4).

## Backup

Three stores, three backups:

1. **`apex` schema** — domain data (prompts, catalog, connections, consumers,
   engine-run projections, saved queries):
   `pg_dump --schema=apex apex > apex-schema.sql`
2. **LangGraph runtime tables** — threads/runs/checkpoints (everything outside
   the `apex` schema in the same DB). Simplest: back up the whole database —
   `pg_dump apex` covers both. Point-in-time recovery on the Postgres instance
   is the preferred production posture; a thread restored from backup resumes
   from its last checkpointed state.
3. **Artifact bucket (MinIO/S3)** — `mc mirror` / bucket versioning +
   replication. Artifacts are content-addressed by the store; `apex` rows and
   checkpoints hold references, so restore the bucket alongside the DB to keep
   references resolvable.

Secrets (consumer keys) are stored only as sha256 hashes — there is nothing to
back up beyond the DB; lost plaintext keys are re-issued via rotation
(`operations.md`).

## Deploy index (local → cloud)

| Target | Command | Notes |
|---|---|---|
| Local infra only (`langgraph dev`) | `make infra-up` | Postgres/Redis/MinIO; app runs in-memory |
| Full local stack | `make compose-up` | server + dashboard + infra (`docker-compose.yaml`) |
| HA soak rig | `make compose-ha-up` | 2 replicas + nginx; needs the license env vars |
| Any Kubernetes | `make helm-install` (or `helm upgrade`) | bring your own Postgres/Redis/secrets |
| Azure AKS (turnkey) | `make aks-up` (`APEX_ENV=…`) | Terraform + ACR + Key Vault; see `aks-deployment.md` |

## Helm chart modes (full-app)

The chart (`deploy/helm/apex-orchestration-engine/`, v0.2.0) is the server **and**,
opt-in, the dashboard. Everything new is values-gated and defaults to today's
behavior.

- **Migration hook** (`migrations.enabled=true`): a pre-install/pre-upgrade Job
  runs `alembic upgrade head` on the exact image+tag before pods roll — automating
  the migrate-then-roll order above. A failure aborts the release.
- **Bootstrap hook** (`bootstrap.enabled=true`): a post-migration Job applies a
  declarative document (`apex.bootstrap`) — prompts/applications/environments/
  connections + an initial admin (key from `bootstrap.adminKeySecret`, hashed,
  never logged). The document carries `secret_ref` names only, no secret values.
- **Secret backends** (`secretBackend.mode`): `existingSecret` (default; pre-create
  the Secrets), `secretsStoreCSI` (Azure Key Vault via the CSI driver — synthesizes
  the same Secret names), or `externalSecrets` (External Secrets Operator). The env
  wiring is identical across modes.
- **ServiceAccount / RBAC / NetworkPolicy / topology spread / startup probe /
  Gateway-or-Ingress / ServiceMonitor**: all gated; see `values.yaml` comments.
- **`helm test <release>`**: an in-cluster `/ok` smoke.

### Private registry / ACR
On AKS the Terraform stack grants **AcrPull** to the kubelet identity, so no
`imagePullSecrets` are needed. Elsewhere, create one and reference it:
```bash
kubectl create secret docker-registry acr-pull \
  --docker-server=<registry> --docker-username=<u> --docker-password=<p>
helm upgrade ... --set imagePullSecrets[0].name=acr-pull
```

### Dashboard
`dashboard.enabled=true` deploys the SPA image (`apps/dashboard/Dockerfile`). One
image serves any environment: `/config.json` (`apexOrigin`/`langgraphOrigin`) is
generated at container start, and `backendUpstream` turns on an in-pod, SSE-safe
reverse proxy for `/v1`, `/threads`, `/runs`, `/assistants`, `/ok` (same-origin —
no CORS). Built/published as a separate image track in `release.yaml`.

## Secret matrix

| Secret | Compose (`docker-compose.yaml`) | Helm `existingSecret` | Key Vault (AKS) |
|---|---|---|---|
| `DATABASE_URI` (psycopg, `sslmode=require`) | `${APEX_POSTGRES_PASSWORD}` interp | `apex-database` / `DATABASE_URI` | `database-uri` |
| `APEX_DATABASE__URI` (asyncpg, `ssl=true`) | interp | `apex-database` / `APEX_DATABASE__URI` | `apex-database-uri` |
| `REDIS_URI` | static (`redis:6379`) | `apex-redis` / `REDIS_URI` | `redis-uri` |
| `LANGGRAPH_CLOUD_LICENSE_KEY` | `${LANGGRAPH_CLOUD_LICENSE_KEY}` | `apex-langgraph-license` | `langgraph-license` |
| `APEX_INTEGRATION_MINIO_SECRET_KEY` | `${APEX_INTEGRATION_MINIO_SECRET_KEY}` | `apex-minio` (via `extraEnv`) | `artifact-secret-key` |
| initial admin key | n/a | `apex-admin` (`bootstrap.adminKeySecret`) | (operator-supplied) |

Azure is the only place the psycopg/asyncpg SSL forms differ on the wire — see
`aks-deployment.md`.
