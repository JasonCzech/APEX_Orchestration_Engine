# Incident runbook

What breaks how, and what still works. The design principle behind every
degraded mode below: **checkpointed graph state in Postgres is the only source
of truth** — everything else (projections, usage events, caches) is best-effort
and self-heals or is rebuildable.

## Postgres down / unreachable

Postgres is the one dependency the platform cannot truly run without — but it
fails *soft* in specific, designed ways. Triage by surface:

### What hard-fails

- **New runs and checkpoint writes** — the LangGraph runtime checkpoints every
  step to Postgres. New `POST /threads/.../runs` fail; in-flight runs error
  when they next try to commit a checkpoint.
- **Domain reads/writes** (`/v1/prompts`, `/v1/admin/*`, `/v1/pipelines` lists,
  `/v1/analytics/usage`) — 5xx problem details.
- **Real consumer keys** — identity is resolved from `apex.api_consumers`; the
  lookup error is swallowed and logged (`apex.auth.db_lookup_failed`) and the
  request gets a **401**, not a 500.

### What keeps working (the degraded modes)

- **Auth dev-key path** — `APEX_AUTH__DEV_API_KEY`, when configured, matches
  *before* the DB lookup and yields a synthetic admin identity with no DB
  round-trip (`src/apex/auth/service.py`). This is the break-glass credential
  for diagnosing an outage; do not configure it in production unless you accept
  that trade.
- **Connection resolver static fallback** — adapter resolution falls back to
  the in-code `DEV_CONNECTIONS` map (stub adapters + sim engine) with a logged
  warning when the `connections` table is unreachable
  (`src/apex/services/connections.py`). Phases keep executing rather than
  crashing — but against **stubs**, so treat post-outage results from that
  window as suspect.
- **Best-effort projections** — `engine_runs` rows
  (`src/apex/services/engine_runs.py`) and usage events
  (`src/apex/services/usage.py`) are written best-effort: every DB error is
  swallowed and logged, never failing a pipeline run. During an outage these
  writes are simply lost.

### Recovery checklist

1. Restore Postgres (or fail over).
2. No server restart is needed for auth or domain endpoints — the resolver and
   routers open connections per request.
3. Threads that errored mid-run resume from their last committed checkpoint:
   re-invoke with empty input (`POST /threads/{thread_id}/runs` with just the
   assistant id). The write-ahead idempotency key + get-or-create provision
   contract prevent double-started load (see `deployment.md`, rolling restart
   safety).
4. Expect gaps in `engine_runs` / usage analytics for the outage window. If an
   engine-run row is missing for a live thread, the abort path still works —
   it reads the handle from checkpointed thread state first and only falls back
   to the projection (`operations.md`).

## Redis down

Redis carries stream pub-sub between replicas (resumable SSE) — no durable
data. Symptoms: runs execute and checkpoint normally, but live event streams
stall or cannot be joined across replicas. Restore Redis; clients re-join (see
below). Nothing to replay or repair afterward.

## SSE disconnects

Streams are resumable by design (Redis-backed): a dropped connection loses the
transport, not the run.

- **Client fix**: re-join the run's stream — LangGraph SDK
  `client.runs.joinStream(threadId, runId)` (HTTP:
  `GET /threads/{thread_id}/runs/{run_id}/stream`). The run itself never
  noticed the disconnect; current state is always recoverable from
  `GET /v1/pipelines/{thread_id}` even without a stream.
- **Recurring disconnects/stalls** point at a buffering proxy hop, not the
  server. Every hop must disable response buffering and allow long reads —
  see `deploy/compose-ha/nginx.conf` (`proxy_buffering off`,
  `proxy_read_timeout 24h`) and the ingress annotation examples in the Helm
  chart's `values.yaml`.
- A rolling restart mid-stream behaves like any disconnect: re-join via the
  surviving replica (the PDB keeps one serving).

## Licensing

The standalone server **does not boot** without a valid
`LANGGRAPH_CLOUD_LICENSE_KEY`, and the custom auth handlers require the
Self-Hosted Enterprise tier (ADR-0001, ADR-0003).

- **Symptom**: pods crash-loop immediately at startup after a deploy or a
  secret change; `/ok` never comes up. Existing healthy pods keep serving — do
  not delete them while diagnosing.
- **Check**: the secret named by the chart's `license.existingSecret` exists in
  the namespace and holds a current key; `kubectl logs` on a crashed pod shows
  the license error explicitly.
- **Remedy**: fix/renew the key, restart the rollout. If the license decision
  is reversed entirely, the documented fallback is the thin identity-injecting
  gateway in front of an unlicensed-tier server (ADR-0005 / ADR-0001) — an
  architecture change, not an incident fix.
- Local development is never blocked: `langgraph dev` requires no license.
