# HA soak rig — rolling restarts with zero lost pipelines

Two stateless `apex-server` replicas behind an nginx round-robin (SSE-safe:
`proxy_buffering off`), sharing one Postgres/Redis/MinIO. This is the M6 GA
demo rig: start a long run, rolling-restart the replicas underneath it, and
verify nothing is lost — the execution poll loop resumes from the last
committed Postgres checkpoint without double-starting load (the same property
asserted by `tests/integration/test_restart_survival.py`).

## Hard prerequisite: the license decision

`LANGGRAPH_CLOUD_LICENSE_KEY` (Self-Hosted **Enterprise** — custom auth requires
that tier; ADR-0001, ADR-0003). The rig is ready to run but **gated on this
key**: `docker compose` fails fast with a clear error if the variable is unset.
Without the license decision, the equivalent coverage is the Postgres-gated
integration test above, which exercises the same checkpoint-durability
mechanics in-process.

## Soak procedure

All commands from the repo root.

### 1. Build and boot

```bash
export LANGGRAPH_CLOUD_LICENSE_KEY=...        # Enterprise key
export APEX_IMAGE=apex-orchestration-engine:local
uv run langgraph build -t "$APEX_IMAGE"
docker compose -f deploy/compose-ha/docker-compose.ha.yaml up -d --wait
```

### 2. Migrate the apex schema (Postgres is mapped to host port 5433)

```bash
APEX_DATABASE__URI=postgresql+asyncpg://apex:apex@localhost:5433/apex \
  uv run alembic upgrade head
```

The LangGraph runtime creates/migrates its own tables on first server boot.
Auth: the rig sets `APEX_AUTH__DEV_API_KEY=soak-dev-key` (synthetic admin, no
DB row needed) so every call below just sends `x-api-key: soak-dev-key`.

### 3. Start a long simulated run

Seed a fake `script_scenario` result carrying a long `load_test_spec` (mirrors
the integration test) so the run goes straight into the execution poll loop on
the sim engine — a ~10-minute window to restart things underneath it:

```bash
H=(-H 'x-api-key: soak-dev-key' -H 'content-type: application/json')
TID=$(curl -s "${H[@]}" -X POST http://localhost:8123/threads -d '{}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["thread_id"])')

curl -s "${H[@]}" -X POST http://localhost:8123/threads/$TID/runs -d '{
  "assistant_id": "pipeline",
  "input": {
    "title": "ha soak",
    "request": "rolling restart soak",
    "phase_results": {
      "script_scenario": {
        "phase": "script_scenario", "status": "succeeded", "attempt": 1,
        "load_test_spec": {"title": "ha soak load", "vusers": 5,
                            "ramp_s": 1.0, "duration_s": 600}
      }
    }
  },
  "config": {
    "configurable": {
      "phases": ["execution"],
      "gates": {"execution": {"prompt_review": "auto", "output_review": "auto"}},
      "limits": {"poll_interval_s": 1.0, "poll_timeout_s": 1200}
    },
    "recursion_limit": 1500
  }
}'
```

Confirm it is running (round-robins through nginx on :8123):

```bash
curl -s "${H[@]}" http://localhost:8123/v1/pipelines/$TID
curl -s "${H[@]}" http://localhost:8123/v1/engines/runs/$TID   # one attempt, status "running"
```

### 4. Rolling-restart the replicas mid-run

One at a time, waiting for health between (this is what a K8s rolling update
does; the Helm chart's PDB enforces the same one-at-a-time property):

```bash
COMPOSE="docker compose -f deploy/compose-ha/docker-compose.ha.yaml"
$COMPOSE restart apex-server-1 && sleep 15
$COMPOSE restart apex-server-2 && sleep 15
```

Harsher variant — SIGKILL, matching the integration test's crash semantics:

```bash
$COMPOSE kill apex-server-1 && $COMPOSE start apex-server-1 && sleep 15
$COMPOSE kill apex-server-2 && $COMPOSE start apex-server-2
```

Note: a kill mid-poll orphans that replica's in-flight run worker; the thread's
checkpointed state stays intact. If the run shows as interrupted/failed after a
hard kill, re-invoke the thread with empty input — it resumes from the last
committed checkpoint (this is exactly what subprocess B does in the test):

```bash
curl -s "${H[@]}" -X POST http://localhost:8123/threads/$TID/runs \
  -d '{"assistant_id": "pipeline"}'
```

### 5. Assert zero lost pipelines

```bash
curl -s "${H[@]}" 'http://localhost:8123/v1/pipelines?limit=50'   # thread still listed
curl -s "${H[@]}" http://localhost:8123/v1/pipelines/$TID         # eventually "succeeded"
curl -s "${H[@]}" http://localhost:8123/v1/engines/runs/$TID
```

Pass criteria:

- the thread never disappears from `/v1/pipelines` and ends `succeeded`;
- `/v1/engines/runs/$TID` shows **exactly one attempt with one
  `external_run_id`** across all restarts — the checkpointed idempotency key
  plus the engine's get-or-create provision contract and PostgreSQL advisory
  creation lock make concurrent replicas unable to double-start load;
- an SSE consumer (`GET /threads/$TID/runs/<run_id>/stream` through nginx) can
  rejoin after a disconnect and keep receiving events (Redis-backed resumable
  streams).

### Teardown

```bash
docker compose -f deploy/compose-ha/docker-compose.ha.yaml down -v
```

## Notes

- MinIO is included for artifact-store parity but the soak itself runs on the
  sim engine and does not need it. If you exercise artifact uploads, point the
  `minio-artifacts` connection's `base_url` at `http://minio:9000` (in-network
  name) via `PATCH /v1/admin/connections/{id}`.
- Ports: nginx `:8123` (only API entrypoint), Postgres `:5433` (host-side, for
  migrations/seeding only).
