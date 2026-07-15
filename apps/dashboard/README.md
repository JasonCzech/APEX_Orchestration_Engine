# APEX Orchestration Dashboard

Decoupled web client for the APEX Orchestration Engine. React 18 + TypeScript
(strict) + Vite, in the APEX Load design language. The dashboard talks to two
API surfaces on the same server — the LangGraph Assistants/Threads/Runs API and
the custom `/v1` domain API — and is **never served by the backend**: it ships
as a static bundle behind its own reverse proxy.

Architecture decisions: [ADR-0006](../../docs/adr/0006-dashboard-architecture.md).
Backend overview: [repo root README](../../README.md).

## Quickstart

The dashboard needs a running, seeded backend. From the **repo root**:

Windows PowerShell / Command Prompt / cross-platform npm scripts:

```powershell
npm run setup:backend
npm run dev:backend
npm install
npm run -w @apex/dashboard dev
```

Equivalent explicit backend tasks (commonly used on macOS/Linux):

```bash
uv sync
cp .env.example .env
make infra-up
make migrate
uv run python scripts/seed_dev.py
uv run python scripts/seed_prompts.py
uv run python scripts/seed_catalog.py
make dev
npm install
npm run -w @apex/dashboard dev
```

Open <http://localhost:3000> and paste an API key at the gate screen.

**API keys (dev):** either use a key printed by `seed_dev.py`
(`apex-admin` / `apex-operator` / `apex-viewer` — stored hashed, shown once), or
set the no-database shortcut in the root `.env`:

```bash
APEX_AUTH__DEV_API_KEY=dev-key-local    # resolves to a synthetic unscoped admin
```

For local UI-only work, the dashboard can skip the browser API-key gate while
still sending the backend dev key on requests:

```bash
# apps/dashboard/.env.development.local
VITE_APEX_DEV_AUTH=true
VITE_APEX_DEV_API_KEY=dev-key-local
```

The bypass is explicit and Vite-dev-only (`import.meta.env.DEV`); production
builds still require normal authentication. Use `.env.development.local` instead
of `.env.local` so the local Vitest suite does not accidentally start in
bypass mode.

**Dev proxy:** the Vite dev server proxies `/v1`, `/threads`, `/runs`,
`/assistants`, `/ok`, and `/ready` to the backend (default `http://127.0.0.1:2024`).
Browser navigations (`Accept: text/html`) bypass the proxy so the SPA route
`/runs` and the API path `/runs` coexist. Point at a different backend with:

```bash
APEX_API_PROXY=http://other-host:2024 npm run -w @apex/dashboard dev
```

```powershell
$env:APEX_API_PROXY = "http://other-host:2024"
npm run -w @apex/dashboard dev
```

(`APEX_API_PROXY` is read from the **shell** environment by `vite.config.ts`;
see [`.env.example`](./.env.example).)

### Scripts

| Command (repo root) | What it does |
|---|---|
| `npm run -w @apex/dashboard dev` | Vite dev server on :3000 with backend proxy |
| `npm run -w @apex/dashboard test` | vitest (jsdom) — full suite |
| `npm run -w @apex/dashboard typecheck` | `tsc -b` (strict) |
| `npm run -w @apex/dashboard lint` | eslint 9 flat config |
| `npm run -w @apex/dashboard build` | typecheck + production bundle to `dist/` |

## Runtime configuration (`/config.json`)

Origins are resolved at **runtime**, not build time — one bundle serves every
environment. Before mounting, the app fetches `/config.json`
(`src/config/runtimeConfig.ts`):

```json
{
  "apexOrigin": "",
  "langgraphOrigin": ""
}
```

- `apexOrigin` — origin serving the `/v1` domain API. Empty string = same
  origin (the common case: vite proxy in dev, reverse proxy in prod).
- `langgraphOrigin` — origin serving the LangGraph Assistants/Threads/Runs API.
  Empty string = same origin.

Missing, non-JSON, or schema-invalid responses fall back to same-origin
defaults — a deploy without `config.json` behind a same-origin proxy just
works. Template: [`public/config.example.json`](./public/config.example.json)
(copy to `config.json` next to `index.html` in the deployed bundle).

## Architecture map

```
src/
├── api/                  # the ONLY two API clients + cache plumbing
│   ├── apexClient.ts     #   /v1 via openapi-fetch, typed by @apex/api-client (generated)
│   ├── langgraphClient.ts#   LangGraph surface via @langchain/langgraph-sdk
│   ├── queryKeys.ts      #   central query-key factory — APPEND-ONLY
│   ├── queryClient.ts / errors.ts   # one ApiError shape across both clients
│   └── hooks/            #   one use*.ts per /v1 resource
├── streaming/            # usePipelineStream: one custom-only SSE connection per run,
│   │                     #   with /v1 snapshots as the durable state authority
│   ├── applyStreamEvent.ts  # patches the react-query cache (snapshot stays canonical)
│   ├── ringBuffer.ts / tokenBuffer.ts  # engine_poll + token coalescing (never the cache)
│   └── resumeStore.ts    #   last_event_id persistence for joinStream resume
├── hitl/                 # gate machine + shared GateModule
│   ├── gateMachine.ts    #   pure discriminated-union reducer (pessimistic resume)
│   └── GateModule.tsx    #   renders prompt_review AND phase_review — run page + inbox
├── features/<screen>/    # one folder per screen; pages.tsx is the lazy route entry
│   └── pages.tsx         #   router.tsx never changes — features swap their pages module
├── routes/router.tsx     # the single route table (lazy per-feature chunks)
├── components/           # shell (sidebar/topbar + topbar-contribution), controls, viewers
├── theme/                # design tokens + 5 themes (see Theming)
├── auth/                 # ApiKeyGate + AuthProvider (role from /v1/system/info)
├── config/               # runtime /config.json loader
└── test/                 # renderApp harness + MSW server (see Testing)
```

**Two clients, one contract.** All server IO goes through exactly two typed
clients: `openapi-fetch` over the generated
[`@apex/api-client`](../../packages/api-client) types for `/v1`, and the
official `@langchain/langgraph-sdk` for threads/runs/streams. No hand-written
fetch wrappers. Streamed custom events, interrupt payloads, and thread-state
slices are validated with zod schemas from
[`@apex/pipeline-events`](../../packages/pipeline-events), which is
contract-tested against fixtures lifted from the backend's own test suite —
schema drift fails the build, not the operator.

**Streaming model — snapshot + tail.** The cached `threads.state` snapshot is
canonical; the SSE stream only patches it. `usePipelineStream`
(`src/streaming/usePipelineStream.ts`) reconnects with jittered backoff,
resumes via `joinStream(lastEventId)`, and heals with one snapshot refetch on
stream end/error or resume-window expiry. High-frequency data (`engine_poll`
samples, reasoning tokens) never enters the query cache — it lives in a 300-pt
ring buffer / rAF-coalesced token buffer with a 50 ms flush floor.

**HITL gates.** `gateMachine.ts` is a pure reducer
(`no_gate → open → submitting → awaiting_agent | superseded | failed`), keyed
by `interrupt_id`. Resume is **pessimistic** through the backend's
compare-and-set route — no optimistic cache writes; a 409 CAS loss renders as
`superseded` ("actioned by another operator"), a network failure preserves the
operator's draft for retry.

## Testing

```bash
npm run -w @apex/dashboard test          # full suite (vitest, jsdom)
npm run -w @apex/dashboard test -- src/hitl   # one folder/file
```

Conventions:

- **`renderApp`** (`src/test/render.tsx`) mounts the full provider stack
  (query client, auth, connectivity, theme, topbar contributions, ApiKeyGate)
  on a memory router over the **real** `appRoutes` — tests navigate the actual
  route table. `authenticatedState(role)` starts a test past the key gate;
  `createTestQueryClient()` disables retries so error states surface
  immediately.
- **MSW** (`src/test/server.ts`) provides global happy-path handlers
  (`/v1/system/info`, empty `/v1/pipelines`, drafts, usage). Tests layer
  scenario-specific handlers with `server.use(...)` — which takes precedence —
  rather than editing the global list. Handler payloads are typed against
  `@apex/api-client`.
- **Pure logic is tested as pure logic**: the gate machine and stream reducer
  have exhaustive table-driven tests; streaming behavior is driven through a
  fake LangGraph client with scriptable async-generator streams
  (`src/streaming/__tests__`).
- Co-location: `Foo.test.tsx` next to `Foo.tsx`, or under the feature's
  `__tests__/`.

## Theming

Five themes, switched via `data-theme` on `<html>` (`src/theme/useTheme.ts`,
persisted in localStorage):

| `data-theme` | Name |
|---|---|
| `dark` *(default)* | Premium Dark (OLED / Deep Space, electric-violet accent, glassmorphism) |
| `light` | Sleek Light (Pearl / Glass) |
| `solarized-dark` | Premium Solarized Dark |
| `solarized-light` | Premium Solarized Light |
| `monokai-dimmed` | Premium Monokai Dimmed |

**Tokens-only rule:** component CSS may use `var(--token)` values exclusively —
no literal colors, shadows, or radii. `src/theme/tokens.css` (the dark default)
is ported **verbatim** from the APEX Load reference implementation
(`Project_Stormrunner/dashboard/src/index.css`), with identical token names so
APEX Load component patterns drop in unchanged; `themes.css` overrides the
token set per theme, and `primitives.css` carries the shared `.btn` /
`.glass-panel` / badge / chip / metric-pill / table primitives. A new theme is
a new token block — zero component changes.

## Deployment

`npm run -w @apex/dashboard build` emits a fully static bundle to `dist/`
(vendor-split chunks: `vendor-langgraph`, `vendor-codemirror`,
`vendor-recharts`, `vendor`). The backend **does not serve the dashboard** —
host the bundle on any static server (the plan's target shape is a small
caddy/nginx container) with:

1. **SPA fallback** — unknown paths serve `index.html`.
2. **Same-origin reverse proxy** of the API paths (`/v1`, `/threads`, `/runs`,
   `/assistants`, `/ok`, `/ready`) to the APEX server, mirroring the dev proxy. Same
   origin keeps the artifact-viewer iframes and SSE free of CORS. The HTML-vs-
   API split on `/runs` must key on the `Accept` header, as the dev proxy does.
3. **SSE-safe proxying** — response buffering OFF and long read timeouts on
   every hop, or live streams stall and gates appear late. Reference settings:
   [`deploy/compose-ha/nginx.conf`](../../deploy/compose-ha/nginx.conf)
   (`proxy_buffering off; proxy_read_timeout 24h;`).
4. Optional `config.json` beside `index.html` when the APIs live on another
   origin (see Runtime configuration).
