# Legacy-Parity Sign-off (D8 exit criterion)

Audit of the shipped dashboard against the plan's legacy-capability inventory
(plan Part 2, "the legacy-capability coverage table is the D8 sign-off
checklist"). For every capability the feature source was read and the
load-bearing wire-up verified (hook → endpoint, action → payload). Paths are
relative to `apps/dashboard/src` unless noted; backend paths relative to
`src/apex`.

Verified at: 63 test files / 371 tests green, `tsc -b` clean, eslint 0 errors,
production build clean.

## Parity table

| # | Capability | Screen / module | Backend surface | Status |
|---|---|---|---|---|
| 1 | Home overview | `features/home/HomePage.tsx` (attention rail, active-run cards w/ `PhaseStrip`, recent runs, snapshot panel) | `GET /v1/pipelines`, `GET /v1/analytics/usage`, `GET /v1/drafts`, `threads.search(status=interrupted)` | complete |
| 2a | New test — draft prep | `features/new-test/useDraft.ts`, `NewRunWizard.tsx` (`?step=&draft=`, autosave, resume) | `/v1/drafts` CRUD | complete |
| 2b | New test — launch shaping | `features/new-test/steps/{ScopeStep,ConfigStep}.tsx` (catalog cascade, engine cards, golden-config picker, 7-phase toggles, gate matrix), `useWizardLaunch.ts` | `GET /v1/catalog/*`; SDK `threads.create` + `runs.create(pipeline, configurable)` w/ `durability:'sync'`, `streamResumable`, `multitaskStrategy:'reject'` | complete |
| 2c | New test — doc upload | `features/new-test/steps/ContextStep.tsx`, `api/hooks/useDocuments.ts` | `POST /v1/documents` (multipart), `GET /v1/documents` | complete |
| 2d | New test — prompt review | `features/new-test/steps/PromptsStep.tsx` (active catalog content + provenance chip; inline edit → `prompt_overrides`) | `GET /v1/prompts?namespace=phase` → run `configurable.prompt_overrides` | complete |
| 3a | Test history — run list | `features/runs/RunsListPage.tsx` + `runsFilters.ts` (URL-canonical filters, pagination, phase-strip micro-viz, compare checkboxes) | `GET /v1/pipelines?status&q&project&limit&offset` | partial — plan's `engine=`/`from=`/`to=` filters unsupported by the façade (see follow-up F3) |
| 3b | Test history — per-phase inspection | `features/runs/PhaseWorkspace.tsx` (Output/Artifacts/Prompt/Dialogue tabs: status, timestamps, KPIs, summaries, reasoning digest, warnings, errors, approvals, **tool calls — fixed in this audit**, resolved-prompt provenance), top `PhaseStrip` phase flow | `GET /v1/pipelines/{thread_id}` snapshot (`PhaseResult` incl. `tool_calls`, `transcript_ref`) | complete |
| 4a | Real-time — live status | `streaming/usePipelineStream.ts` (3 stream modes, reconnect/`joinStream(last_event_id)`, healing refetch w/ monotonicity guard), `LiveStatusChip`, top phase flow status | SDK SSE `updates`/`messages-tuple`/`custom` | complete |
| 4b | Real-time — streaming activity | `features/runs/ActivityFeed.tsx` (phase_status dividers, tool-call cards, engine tick rows, jump-to-live), `EngineStrip.tsx` (4 pills + AreaChart, 300-pt buffer) | `phase_status`/`tool_call`/`engine_poll` custom events | partial — reasoning-token rendering deliberately deferred until backend agents stream real `messages-tuple` content (documented in `ActivityFeed.tsx`; durable record is the `transcript_ref` artifact) |
| 4c | Real-time — approval updates | `features/approvals/useApprovalsInbox.ts` (15s visibility-aware poll) + `pendingGateHint` from any open stream (`gate_opened` accelerator) | `threads.search(status=interrupted)` + `gate_opened` custom event | complete |
| 5a | HITL — prompt gate | `hitl/GateModule.tsx` + `gateMachine.ts` + `useResumeGate.ts` (approve / modify w/ CodeMirror editing / skip_phase / abort; provenance chip; context & tools accordions) | `POST /v1/pipelines/{thread_id}/gates/{interrupt_id}/resume` (CAS; 409 → superseded state) | complete |
| 5b | HITL — output gate | same module, `phase_review` payload (approve / revise w/ instructions / discuss / abort; summary, result preview, artifacts, dialogue tail) | same CAS resume route | complete |
| 5c | HITL — multi-turn dialogue | `hitl/DialogueThread.tsx`; discuss loop re-enters `open` via stream-delivered re-interrupt; `PhaseWorkspace` Dialogue tab shows durable history | interrupt cycle + `state.dialogue` channel | complete |
| 5d | HITL — inbox | `features/approvals/ApprovalsInboxPage.tsx` (keyboard-first j/k/a/m/s/x/o, queue + preview, deep link `/approvals/:threadId/:interruptId`, superseded rows gray inline) | `threads.search` + on-focus payload hydration | complete |
| 6 | Prompt catalog | `features/prompts/{PromptsPage,PromptDetailPage,PromptVersionPage,PromptPlaygroundPage}.tsx` (namespace tree, version timeline, diff, rollback-with-confirm, archive/unarchive, create, save version, playground run) | `/v1/prompts` list/create/get/versions/rollback/archive/unarchive + `POST /v1/prompts/{id}/test` (202 → playground run on `playground` assistant) | complete — live playground output streaming is a noted follow-up (F4) |
| 7 | Environment configs | `features/environments/{EnvironmentsPage,EnvironmentDetailPage}.tsx` (CRUD forms, k8s inventory viewer, rescan, staleness chips) | `/v1/catalog/environments` CRUD; `GET /v1/inventory/environments/{id}` + `/rescan` (202) | complete |
| 8a | Admin — connections | `features/admin/{ConnectionsPage,ConnectionDetailPage}.tsx` | `/v1/admin/connections` CRUD + enable/disable + `POST .../test` probe + `GET/PUT .../host-mappings` | complete |
| 8b | Admin — consumers | `features/admin/{ConsumersPage,ConsumerDetailPage}.tsx` (key shown once, rotate) | `/v1/admin/consumers` CRUD + `POST .../rotate` | complete |
| 8c | Admin — system | `features/admin/AdminSystemPage.tsx` (info, identity, feature flags, connectivity) | `GET /v1/system/info` | complete |
| 9 | Ticket / search | `features/work-items/{WorkItemsPage,SavedQueriesPage,WorkItemDetailPage}.tsx` (NL → editable translated query w/ confidence chip → execute; manual mode; saved queries CRUD + run; item create; detail + enrich) | `/v1/work-tracking/query/translate`, `query/execute`, `items` (POST), `items/{key}` (GET), `items/{key}/enrich`, `saved-queries` CRUD | complete |
| 10 | Context | `features/context/{ContextPage,SummariesTab,DocumentsTab,EvidenceTab}.tsx` (`?tab=`; generate-now 202 → run link; evidence grouped by source; documents list/upload/delete) | `POST /v1/context/summaries` (202), `GET /v1/context/evidence`, `/v1/documents` | complete |
| 11 | Usage analytics | `features/analytics/AnalyticsPage.tsx` (+ `analyticsFilters.ts` window presets) | `GET /v1/analytics/usage` | complete |
| 12 | Health / connectivity | `health/{useSystemHealth,ConnectivityProvider}.tsx`, sidebar status dot, `auth/ApiKeyGate.tsx`, `RequireRole` | `GET /v1/system/info` poll | complete |
| 13 | Phase-subset / single-phase runs | `features/runs/{PreflightModal,useRerun}.ts(x)` (readiness list from `phase_results`, prereq misses, gates mode) + grid row / header entry points + wizard phase toggles | `runs.create(threadId, pipeline, configurable.phases)` (input `{}`, reject strategy) | complete |
| 14 | Run abort | gate-state: `hitl` machine `abort` action via CAS resume; busy-run: header `AbortConfirm` → `features/runs/useAbortRun.ts` (**wired in this audit**) | CAS resume route; `POST /v1/pipelines/{thread_id}/abort` (cancels active runs; backend cleanup calls engine abort/teardown) | complete |
| 15 | Log search | `features/logs/LogsPage.tsx` + `logsFilters.ts` (URL-carried `?q&from&to&thread&service&level`; run header now deep-links `?thread=` — **fixed in this audit**) | `POST /v1/logs/search` | complete |
| 16 | Golden configs | `features/golden-configs/{GoldenConfigsPage,GoldenConfigDetailPage}.tsx` (structured config viewer/editor, prompt pins, gate matrix, save-new-version, "Start run with this config" → wizard `?golden=`) | SDK `assistants.search/get/update` (graph `pipeline`) | complete — creation of NEW assistants is server-side by design (plan: "managed via /v1 admin") |
| 17 | Artifact viewing | `features/artifacts/{ArtifactViewerPage,artifactUrl}.ts(x)` (CodeMirror text/JSON, iframe HTML, binary download; memory:// and s3:// key extraction) | `GET /v1/artifacts/{key:path}` same-origin proxy w/ `x-api-key` | complete |
| 18 | Run comparison | `features/compare/{ComparePage,CompareSelectBar,compareModel}.ts(x)` (`/runs/compare?ids=`, 2–4 columns, per-phase duration divergence, KPI best/worst) | `GET /v1/pipelines` + per-thread `GET /v1/pipelines/{thread_id}` (client-composed) | complete |

## Fixes applied during this audit (all < ~30 lines each, suite green)

1. **Settings reachable** — `/settings` was routed (real `SettingsPage`) but no
   UI element linked to it. Added a Settings link to the sidebar identity card
   (`components/layout/Sidebar.tsx`).
2. **Logs deep link wired** — `logsFilters.ts` documented a `?thread=` deep
   link "from run pages", but no run screen emitted it. Added a Logs link to
   the run-detail header (`features/runs/RunDetailPage.tsx`).
3. **Historical tool calls rendered** — `PhaseResult.tool_calls` exists in the
   snapshot schema (`@apex/pipeline-events` `state.ts`) but was only shown for
   live streams (ActivityFeed). Added a "Tool calls" section to the Output tab
   (`features/runs/PhaseWorkspace.tsx`) so completed/historical runs retain
   tool-call inspection (legacy parity item).
4. **Busy-run abort wired** — abort was only possible through an open HITL
   gate; a mid-phase busy run (e.g. engine polling) had no abort path despite
   the backend shipping `POST /v1/pipelines/{thread_id}/abort` (`abortPipeline`,
   202, cancels active runs). Added `features/runs/useAbortRun.ts` and a header
   `AbortConfirm` (same type-to-confirm affordance) shown when the thread is
   busy and no gate is open (`features/runs/RunDetailPage.tsx`).

## Follow-ups (documented, not fixed)

| # | Gap | Where | Effort |
|---|---|---|---|
| F1 | "Open in APEX Load ↗" external link: `EngineHandle` carries no console URL (`engine/connection_id/external_run_id/extras` only — `src/apex/domain/pipeline.py`). RunRail shows engine + external_run_id chip without a link. Needs the backend to surface a console URL (e.g. in `extras`) before the UI can link out. | backend `apex_load` adapter + `features/runs/RunRail.tsx` | ~0.5 day (backend-led) |
| F2 | `/v1/engines/runs*` read surface unused: `queryKeys.engines.*` was reserved (append-only factory) but no screen reads the engine-runs projection (`listEngineRuns`/`getEngineRuns`) or the engine-level kill (`abortEngineRun`). Run-level abort (fix 4) covers the legacy abort capability; an engine-run history panel would be additive. | new panel on run detail or admin | ~1 day |
| F3 | Runs-grid `engine=`/`from=`/`to=` filters from the plan's route table: the `/v1/pipelines` façade only supports `status/q/project/limit/offset` (`src/apex/routers/pipelines.py`). Backend filter params first, then `runsFilters.ts` is built to extend. | backend façade + `features/runs/runsFilters.ts` | ~1 day |
| F4 | Live playground output: `POST /v1/prompts/{id}/test` 202 renders an accepted card + run link; streaming the playground run inline is a noted follow-up in `PromptPlaygroundPage.tsx`. | `features/prompts/PromptPlaygroundPage.tsx` reusing `usePipelineStream` | ~1 day |
| F5 | Reasoning-token live rendering: deliberately omitted while backend stub agents emit no meaningful `messages-tuple` content (`ActivityFeed.tsx` header comment); the rAF-coalesced token buffer (`streaming/tokenBuffer.ts`) is already built and tested. | `features/runs/ActivityFeed.tsx` | ~1–2 days when real agents stream |

## Sweeps

- **Dead placeholder pages:** none routed. The only `FeaturePlaceholder` usage
  is the catch-all 404 (`features/not-found/pages.tsx`) — intentional.
- **Broken inter-screen links:** every `Link`/`navigate` target greps clean
  against the route table (`/runs*`, `/approvals*`, `/prompts*`,
  `/golden-configs*`, `/work-items*`, `/environments*`, `/context`,
  `/analytics`, `/logs`, `/admin/*`, `/settings`, `/runs/compare?ids=`,
  `/runs/new?step=&draft=&golden=`). The two missing-link findings
  (Settings, Logs deep link) were fixed above.
- **TODO/FIXME comments:** none in dashboard src. The only "follow-up" markers
  are the two deliberate notes in `PromptPlaygroundPage.tsx` (F4).
