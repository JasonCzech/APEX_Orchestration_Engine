# Runs grid — route wiring (D1, grid agent)

## What ships in this folder

| Export | File | Route |
|---|---|---|
| `RunsListPage` | `src/features/runs/RunsListPage.tsx` | `/runs` |

`src/routes/router.tsx` currently lazy-loads `RunsListPage` from
`@/features/runs/pages` (the D0 placeholder module). To wire the real screen,
re-export the implementation from `pages.tsx` — no router change needed:

```ts
// src/features/runs/pages.tsx — replace the placeholder RunsListPage with:
export { RunsListPage } from './RunsListPage'
```

(Alternatively point the router's `lazy` at
`import('@/features/runs/RunsListPage')` with name `RunsListPage`; the
re-export is the minimal-diff option and keeps one lazy chunk per feature.)

## Routes this screen links to (must exist or remain placeholders)

- `/runs/new` — empty-state CTA
- `/runs/:threadId` — row click, run-title link, pending-gate chip
- `/runs/:threadId/phases/:phase` — phase-strip segment click

All three already exist in `router.tsx` (placeholders until the detail agent
lands), so wiring is just the one re-export above.

## URL contract for `/runs` (deep links)

`?status=idle|busy|interrupted|error & q=<text> & project=<id> & limit=<1..100> & offset=<n>`
— parsed/serialized by `src/features/runs/runsFilters.ts`; defaults
(`limit=25`, `offset=0`) are omitted from the URL. Unknown statuses and
malformed numbers are dropped/clamped on parse, so stale links never break the
screen.

## Shared pieces other screens may reuse

- `PhaseStrip` (`src/components/runs/PhaseStrip.tsx`) — props
  `{ strip: {phase, status, attempt?}[], onSelect?: (phase: PhaseName) => void, size?: 'sm'|'md' }`.
  Planned reuse: Home active-run cards, `/runs/compare`.
- `usePipelines(filters)` (`src/api/hooks/usePipelines.ts`) — pipelines list on
  `queryKeys.pipelines.list`, keepPreviousData, 15s visibility-aware poll.
- `formatRelative(iso, now?)` (`src/utils/time.ts`).

## Known drift (integrator follow-up)

The generated `@apex/api-client` schema predates `PipelineSummary.engine`
(present in both `docs/api/apex-v1.openapi.json` and the live backend).
`usePipelines.ts` extends the type locally (`PipelineEngineInfo`); once the
client is regenerated, that local extension can be deleted.

---

# Run detail / timeline / artifact viewer — route wiring (D1, detail agent)

This feature does NOT touch `src/routes/router.tsx`. The integrator wires the
routes below; until then the D0 placeholders render.

## Exports

| Component | Module | Route |
|---|---|---|
| `RunDetailPage` | `@/features/runs/RunDetailPage` | BOTH `/runs/:threadId` (redirects to the current phase) AND `/runs/:threadId/phases/:phase?tab=` (reads the optional `:phase` param) |
| `TimelinePage` | `@/features/runs/TimelinePage` | `/runs/:threadId/timeline` |
| `ArtifactViewerPage` | `@/features/artifacts/ArtifactViewerPage` | `/runs/:threadId/artifacts/:name` — `:name` is the **artifact id** |

## Exact wiring (Option A — preferred, no router.tsx change)

Re-export from `src/features/runs/pages.tsx`, replacing four placeholders:

```tsx
export { RunDetailPage } from './RunDetailPage'
export { RunDetailPage as PhaseDetailPage } from './RunDetailPage'
export { TimelinePage as RunTimelinePage } from './TimelinePage'
export { ArtifactViewerPage } from '@/features/artifacts/ArtifactViewerPage'
```

The existing route table already lazy-loads those four export names from
`@/features/runs/pages` at the correct paths. (Option B: point each route's
`lazy` loader directly at the implementation modules.)

## Notes for the integrator (detail agent)

- `RunDetailPage` issues a client redirect from `/runs/:threadId` to
  `/runs/:threadId/phases/<current_phase>` (falls back to the first phase with
  a result, then the plan head). Keep both paths on the same component.
- Workspace tab state lives in `?tab=output|artifacts|prompt|dialogue`
  (default `output`); the phase-strip buttons preserve the current search
  string, so phase deep links (`/runs/:threadId/phases/:phase`) compose with
  tabs.
- Data: all three screens read `useThreadState(threadId)`
  (`GET /v1/pipelines/{thread_id}` facade — summary + values + interrupts in
  one call; `values` parsed through the lenient `@apex/pipeline-events`
  PipelineState mirror, raw fallback on drift). The raw SDK
  `threads.get_state` path is the D2 alternative once streams patch the cache.
  Query keys: `queryKeys.threads.state` / `queryKeys.threads.artifact`
  (threads.* namespace is the detail agent's).
- Artifact bytes go through `src/features/artifacts/artifactUrl.ts`
  (`memory://<key>` and `s3://<bucket>/<key>` -> `/v1/artifacts/<key>`,
  verified against `src/apex/routers/artifacts.py` + both store adapters) with
  a plain authenticated fetch — the typed client would percent-encode the
  `{key:path}` slashes.
- Same generated-client drift as the grid note: `PipelineDetail.engine` is
  missing from `@apex/api-client`; `useThreadState.ts` extends the type
  locally — delete on regeneration.
- Tests mount a memory router in
  `src/features/runs/__tests__/testUtils.tsx` mirroring this wiring exactly.

---

# Live run experience + minimal launch — route wiring (D2, live-UI agent)

No `src/routes/router.tsx` changes needed: every D2 surface mounts inside
pages that are already wired (`RunDetailPage`, `RunsListPage` via `pages.tsx`).

## What ships

| Piece | File | Mounted where |
|---|---|---|
| `LiveStatusChip` | `src/features/runs/LiveStatusChip.tsx` | RunDetailPage header (idle / connecting / live / reconnecting / ended / error; title explains) |
| `ActivityFeed` | `src/features/runs/ActivityFeed.tsx` | PhaseWorkspace "Activity" tab (NEW first tab) |
| `EngineStrip` | `src/features/runs/EngineStrip.tsx` | PhaseWorkspace, execution phase only (samples present or phase running) |
| `LaunchRunButton` | `src/features/runs/LaunchRunButton.tsx` | RunsListPage toolbar (right edge) |
| `launchRun` / `ALL_AUTO_GATES` | `src/features/runs/launchRun.ts` | SDK launch path (thread create + runs.create on `pipeline`) |
| `useLaunchRun` | `src/api/hooks/useLaunchRun.ts` | mutation; invalidates `queryKeys.pipelines.all` |
| contract mirror types | `src/features/runs/liveTypes.ts` | loose structural mirror of `PipelineStreamView` for component props |

## URL contract changes

- `?tab=` on `/runs/:threadId/phases/:phase` now accepts
  `activity|output|artifacts|prompt|dialogue`. Default is **activity when the
  thread is busy**, output otherwise. Old `?tab=output` deep links unchanged.
- `/runs/:threadId` (no phase) now PRESERVES the query string through its
  redirect, so the post-launch deep link `/runs/{threadId}?tab=activity` lands
  on the current phase with the Activity tab open.

## /runs/new (D4 placeholder note)

`/runs/new` stays the D0 `FeaturePlaceholder` — the full 6-step wizard is D4.
Until then the minimal launch lives on the `/runs` toolbar (`LaunchRunButton`).
If an interim launch affordance is wanted on `/runs/new`, mount
`<LaunchRunButton />` inside that placeholder; it is provider-free (needs only
react-query + router context). D2 launches force `configurable.gates` ALL-AUTO
for all 7 phases (gate review UX is D3; backend defaults are GATED).

## Integration contract consumed (streaming agent's `src/streaming/`)

`useRunLiveness(threadId)` -> `{ runId, stream: PipelineStreamView }` is
imported ONLY in `RunDetailPage.tsx`. All other live components take loose
structural props (`liveTypes.ts`), so they never import streaming internals.
Tests mock `@/streaming/usePipelineStream` at that boundary
(`__tests__/liveFixtures.ts` provides scripted views).

Perf rule honored: `engine_poll` data reaches the UI only through the stream
view's flushed ring buffer (≤20fps); the feed renders 1 expandable row per 10
engine ticks, caps at 500 entries with a truncation notice, and nothing
high-frequency enters the react-query cache. Reasoning tokens are deliberately
omitted from the feed (M-era backend stubs don't stream messages-tuple
meaningfully; `transcript_ref` artifacts are the durable record).

## Build note

`recharts@^2.15.4` added (workspace-installed from the repo root). Vite
`manualChunks` routes recharts + its d3/victory-vendor tree to
`vendor-recharts` (verified: 313 kB chunk, loaded only with run-detail pages).
In jsdom tests, mock `ResponsiveContainer` (no ResizeObserver/layout) — see
`__tests__/EngineStrip.test.tsx`.

---

# HITL gate machine + GateModule (D3, gate-machine agent)

No `src/routes/router.tsx` changes: every D3 surface mounts inside already
wired pages (`RunDetailPage`) or the approvals inbox (its own agent's wiring).

## What ships (src/hitl/)

| Piece | File | Mounted where |
|---|---|---|
| `gateReducer` + types (`GateMachineState`, `GateInstance`, `GateDraft`, `buildResumeBody`) | `src/hitl/gateMachine.ts` | pure machine, no React/IO |
| `useGate(threadId, {gateHint?})` | `src/hitl/useGate.ts` | binds machine to `useThreadState` interrupts + stream hint accelerator; returns `{state, gate, lastAccepted, edit, submit, reset, viewCurrent}` |
| `useResumeGate` | `src/hitl/useResumeGate.ts` | CAS POST `/v1/pipelines/{thread_id}/gates/{interrupt_id}/resume`; 202 invalidates `threads.state(threadId)` + `pipelines.lists()`; 409 problem `gate_superseded` -> conflict |
| `GateModuleView` (controlled) | `src/hitl/GateModule.tsx` | RunDetailPage pins it ABOVE the PhaseWorkspace tabs (via the new `gateSlot` prop) when the gate's phase == selected phase |
| `GateModule` (self-contained, inbox contract `{threadId, interrupt, compact, onOutcome, handleRef}`) | `src/hitl/GateModule.tsx` | ApprovalsInboxPage preview (`features/approvals/gateModuleContract.ts` re-exports the types) |
| `GateSlimBanner` | `src/hitl/GateModule.tsx` | RunDetailPage on phases that are NOT the gate's phase (links to it) |
| `AbortConfirm` (type-to-confirm 'ABORT') | `src/hitl/GateActionBar.tsx` | gate action bar + RunDetailPage header (same machine, action `abort`) |
| panels (`PromptReviewPanel`, `PhaseReviewPanel`, `DialogueThread`, `SupersededBanner`) | `src/hitl/*.tsx` | inside GateModuleView |

## Run-detail integration (surgical edits)

- `PhaseWorkspace.tsx`: new optional `gateSlot?: ReactNode` rendered above the
  tab bar — nothing else changed.
- `RunDetailPage.tsx`: one page-level `useGate(threadId, { gateHint:
  live.stream.pendingGateHint })`; workspace gate slot + header abort share it.
- `RunRail.tsx`: the D2 disabled "Review gate" placeholder is now a real link
  to `/runs/:threadId/phases/<gatePhase>`; the stream-hint chip copy changed
  from "review arrives in D3" to "loading gate…" (tests updated accordingly).

## Semantics (plan "HITL gate machine")

Pessimistic resumes: no cache writes before the 202. Gate identity =
interrupt_id (new id -> NEW open instance with a fresh draft; same id ->
no-op). 202 on approve/modify/skip_phase/abort -> `no_gate` (snapshot/stream
narrative takes over; a settled-id guard suppresses the stale cache echo
until the refetch lands); 202 on discuss/revise -> `awaiting_agent` until the
re-interrupt mints a new id. 409 `gate_superseded` -> `superseded(conflict)`;
other failures -> `failed` with the draft preserved for [Retry].

---

# Phase-subset re-runs — pre-flight modal + entry points (D4, phase-independence agent)

No `src/routes/router.tsx` changes: every surface mounts inside already wired
pages (`RunDetailPage`, `RunsListPage`).

## What ships (src/features/runs/)

| Piece | File | Mounted where |
|---|---|---|
| `assessPlan`, `runFromHereSelection`, `lastPlanSelection`, `PHASE_ORDER`, `PHASE_PREREQUISITES`, `STALE_AFTER_MS` | `preflight.ts` | pure logic — mirrors `src/apex/domain/pipeline.py` + `graph.py` plan_resolver semantics |
| `PreflightModal {threadId, initialSelection?, onClose}` | `PreflightModal.tsx` | the single pre-flight checkpoint every entry point funnels through |
| `OverflowMenu {label, items, trigger?, className?}` | `PreflightModal.tsx` | shared glass dropdown (menu role, Escape/outside-click close, arrow keys, focus-first-item); stops click propagation so it is row-click safe |
| `useRerun` / `buildRerunConfigurable` / `ALL_GATED_GATES` | `useRerun.ts` | `runs.create(threadId, 'pipeline', {input: {}, config:{configurable:{phases, gates?}}})` on the EXISTING thread |
| styles | `preflight.css` | modal, overflow menu, actions cell, split button, `.sr-only` |

## Entry points (surgical edits)

1. `RunsListPage.tsx` — new trailing actions column (`⋯`): [Re-run…] opens the
   modal for that thread WITHOUT initialSelection (the modal hydrates the
   default from the fetched plan; loading state lives inside the modal) |
   [Open] navigates. Row-click navigation untouched (the menu stops
   propagation internally).
2. `RunDetailPage.tsx` — header split button: [Re-run] (all phases) +
   [▾] → [All phases] / [Run phases…].

## Semantics (plan Part 2 §4 — verified against the backend)

- Readiness per selected phase: **OK** (prereq earlier in plan — canonical
  order means membership implies ordering) | **REUSE** (prereq succeeded on
  thread; shows attempt + age) | **STALE** (succeeded > 3 days ago — amber
  "environment may have drifted"; NOT a blocker) | **BLOCKED** (neither —
  danger "include <prereq> or it will fail at plan resolution").
- **Warn-don't-block**: blockers render danger rows + the caption "server
  will reject at plan resolution", but Start stays enabled — the backend
  plan_resolver is the authority. Start is disabled only for an EMPTY
  selection (backend treats empty `phases` as falsy → would run ALL phases).
- Gates segmented control: Inherit defaults (gates OMITTED from configurable —
  backend default is GATED) | All gated | All auto.
- `input` is `{}` and **NOT null** — null means continue-from-checkpoint to
  the LangGraph server; `{}` triggers a fresh plan_resolver pass (M2 smoke).
- On 2xx: invalidates `threads.state(threadId)` + `pipelines.lists()`, then
  navigates to `/runs/{threadId}?tab=activity`.
