# ADR-0006: Dashboard architecture — APEX Load stack, two typed clients, snapshot+tail streaming

**Status:** accepted (2026-06-12)

## Decision
The dashboard (`apps/dashboard`) is a fully decoupled SPA in the same monorepo
(npm workspaces), never served by the backend. Stack: **mirror APEX Load
(Project_Stormrunner reference), diverge only with cause** — React 18 +
TypeScript strict + Vite, CodeMirror 6, Recharts, and APEX Load's CSS
custom-property token system ported verbatim (5 themes, tokens-only component
CSS). Each deliberate divergence fixes a named legacy-frontend weakness:

| Divergence from APEX Load | Legacy weakness it fixes |
|---|---|
| `react-router` v7 browser-history routes (APEX Load uses hash routing) | No deep links — every screen/filter/tab now lives in the URL |
| `@tanstack/react-query` v5 server-state cache | No query/cache framework — ad-hoc fetch + local state per view |
| Generated `@apex/api-client` (openapi-fetch) + official `@langchain/langgraph-sdk` as the **only two clients** | Dual-source divergence from hand-written fetch wrappers |
| Feature folders with a `pages.tsx` lazy route entry per screen; the route table never changes when a screen is rebuilt | Giant container files concentrating whole screens |
| zod validation of streamed payloads via `@apex/pipeline-events` | Untyped event handling drifting silently from the server |

Not adopted from APEX Load: three.js, the Wails desktop wrapper. Not added:
Tailwind, component libraries, Redux/Zustand/xstate (client state is React
context; server state is the query cache; the one real state machine is a pure
hand-rolled reducer).

Core rules:

- **Two-clients contract rule.** All server IO flows through exactly two typed
  clients sharing one `ApiError` shape and one `x-api-key`: the `/v1` domain
  API via types generated from the committed OpenAPI spec, and the LangGraph
  Assistants/Threads/Runs API via the official SDK — never wrapped, never
  re-documented. Every cache read keys through one append-only query-key
  factory (`src/api/queryKeys.ts`).
- **Snapshot + tail streaming.** The cached `threads.state` snapshot is
  canonical; one resumable SSE connection per (thread, run) only patches it.
  Reconnect uses `joinStream(last_event_id)` with a healing snapshot refetch on
  failure or resume-window expiry — correctness comes from the snapshot,
  streaming only adds liveness. High-frequency data (`engine_poll`, reasoning
  tokens) stays in ring/token buffers with a coalesced flush gate and never
  enters the query cache.
- **Pessimistic HITL.** Gate state is a pure discriminated-union reducer keyed
  by `interrupt_id`. Resume goes through the backend's compare-and-set route
  with no optimistic writes: a CAS 409 renders as `superseded` (normal
  multi-operator outcome, not an error); a network failure preserves the
  operator's draft for retry.
- **Poll-based fleet liveness.** Cross-run surfaces (home, runs grid, approvals
  inbox) poll the `/v1/pipelines` façade and `threads.search` at 15 s,
  visibility-aware; there is deliberately no global event feed in v1. Live
  streams exist only on run detail / watched runs; `gate_opened` from any open
  stream merely accelerates an inbox refresh.
- **Monorepo lockstep with a spec drift gate.** The OpenAPI spec
  (`docs/api/apex-v1.openapi.json`), the generated `@apex/api-client` types,
  and `@apex/pipeline-events` zod schemas (contract-tested against fixtures
  lifted from the backend test suite) are committed and move in the same
  commits as the server. CI re-exports the spec and fails on diff, and runs the
  dashboard workspace's typecheck/lint/test/build alongside the backend.

## Rationale
APEX Load's visual language and component patterns are a proven house standard;
mirroring them makes the dashboard immediately familiar and lets token sheets
and CSS port unchanged. The legacy frontend's four documented weaknesses are
all *architectural absences*, so each is fixed by construction rather than
convention — deep links by routing choice, caching by framework, screen
decomposition by folder contract, and client divergence by generating one
client and importing the other. Snapshot+tail and pessimistic gates follow from
the backend's design (durable checkpoints as truth — ADR-0004; the `/v1`
compare-and-set gate-resume route for multi-operator safety): the UI never
holds state the server could contradict. Poll-based fleet liveness matches the backend's deliberate decision
to scope event streams per-run.

## Consequences
- A rebuilt screen is a `pages.tsx` swap; `router.tsx` and query keys are
  append-only, which keeps parallel feature work conflict-free.
- API changes are visible at compile time in the dashboard (generated types) and
  at test time for streamed payloads (fixture contract tests) — but the fixture
  set must be maintained as the backend evolves.
- The static bundle imposes a deploy contract on every host: SPA fallback,
  same-origin reverse proxy of `/v1` + LangGraph paths, and SSE-safe proxying
  (buffering off, long read timeouts) — see ADR-0005 and
  `apps/dashboard/README.md`.
- Polling caps fleet-view freshness at ~15 s by design; an optional `/v1/events`
  feed is the documented escalation path if that proves insufficient.
- Schema-drifted gate payloads render as a degraded gate (raw payload, no
  actions) instead of crashing — operators can always fall back to the API.
