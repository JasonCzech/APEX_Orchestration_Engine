# ADR-0001: Self-hosted LangGraph Server as the orchestration runtime

**Status:** accepted (2026-06-11)

## Decision
The platform runs as a self-hosted LangGraph Server (`langgraph-api`). The built-in
Assistants/Threads/Runs API is the first-class pipeline execution surface; domain
endpoints are a custom FastAPI app mounted via `langgraph.json` `http.app` under `/v1`.

## Rationale
The legacy platform hand-rolled orchestration, approvals, WebSocket broadcast, and
file-based run state inside one FastAPI process — no horizontal scaling, lost state on
restart. LangGraph Server provides durable Postgres-checkpointed runs, native
human-in-the-loop interrupts, resumable SSE streaming (Redis pub-sub), and assistant
versioning. Rebuilding those would repeat the legacy mistake.

## Consequences
- One deployable container; stateless replicas; Postgres + Redis required in production.
- `langgraph dev` (in-memory) covers all local development for free.
- **Licensing gate:** `langgraph-api` is ELv2. The production standalone server needs
  `LANGGRAPH_CLOUD_LICENSE_KEY`, and custom auth requires the Self-Hosted Enterprise
  tier. Decision due by end of M1; fallback is a thin identity-injecting gateway
  (loses native per-resource authorization filters).
- We never shadow built-in routes; `/v1` is fully additive (upgrade safety).
