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
graphs, the `/v1` app, and auth handlers on the pinned LangGraph API 0.10.0 base
per `langgraph.json`; local `.env` files are excluded from the image. The release
workflow (`.github/workflows/release.yaml`) builds it on every `v*` tag.

## Migration procedure — compatibility-aware Alembic before rollout

The `apex` schema is migrated by Alembic and **must be at head before new pods
roll out** (new code may select new columns; old code never breaks on additive
migrations, which is the project convention).

The migration runner records the immutable ancestry of every packaged revision
in `apex.alembic_revision_lineage` before it changes the Alembic head (and refreshes
the registry even when there are no revisions to apply). The chart's
`schema-readiness` init container and the application lifespan then require every
database head to be either the packaged head or a registered descendant of it.
Missing, behind, divergent, cyclic, mutated, and unregistered heads fail closed,
so `/ok` cannot make an incompatible-schema pod Ready while an older compatible
image can still start during a rollout or code rollback.

```bash
APEX_DATABASE__URI=postgresql+asyncpg://user:pass@host:5432/apex \
  uv run python -m apex.persistence.migrate
```

Order of a release rollout:

1. `python -m apex.persistence.migrate` (run from CI/a job pod against the prod DB).
2. `helm upgrade <release> deploy/helm/apex-orchestration-engine --set image.tag=<tag> ... --wait`
3. Watch the rollout: surge-first strategy (`maxUnavailable: 0`) keeps
   `replicaCount` serving throughout.
4. Smoke: `/ok`, then `GET /v1/system/info` with a real key.

The LangGraph runtime manages its own tables — first boot of a new server
version applies its internal migrations automatically.

Rollback: `helm rollback <release>` is safe for code when the target image contains
the compatibility-aware lineage gate; do not run `alembic downgrade` in prod.
Images released before the lineage gate required exact head equality and are not
valid rollback targets after the database advances. Treat the first lineage-aware
release as the rollback baseline and roll forward with a fixed image if an older
legacy release is the only alternative.

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
   `pg_dump apex` covers both. Azure production Terraform sets a 35-day PITR
   window and geo-redundant backups explicitly; a thread restored from backup resumes
   from its last checkpointed state.
3. **Artifact bucket (MinIO/S3)** — the AKS CronJob copies to a GRS/versioned
   account in a separately stateful, delete-locked resource group. Artifacts are content-addressed by the store; `apex` rows and
   checkpoints hold references, so restore the bucket alongside the DB to keep
   references resolvable.

Secrets (consumer keys) are stored only as sha256 hashes — there is nothing to
back up beyond the DB; lost plaintext keys are re-issued via rotation
(`operations.md`).

Run a quarterly restore drill, and after any recovery-policy change:

1. Restore PostgreSQL to a new server at a selected point inside the PITR window.
2. Copy the Blob backup into an isolated MinIO bucket with the backup workload
   identity (never overwrite the live bucket during a drill).
3. Start an isolated APEX release against both restored stores, confirm the
   schema-head gate passes, sample referenced artifacts, and resume a checkpointed
   thread without creating a duplicate external run.
4. Record RPO/RTO and delete drill resources only after evidence is retained.

GRS/versioning protects media failures and accidental object deletion; the
separate Terraform state/resource group plus delete lock protects against a live
stack destroy. None substitutes for the restore drill.

## Deploy index (local → cloud)

| Target | Command | Notes |
|---|---|---|
| Local infra only (`langgraph dev`) | `make infra-up` | Postgres/Redis/MinIO; app runs in-memory |
| Full local stack | `make compose-up` | builds the server, then migrates + bootstraps server/dashboard/infra (`docker-compose.yaml`) |
| HA soak rig | `make compose-ha-up` | builds, migrates, bootstraps, then starts 2 healthy replicas + nginx; needs license and rig admin key |
| Any Kubernetes | `make helm-install` (or `helm upgrade`) | bring your own Postgres/Redis/secrets |
| Azure AKS (turnkey) | `make aks-up` (`APEX_ENV=…`) | Terraform + ACR + Key Vault; see `aks-deployment.md` |

## Helm chart modes (full-app)

The chart (`deploy/helm/apex-orchestration-engine/`, v0.2.0) is the server **and**,
opt-in, the dashboard. Everything new is values-gated and defaults to today's
behavior.

- **Migration hook** (`migrations.enabled=true`): a pre-install/pre-upgrade Job
  runs `python -m apex.persistence.migrate` on the exact image+tag before pods roll — automating
  the migrate-then-roll order above. A failure aborts the release.
- **Database-role generation cleanup**
  (`databaseRoleProvisioning.cleanupOldGenerations=true`): opt in only when every
  upgrade and rollback uses `--wait`. Its post-hook retires old login roles after
  the replacement Deployment is Ready; the generic default is manual cleanup so
  a non-waiting Helm client cannot revoke credentials from still-serving pods.
  Runtime and migration owner-role names must be unique to a Helm release within
  a database. The hook binds each owner and generation comment to both
  `<namespace>/<release>` and the stable, hook-only
  `databaseRoleProvisioning.claimSecret` HMAC key; a predictable release name is
  not ownership proof. Preserve that Secret across database-admin password and
  runtime/migration login rotations. Losing or changing it deliberately makes
  provisioning and cleanup fail closed; restore the original key through the
  secret backend before upgrading rather than editing role comments or enabling
  automatic adoption. The migration, post-migration grants, and cleanup hooks
  independently revalidate the exact owner, every direct generation, and the
  `apex` schema HMAC immediately before privileged work; hook ordering alone is
  not treated as continuity proof. Migration retains the database-role advisory
  lock on the exact Alembic connection from claim verification through DDL and
  the final compatibility check. This serializes cooperating release hooks; as
  with any database system, a compromised server administrator who deliberately
  ignores the control-plane protocol is outside the workload isolation boundary.
  Rotation creates new versioned logins, verifies them, rolls every pod to
  `credentialGeneration`, and only then retires prior claimed generations.
  Unrelated roles and public-schema objects are never adopted.
- **Schema readiness** (`schemaReadiness.enabled=true`): an init container and
  the app lifespan both require the packaged Alembic head or a proven registered
  descendant. `/ok` remains process liveness; the unauthenticated, opaque
  `/ready` probe continuously rechecks schema access, shared Redis admission,
  and required reconcilers before the pod receives traffic. Each reconciler
  advances a bounded progress heartbeat while draining its batch, so an alive
  but wedged worker also removes the pod from service.
- **Bootstrap hook** (`bootstrap.enabled=true`): a post-migration Job applies a
  declarative document (`apex.bootstrap`) — prompts/applications/environments/
  connections + an initial admin (key from `bootstrap.adminKeySecret`, hashed,
  never logged). The document carries `secret_ref` names only, no secret values.
- **Secret backends** (`secretBackend.mode`): `existingSecret` (default; pre-create
  the Secrets), `secretsStoreCSI` (Azure Key Vault via the CSI driver — synthesizes
  the same Secret names), or `externalSecrets` (External Secrets Operator). The env
  wiring is identical across modes.
- **Locked configuration contract:** production defaults require an `apex-auth`
  pepper Secret, TLS DB/Redis URIs, and explicit HTTPS CORS origins. The
  LangGraph `CORS_CONFIG` origins and authenticated/SSE headers must match
  `APEX_CORS_ORIGINS`; the chart derives this automatically. Bootstrap
  receives the same non-secret settings and hook-only pepper so it cannot bypass
  production validation. With External Secrets, use the documented two-stage
  first install: disable database-role provisioning, migrations, and bootstrap,
  wait for every ExternalSecret to be Ready, then reenable them on an upgrade.
  For a CSI-to-ESO transition, run CSI cleanup in that hook-free first stage.
- **ServiceAccount / RBAC / NetworkPolicy / topology spread / startup probe /
  Gateway-or-Ingress / ServiceMonitor**: all gated; see `values.yaml` comments.
- **`helm test <release>`**: an in-cluster dependency-aware `/ready` smoke.

### Database-role claim migration and recovery

The HMAC claim is intentionally not backward-compatible with the old,
predictable `apex-role-owner:<namespace>/<release>` comment. The first upgrade
from that format must be an explicit database-administrator maintenance event;
the hook will not auto-adopt a role based on its name, prefix, comment, or
objects in `public`.

1. Stop all Helm operations for the database, leave old-generation cleanup
   disabled, and hold PostgreSQL advisory lock `4706337856242535493` for the
   maintenance session.
2. Inventory the two configured stable owners and every **direct** member from
   `pg_auth_members`. Verify that stable owners are safe `NOLOGIN` roles with no
   parent memberships, and that each generation is a safe `LOGIN` role whose
   only parent is its expected owner and which has no members. Establish
   ownership from deployment records and database audit history, not from a
   configurable role prefix. If any role is ambiguous, do not claim it: create
   new release-unique roles and explicitly reassign only independently verified
   APEX objects. Independently verify that the `apex` schema is already owned by
   the validated migration owner; never take an existing schema from a different
   owner based on its well-known name.
3. Create and recovery-protect a dedicated random claim key of at least 32
   bytes in the hook secret backend. For each verified role and the verified
   `apex` schema, calculate
   `digest = HMAC-SHA256(key, "apex-role-claim-v2:<namespace>/<release>:<kind>:<name>")`
   and set its exact comment to
   `apex-role-claim-v2:<namespace>/<release>:<kind>:<digest>`. Valid kinds are
   `runtime-owner`, `migration-owner`, `runtime-generation`, and
   `migration-generation` for roles, plus `schema` for the `apex` schema. Apply
   all comments in one database transaction while retaining the advisory lock;
   never put the key in SQL, process arguments, or an operator transcript.
4. Populate `databaseRoleProvisioning.claimSecret`, run one upgrade with cleanup
   still disabled, and verify provisioning plus the `/ready` rollout before
   enabling cleanup on a later upgrade.

If the current claim key is lost, restoring its previous secret version is the
safe recovery. An intentional key rotation requires the same exclusive
maintenance window: preserve the old key, revalidate every owner and direct
generation plus the `apex` schema owner, atomically replace all role and schema
comments with digests from the new key while the advisory lock is held, then
update the hook secret. Run provisioning with cleanup disabled before allowing
a cleanup hook. Never overlap old-key and new-key Helm jobs, copy a key between
releases, or rotate the key as an ordinary credential refresh.

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
reverse proxy for `/v1`, `/threads`, `/runs`, `/assistants`, `/ok`, and `/ready`
(same-origin — no CORS). Built/published as a separate image track in
`release.yaml`.

## Secret matrix

| Secret | Compose (`docker-compose.yaml`) | Helm `existingSecret` | Key Vault (AKS) |
|---|---|---|---|
| `DATABASE_URI` (psycopg, `sslmode=verify-full&sslrootcert=system`) | `${APEX_POSTGRES_PASSWORD}` interp | `apex-database` / `DATABASE_URI` | `database-uri` |
| `APEX_DATABASE__URI` (asyncpg, `sslmode=verify-full`) | interp | `apex-database` / `APEX_DATABASE__URI` | `apex-database-uri` |
| `REDIS_URI` | static (`redis:6379`) | `apex-redis` / `REDIS_URI` | `redis-uri` |
| `LANGGRAPH_CLOUD_LICENSE_KEY` | `${LANGGRAPH_CLOUD_LICENSE_KEY}` | `apex-langgraph-license` | `langgraph-license` |
| `APEX_INTEGRATION_MINIO_SECRET_KEY` | `${APEX_INTEGRATION_MINIO_SECRET_KEY}` | `apex-minio` (via `extraEnv`) | `artifact-secret-key` |
| API-key hash pepper | n/a in unlocked Compose | `apex-auth` | `api-key-hash-pepper` (runtime + hook vault copy) |
| initial admin key | `${APEX_BOOTSTRAP_ADMIN_KEY}` (HA reuses rig dev key) | `apex-admin` (`bootstrap.adminKeySecret`) | `bootstrap-admin-key` (hook vault only) |

Azure is the only place the psycopg/asyncpg TLS forms differ on the wire — see
`aks-deployment.md`.
