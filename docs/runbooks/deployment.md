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
  external load.
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
