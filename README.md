# APEX Orchestration Engine

LangGraph-driven agentic platform for end-to-end performance testing. A self-hosted
LangGraph Server hosts the 7-phase pipeline (story_analysis → test_planning →
env_triage → script_scenario → execution → reporting → postmortem) with
human-in-the-loop gates; a custom `/v1` FastAPI surface (mounted via `langgraph.json`)
provides the domain API (prompts, catalogs, connections, work tracking, analytics).
Execution engines (LoadRunner, APEX Load, simulated) sit behind an engine-agnostic
port. Dashboards are fully decoupled clients.

## Prerequisites

- Python 3.12 and `uv`
- Node.js 20.19+ and npm
- Docker Desktop or Docker Engine with Compose v2

## Quickstart (backend)

Cross-platform setup for Windows PowerShell, Command Prompt, macOS, and Linux:

```powershell
npm run setup:backend
npm run dev:backend
```

`setup:backend` runs `uv sync`, creates `.env` from `.env.example` when needed,
starts the Docker dev infra, applies migrations, and seeds local data.
`dev:backend` starts the LangGraph dev server on `:2024`.

The Makefile still mirrors the backend tasks for macOS/Linux users:

```bash
uv sync
cp .env.example .env
make infra-up
make migrate
uv run python scripts/seed_dev.py
uv run python scripts/seed_prompts.py
uv run python scripts/seed_catalog.py
make dev
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

Backend-only gates: `npm run check:backend` or `make check` (ruff, pyright,
pytest). The root `npm run check` additionally typechecks/lints/tests the shared
API/event contracts and runs the complete dashboard gate.

## Dashboard

The web dashboard lives in `apps/dashboard` (npm workspaces — install at the
repo root). It is a fully decoupled static SPA: the backend never serves it.

```powershell
npm install
npm run dev:dashboard
npm run test:dashboard
npm run build
```

`dev:dashboard` starts Vite on `:3000` with the backend proxy. `npm run build`
typechecks and writes the static bundle to `apps/dashboard/dist`.

If you need to point the dashboard at a different backend in PowerShell:

```powershell
$env:APEX_API_PROXY = "http://other-host:2024"
npm run dev:dashboard
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
