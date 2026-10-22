# Coverage matrix — holistic UX review (2026-10-17)

**Final status.** This inventory was built before the review ran and is kept as the route map.
The outcome of the review — findings, scorecard, what was and was not reached — is in
[REPORT.md](REPORT.md). Walkthrough results are recorded below.

`✓` covered · `~` partial · `✗` could not reach (reason recorded)

Source of truth for this inventory: `apps/dashboard/src/routes/router.tsx` (36 route
entries), `components/layout/Sidebar.tsx` (navigation), and a source sweep for
`role="tablist"`, `<Dialog`, and `useSearchParams` sub-state.

## A. Routes and sub-views

| # | Route | Topbar title (route handle) | Sidebar entry | Sub-views / URL state | Modals, drawers, preflight |
|---|---|---|---|---|---|
| 1 | `/` | Test Operations Dashboard | Testing › Home | Needs-your-attention rail; Key metrics (Execution Health, Test Results); Recent Runs; Resume Drafts; first-run empty ("Start your first pipeline") | — |
| 2 | `/runs` | Tests | Testing › Tests (badge) | `?status &q &project &limit &offset`; `?selected` inline inspector; `?runSection`; `?phase`; `?tab`; compare checkbox selection; rows/pagination | PreflightModal (re-run), OverflowMenu |
| 3 | `/runs/new` | New Test | New Test (top-level) | `?step=` × 6 steps (Scope, Config, Work Items, Context, Prompts, Review), `?draft=`, `?golden=`; autosave + resume | Launch confirm (LaunchRunButton), ReleaseMappingPicker, unsaved-changes blocker |
| 4 | `/runs/compare` | Compare Runs | (from Tests) | `?ids=` 2–4 runs; baseline selector; per-phase + KPI deltas | CompareSelectBar |
| 5 | `/runs/:threadId` | Run Detail | (Tests stays active) | Section nav Overview / Decision / Workspace / Timeline; `?phase=`; `?tab=` (Phase Details, Pipeline Log, Agent Reasoning); RunRail; EngineStrip; LiveStatusChip | PreflightModal, AbortConfirm, OverflowMenu |
| 6 | `/runs/:threadId/reviews/:interruptId` | Review Run | — | Review Workspace (prompt_review and phase_review); `?source=`, `?phase=` | AbortConfirm |
| 7 | `/runs/:threadId/phases/:phase` | Phase Detail | — | PhaseWorkspace tabs `?tab=details\|log\|reasoning` (legacy `activity` → log) | gate slot inline |
| 8 | `/runs/:threadId/timeline` | Run Timeline | — | RunTimeline / timelineModel | — |
| 9 | `/runs/:threadId/artifacts/:name` | Artifact Viewer | — | text/JSON (CodeMirror), markdown document, HTML iframe, binary download; `memory://` and `s3://` keys | — |
| 10 | `/approvals` | **Tests** | (no entry — reached via "Needs review" button on `/runs`) | Queue + Review Workspace; j/k/o/m/x keyboard model; superseded rows | Keyboard-shortcuts dialog |
| 11 | `/approvals/:threadId/:interruptId` | Approval | — | ApprovalDetailPage (same component as #6) | AbortConfirm |
| 12 | `/cycles` | Test Cycles | Testing › Test Cycles (feature-flag `features.cycles`) | List + NewCycle form; disabled state when flag off | — |
| 13 | `/cycles/:cycleId` | Cycle Workspace | — | 8 steps: Overview, Scope, Test Plan, Readiness, Scripts, Executions, Results & Reports, Postmortem; CycleEditor, CycleSources, ProviderValidation, ProviderScriptImport, ScriptStorage, ExecutionWorkspace, CycleHandoff, FrozenEvidenceDiff, ReportEvidence, CycleResults; review actions (submit/approve/request_revision/archive/restore) | Unsaved deliverable changes, Unsaved provider import, Unsaved cycle follow-up |
| 14 | `/work-items` | Tickets | Testing › Tickets | NL query → editable translated query (confidence chip) → execute; manual mode | Save query, New work item |
| 15 | `/work-items/saved` | Saved Queries | (from Tickets) | List, run, "Modified query" state | Edit saved query, Delete saved query |
| 16 | `/work-items/:provider/:itemId` | Work Item | — | Detail + enrich | Enrich `{key}` |
| 17 | `/environments` | Applications | Resources › Applications | Registry list, filters | Delete environment, OverflowMenu |
| 18 | `/environments/:id` | Environment | — | CRUD form, k8s inventory viewer, rescan, staleness chips | — |
| 19 | `/context` | Knowledge & Documents | Resources › Knowledge & Documents | Tabs `?tab=` Summaries / Documents; generate-now (202) → run link; upload | Delete document `{name}` |
| 20 | `/evidence` | Evidence | Investigation › Evidence | Filter bar (project/application/test/source/search); scope rail; result list; detail tabs Content / Details / Provenance | — |
| 21 | `/golden-configs` | Test Presets | Resources › Test Presets | List | — |
| 22 | `/golden-configs/:assistantId` | Test Preset | — | Config viewer/editor, prompt pins, gate matrix, save new version, "Start run with this config" | — |
| 23 | `/prompts` | Prompts | Resources › Prompts | Namespace tree, list, archive/unarchive | New prompt |
| 24 | `/prompts/:ns/:name` | Prompt | — | Tabs Content / Versions; inline edit; save version; archive toggle | Set v`{n}` active (rollback) |
| 25 | `/prompts/:ns/:name/versions/:v` | Prompt Version | — | Version view + PromptDiff (lineDiff) | Set v`{n}` active (rollback) |
| 26 | `/prompts/:ns/:name/playground` | Template Preview | — | Render/test (202 accepted card + run link — F4 partial) | — |
| 27 | `/analytics` | Agent Analytics | Investigation › Agent Analytics | WindowPresets; model/agent filters; usage charts | — |
| 28 | `/logs` | Logs | Investigation › Logs | `?q &from &to &thread &service &level` | — |
| 29 | `/apex-load` | APEX Load | (Connections → APEX Load tab) | Unconfigured ("Connect APEX Load"); connection readiness; tabs Executions / Authoring (scenarios + load-model validation, schedules/occurrences/approvals, executions + analysis, error evidence) | — |
| 30 | `/admin/connections` | Connections | Admin › Connections | Tabs Connections / APEX Load; registry | — |
| 31 | `/admin/connections/:id` | Connection | — | Tabs Config / Host mappings; ProviderConnectionFields; test probe; enable/disable | Delete connection `{name}` |
| 32 | `/admin/consumers` | Consumers | Admin › Consumers | Registry + overflow actions | Edit consumer, Rotate key, Delete consumer, API key created/rotated (shown once) |
| 33 | `/admin/consumers/:id` | Consumer | — | Detail, scopes | — |
| 34 | `/admin/system` | System status | Admin › System status | Info, identity, feature flags, connectivity | — |
| 35 | `/settings` | Settings | Settings (own group) | TabList: Personal preferences / Connection & session | — |
| 36 | `*` | Not Found | — | FeaturePlaceholder 404 | — |

## B. Shell and cross-cutting surfaces

| Surface | Where | Notes |
|---|---|---|
| ApiKeyGate | pre-shell | key paste, dev bypass (`VITE_APEX_DEV_AUTH`), demo auto-auth |
| Sidebar | expanded / collapsed rail / mobile drawer | groups New Test, Testing, Resources, Investigation, Admin (role-gated), Settings |
| Account panel | sidebar identity button | Dummy data switch / Reset demo, Theme select, Sign out |
| Topbar | all routes | `h1` = route handle title, DEMO · OFFLINE chip, TopbarContribution slot |
| Navigation drawer | compact ≤ breakpoint | `Dialog` ariaLabel "Navigation" |
| ConsumerKeyLifecycle | shell-level | "API key created/rotated", "API key request still pending" |
| NavigationGuards | shell-level | single react-router blocker, priority-ordered guards |
| RouteErrorBoundary | route level | render/loader error |
| ProblemCard / CachedDataWarning | many screens | 4xx/5xx and stale-cache surfaces |
| Connectivity status | sidebar footer | Connected / Checking… / Degraded / Unreachable / Offline simulation |

## C. Review states per view (plan step 3)

Each area is to be visited in: empty · loading · populated · very large · validation error ·
backend down / 5xx · permission denied (viewer, operator, project-scoped admin,
app-scoped admin, global admin) · stale / superseded · leaving with unsaved changes.

## D. Themes, viewports, modes

- Themes (6, not the 5 in `apps/dashboard/README.md`): `apex-light` **(code default)**, `dark`, `light`, `solarized-dark`, `solarized-light`, `monokai-dimmed`, plus `system` preference.
- Reduced motion: `data-reduce-motion` on `<html>` via preferences.
- Viewports: 390 · 768 · 1440 px, and 200% zoom.
- Data modes: demo (`DASHBOARD_DATA_MODE=demo`, ACC add-a-line v2.0) and connected (simulated engine).
- Roles: viewer, operator, admin; scopes: unscoped (global), project-scoped, app-scoped.

## E. Task walkthroughs (plan step 2)

| ID | Walkthrough | User | Status |
|---|---|---|---|
| a | New test draft → scope → config → context upload → prompt review → Review Test → Launch Test; abandon and resume a draft | Operator | ✓ |
| b | Prompt gate: approve; modify then approve; skip a phase | Approver | ✓ |
| c | Output gate: revise with instructions; multi-turn discuss; approve | Approver | ~ revise and approve exercised; multi-turn discuss not run |
| d | Approvals inbox: clear 5+ gates keyboard-only (j/k/o/m/x) | Approver | ~ all shortcuts verified, 3 gates cleared; `m` is broken on the canonical review URL |
| e | Live run: stream, kill network / restart backend mid-phase, reconnect, jump-to-live, no lost or duplicated events | Operator | ✗ no live stream could be established in either data mode — see REPORT.md |
| f | Concurrency: same gate in two browsers, decide in both, 409 supersede | Approver ×2 | ✓ passes |
| g | Re-run one phase through preflight, including a missing prerequisite | Operator | ✓ passes |
| h | Compare two runs against a chosen baseline | Release manager | ✓ |
| i | Prompt catalog: edit, save version, diff, roll back, playground | Operator | ✓ |
| j | Work items: NL query, edit translated query, run, save, modify saved query | Operator | ✓ |
| k | Admin: create consumer (key once), rotate key, add connection, test it, map hosts | Admin | ~ consumer, one-time key and connections done; host mappings not exercised |
| l | Governance audit of a finished run: who approved what, prompt version, tool calls, evidence | Governance reviewer | ✓ — and it fails; every approval reads `actor: "unknown"` |
| m | Hand off to APEX Load and back per the handoff contract | Operator | ✗ no Load console URL configurable in this environment |

## F. Explicitly out of scope

No application, backend, or `package.json` changes. axe-core is installed in the
scratch directory only. Prior findings in `docs/qa/*`,
`docs/dashboard-usability-remediation.md`, `design-qa.md`, and the known
`PARITY.md` partials F3/F4 are not re-reported unless regressed.

## G. Outcome

Five area sweeps plus the lead reviewer produced **120 findings**; an independent reviewer
reproduced 34 of them and the counted claims, downgrading two and deleting one. 61 captures are
registered in [capture-register.json](capture-register.json) and rendered in
[report.html](report.html).

Companion documents: [LOAD_CONVERGENCE.md](LOAD_CONVERGENCE.md) ·
[CONSISTENCY.md](CONSISTENCY.md) · [CORRECTNESS.md](CORRECTNESS.md) ·
[ACCESSIBILITY.md](ACCESSIBILITY.md) · [AGENT_UX.md](AGENT_UX.md) · [AREAS.md](AREAS.md) ·
[LEAD_FINDINGS.md](LEAD_FINDINGS.md) · [VERIFICATION.md](VERIFICATION.md).
