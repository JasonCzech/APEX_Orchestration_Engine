# APEX Orchestration Engine

LangGraph-driven agentic platform for end-to-end performance testing. A self-hosted
LangGraph Server hosts the 7-phase pipeline (story_analysis → test_planning →
env_triage → script_scenario → execution → reporting → postmortem) with
human-in-the-loop gates; a custom `/v1` FastAPI surface (mounted via `langgraph.json`)
provides the domain API (prompts, catalogs, connections, work tracking, analytics).
Execution engines (LoadRunner, APEX Load, simulated) sit behind an engine-agnostic
port. Dashboards are fully decoupled clients.

## Quickstart (backend)

```bash
uv sync                      # install deps
cp .env.example .env
make infra-up                # dev Postgres/Redis/MinIO (the /v1 domain API uses Postgres)
make migrate                 # apex-schema migrations
uv run python scripts/seed_dev.py      # API consumers — keys printed exactly once
uv run python scripts/seed_prompts.py  # built-in phase prompts
uv run python scripts/seed_catalog.py  # demo app/env + stub connections
make dev                     # LangGraph dev server on :2024
```

Smoke checks:

```bash
curl -s http://127.0.0.1:2024/ok                  # LangGraph server health
curl -s http://127.0.0.1:2024/v1/system/info      # APEX domain API (custom routes)
```

Live end-to-end smokes against the running dev server: `scripts/m1_smoke.py`
(gated pipeline, interrupts, single-phase re-run, resume-conflict spike — expects
`APEX_AUTH__DEV_API_KEY=dev-key-m1` in `.env`) and `scripts/m2_smoke.py` (domain
API surface; usage in its docstring, takes seeded admin/viewer keys).

Quality gates: `make check` (ruff, pyright, pytest).

## Dashboard

The web dashboard lives in `apps/dashboard` (npm workspaces — install at the
repo root). It is a fully decoupled static SPA: the backend never serves it.

```bash
npm install
npm run -w @apex/dashboard dev      # vite on :3000, proxies to langgraph dev on :2024
npm run -w @apex/dashboard test     # vitest suite
npm run -w @apex/dashboard build    # typecheck + static bundle to apps/dashboard/dist
```

Setup details (seeded backend, API keys, runtime config, deploy contract):
[`apps/dashboard/README.md`](apps/dashboard/README.md). Architecture decisions:
[`docs/adr/0006-dashboard-architecture.md`](docs/adr/0006-dashboard-architecture.md).

## Layout

- `langgraph.json` — binds graphs + custom HTTP app + auth to the server
- `src/apex/graphs/` — pipeline master graph (7 phase subgraphs, HITL gates)
- `src/apex/app/`, `src/apex/routers/` — `/v1` domain API composition root + routers
- `src/apex/ports|adapters|services/` — integration seams (stubs, Jira/ADO, ELK, k8s, engines)
- `src/apex/persistence/` — SQLAlchemy models + Alembic migrations (`apex` schema)
- `apps/dashboard/` — `@apex/dashboard` web client (React + Vite)
- `packages/api-client/` — `@apex/api-client`, TS types generated from the committed `/v1` spec
- `packages/pipeline-events/` — `@apex/pipeline-events`, zod contracts for SSE events + interrupts
- `scripts/` — OpenAPI export, SDK generation, seeds, m1/m2 live smokes
- `docs/adr/` — architecture decision records; `docs/api/` — committed OpenAPI spec

The full rebuild plan (architecture, milestones M0–M6 + dashboard D0–D8) lives with
the project owner; ADRs 0001–0006 capture the load-bearing decisions.

## Deployment

Standalone server image (`langgraph build`), Helm chart, and HA soak rig live in
`deploy/` (`helm/apex-orchestration-engine/`, `compose-ha/`); releases are built by
`.github/workflows/release.yaml` on `v*` tags. Procedures: `docs/runbooks/`
(`deployment.md`, `operations.md`, `incident.md`). Topology decision (one image,
external Postgres/Redis/S3, N stateless replicas, license gate):
`docs/adr/0005-deployment-topology.md`.
