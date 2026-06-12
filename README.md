# APEX Orchestration Engine

LangGraph-driven agentic platform for end-to-end performance testing. A self-hosted
LangGraph Server hosts the 7-phase pipeline (story_analysis → test_planning →
env_triage → script_scenario → execution → reporting → postmortem) with
human-in-the-loop gates; a custom `/v1` FastAPI surface (mounted via `langgraph.json`)
provides the domain API (prompts, catalogs, connections, work tracking, analytics).
Execution engines (LoadRunner, APEX Load, simulated) sit behind an engine-agnostic
port. Dashboards are fully decoupled clients.

## Quickstart (M0)

```bash
uv sync                      # install deps
cp .env.example .env
make dev                     # LangGraph dev server (in-memory) on :2024
```

Smoke checks:

```bash
curl -s http://127.0.0.1:2024/ok                  # LangGraph server health
curl -s http://127.0.0.1:2024/v1/system/info      # APEX domain API (custom routes)
```

Quality gates: `make check` (ruff, pyright, pytest). Dev infra (Postgres/Redis/MinIO)
when needed: `make infra-up`, migrations: `make migrate`.

## Layout

- `langgraph.json` — binds graphs + custom HTTP app (+ auth from M1) to the server
- `src/apex/graphs/` — pipeline graph (M0: toy 2-node placeholder)
- `src/apex/app/`, `src/apex/routers/` — `/v1` domain API composition root + routers
- `src/apex/ports|adapters|services/` — integration seams (from M1)
- `src/apex/persistence/` — SQLAlchemy models + Alembic migrations (`apex` schema)
- `docs/adr/` — architecture decision records

The full rebuild plan (architecture, milestones M0–M6 + dashboard D0–D8) lives with
the project owner; ADRs 0001–0005 capture the load-bearing decisions.

## Deployment

Standalone server image (`langgraph build`), Helm chart, and HA soak rig live in
`deploy/` (`helm/apex-orchestration-engine/`, `compose-ha/`); releases are built by
`.github/workflows/release.yaml` on `v*` tags. Procedures: `docs/runbooks/`
(`deployment.md`, `operations.md`, `incident.md`). Topology decision (one image,
external Postgres/Redis/S3, N stateless replicas, license gate):
`docs/adr/0005-deployment-topology.md`.
