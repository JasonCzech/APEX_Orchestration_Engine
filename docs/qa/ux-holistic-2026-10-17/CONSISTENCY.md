# Consistency review — APEX Orchestration dashboard

**Date:** 2026-10-17 · **Scope:** `apps/dashboard` (36 routes) · **Method:** source only, no browser
**Baseline:** `docs/design/APEX_UX_CONTRACT.md` v1, `apps/dashboard/README.md`, `docs/adr/0006-dashboard-architecture.md`

## Verdict

The dashboard's *primitives* are in good shape — `ProblemCard` is adopted at 42 call sites, `Dialog` at 22, `role="alert"`/`role="status"` at ~240, `.data-table .num` is the single numeric-alignment rule, every Recharts series resolves through `var(--chart-N)` with `isAnimationActive={false}`, and the time-series palette is deliberately capped at 5 series + "other" so it never exhausts the six-colour ramp. What is not in shape is *reach*: almost every primitive the UX contract names is used in one or two places while each feature folder reimplements it locally. The contract's `TabList` covers 2 of 11 tablists; `.ux-filters` covers 1 of 18 tables; the Rows control covers 1 of 5 paginated tables; sortable headers cover 1 of 18 tables; `--modal-backdrop` is defined in all six themes and consumed by zero components while seven dialogs hardcode three different black scrims. Two of these local copies have crossed into outright breakage that no test can catch because `vite.config.ts` sets `test: { css: false }`: `.tonal-card` — the app's error-banner class, used at 27 call sites including the shared `CachedDataWarning` — is defined only in `features/runs/run-detail.css`, so on 21 of 36 routes every error banner renders as unstyled body text (CON-01); and `components/FeaturePlaceholder.css` redefines the global `.dash-empty` primitive, so visiting any unknown URL permanently flattens ~40 empty states for the rest of the session (CON-04). Terminology is the other systemic gap: the same concept carries 2–4 user-visible names, with the navigation and the body copy disagreeing on nearly every entity (CON-03). Nothing here is a P0; CON-01 and CON-04 are each a few lines to fix.

## Findings

| ID | Sev | Axis | Summary | Occurrences |
|---|---|---|---|---|
| [CON-01](#con-01) | P1 | correctness, consistency | `.tonal-card` error banners render unstyled on 21 of 36 routes — the class is defined only in a run-detail chunk | 27 call sites / 21 routes |
| [CON-02](#con-02) | P1 | accessibility, consistency | 7 of 11 `role="tablist"` blocks have no `aria-controls`, no roving `tabIndex`, no arrow keys, and no `tabpanel`; 3 tab classes are under the 32 px target floor | 7 tablists, 3 undersized |
| [CON-03](#con-03) | P1 | consistency, ease of use | One concept, many user-visible words: Test/Run/Pipeline/Thread, Gate/Approval/Review/Decision, Tickets/Work items/Items, Applications/Environments | 6 concepts, 90+ strings |
| [CON-04](#con-04) | P2 | correctness, consistency | `components/FeaturePlaceholder.css` globally redefines `.dash-empty`; the 404 page tells users the screen "lands in a later dashboard milestone" | 1 stylesheet → ~40 states |
| [CON-05](#con-05) | P2 | consistency, ease of use | Empty, loading, error, not-found and unconfigured are not visually distinguishable: `.dash-empty` carries four of them; 12 bespoke skeletons, 6 heights, 2 inverted pulse envelopes | 12 skeletons, 5 state treatments |
| [CON-06](#con-06) | P2 | consistency | One dialog shape reimplemented six times: 10 overlay classes, 7 hardcoded scrims, 3 z-index scales, `--modal-backdrop` with zero consumers | 10 overlays / 22 dialogs |
| [CON-07](#con-07) | P2 | consistency, correctness | Destructive-confirm strength and button variant are arbitrary: 3 confirm strengths, 2 type-to-confirm conventions, 5 destructive actions styled `btn-ghost` | 11 destructive actions |
| [CON-08](#con-08) | P2 | consistency, correctness | 7 timestamp formatters produce 5 different renderings of the same instant, and no timestamp anywhere states its timezone | 7 formatters / 33+ sites |
| [CON-09](#con-09) | P2 | correctness, consistency | 4 tokens are consumed without a usable fallback but defined in only 1 of 6 themes or in none | 4 tokens / 7 sites |
| [CON-10](#con-10) | P2 | correctness | README documents 5 themes with `dark` default; code ships 6 with `apex-light` default, applied after a network fetch so every cold load animates dark→light | 2 docs + 1 boot path |
| [CON-11](#con-11) | P2 | consistency, ease of use | Each contract "Library control" reaches exactly one table: filters 1/18, active-filter indicator 1/18, Rows 1/5, sort 1/18; cycles pagination diverges on every axis | 18 tables |
| [CON-12](#con-12) | P3 | consistency | The same error-rate metric renders at 1 dp on Home and 2 dp on Run Detail; 5 percent precisions in use | 11 percent sites |
| [CON-13](#con-13) | P3 | correctness, accessibility | `/runs` table has two columns headed "Run"; "Applications" header over a single-value cell that the card view calls "Application" | 3 headers |
| [CON-14](#con-14) | P3 | consistency, accessibility | Page-heading practice splits three ways; `features/cycles` emits a second `<h1>` under the Topbar's | 3 double-h1, 5 title mismatches |
| [CON-15](#con-15) | P3 | consistency | Button copy: Title Case vs sentence case for the same pattern, 2 labels + 2 variants for one action, glyphs in labels | 14 strings |
| [CON-16](#con-16) | P3 | consistency, ease of use | ID truncation happens only in `cycles` (11×, 8 chars, never copyable); copy affordances take 3 shapes with 2 feedback behaviours | 11 + 4 sites |
| [CON-17](#con-17) | P3 | consistency | Shared helpers duplicated verbatim instead of imported | 5 duplications |

---

<a id="con-01"></a>
## CON-01 — `.tonal-card` error banners render unstyled on 21 of 36 routes

**P1 · correctness, consistency · all routes outside `/runs*` and `/approvals*`**

### Evidence

`.tonal-card` is the app's inline error/warning/success banner. It has exactly three CSS rules, all in one feature stylesheet:

- `features/runs/run-detail.css:368` — `.tonal-card`
- `features/runs/run-detail.css:376` — `.tonal-card.warning`
- `features/runs/run-detail.css:382` — `.tonal-card.danger`

That stylesheet is imported by exactly one module — `features/runs/RunDetailPage.tsx:28` (`import './run-detail.css'`). `vite.config.ts` does not set `build.cssCodeSplit`, so it defaults to `true`: the rules ship in the lazy chunk, not the entry bundle (`src/main.tsx` → `theme/index.css`).

The class is consumed at **27 call sites across 18 modules in 8 features**, including the shared `components/CachedDataWarning.tsx:10`, which is itself imported by 26 modules. Only two chunks reach `RunDetailPage`:

- `features/runs/RunsListPage.tsx:37` → `import { RunWorkspace } from './RunDetailPage'`
- `features/approvals/ApprovalsInboxPage.tsx:32` → `import { RunWorkspace } from '@/features/runs/RunDetailPage'`

Everything else does not. `features/prompts/*`, `features/cycles/*`, `features/context/*` and `features/home/HomePage.tsx` contain **no** import from `@/features/runs` that carries CSS (HomePage's two imports are from `runDisplay.ts`, which has none).

**Affected routes (21 of 36):** `/`, `/context`, `/evidence`, `/golden-configs`, `/golden-configs/:assistantId`, `/environments`, `/environments/:id`, `/admin/connections`, `/admin/connections/:id`, `/admin/consumers`, `/admin/consumers/:id`, `/logs`, `/prompts`, `/prompts/:ns/:name`, `/prompts/:ns/:name/versions/:v`, `/prompts/:ns/:name/playground`, `/work-items`, `/work-items/saved`, `/work-items/:provider/:itemId`, `/cycles`, `/cycles/:cycleId`.

**Expected:** an API failure on Home renders a red-bordered, red-tinted banner.
**Actual:** on a cold load of `/`, `features/home/HomePage.tsx:46` (`className="tonal-card danger" role="alert"`) renders with no border, no background, no padding and no danger colour — visually identical to the paragraph above it. The same applies to all 7 prompts banners (`PromptsPage.tsx:304`, `PromptDetailPage.tsx:119`, `:352`, `PromptPlaygroundPage.tsx:335`, `:340`, `:365`, `RollbackConfirm.tsx:45`), both cycles banners (`CycleEditor.tsx:332`, `cycles/pages.tsx:386`) and `SummariesTab.tsx:378`. It styles correctly only if the user happened to open a `/runs` or `/approvals` route earlier in the same session, because the injected `<link>`/`<style>` is never removed — so the bug is *intermittent by navigation history*, which is the worst shape for a reproduction report.

**Second, independent defect in the same class:** `.tonal-card.success` has **no CSS rule at all** (only `.warning` and `.danger` exist), yet it is used three times — `features/approvals/ApprovalsInboxPage.tsx:338` (the "Decision receipt" confirmation), `features/context/SummariesTab.tsx:378`, `features/prompts/PromptPlaygroundPage.tsx:365`. Those three render with the neutral `var(--border)` and no success tint even on routes where the stylesheet *is* loaded.

**Why no test caught it:** `vite.config.ts` sets `test: { css: false }` — Vitest never evaluates stylesheets, so class-presence assertions pass while the computed style is wrong.

### Recommended change

1. Move the `.tonal-card` block (`features/runs/run-detail.css:368-386`) into `src/theme/primitives.css`, beside `.dash-empty` and `.status-badge` — it is a cross-feature primitive, and `primitives.css` is already in the entry bundle via `theme/index.css`.
2. Add the missing `.tonal-card.success` rule there, mirroring `.warning`/`.danger` with `--success-border` / `--success-bg` / `--success-text`.
3. Promote `components/CachedDataWarning.tsx` to its own `CachedDataWarning.css` (as `ProblemCard` already does) so the shared component owns its styling rather than inheriting a feature's.
4. Add a lint rule or a CI grep: a class used in `components/` must be defined in `src/theme/*.css`, never in `features/*/`.

---

<a id="con-02"></a>
## CON-02 — 7 of 11 tablists implement none of the contract's tab semantics

**P1 · accessibility, consistency · `/context`, `/prompts/:ns/:name`, `/admin/connections`, `/admin/connections/:id`, `/apex-load`, `/runs/:threadId`, `/work-items`**

The UX contract names the tabs primitive and its behaviour: *"`components/ux/TabList.tsx`: automatic activation, wrapping arrows, Home/End, one tab stop and linked named panels."* `TabList.tsx` implements all of it correctly — `aria-controls` (line 31), roving `tabIndex` (line 32), Arrow/Home/End with wrapping (lines 34-49), stable `id` scheme for panel linkage.

It is used twice. Nine other blocks hand-roll `role="tablist"`:

| # | Location | `aria-controls` | roving `tabIndex` | Arrow/Home/End | linked `tabpanel` |
|---|---|---|---|---|---|
| — | `features/settings/SettingsPage.tsx:337` (TabList) | ✅ | ✅ | ✅ | ✅ `:338` |
| — | `features/runs/PhaseWorkspace.tsx:101` (TabList) | ✅ | ✅ | ✅ | ✅ `:102` |
| 1 | `features/context/ContextPage.tsx:41-53` | ❌ | ❌ | ❌ | ❌ none exists |
| 2 | `features/prompts/PromptDetailPage.tsx:357-371` | ❌ | ❌ | ❌ | ❌ none exists |
| 3 | `features/admin/ConnectionsPage.tsx:333-352` | ❌ | ❌ | ❌ | ❌ none exists |
| 4 | `features/admin/ConnectionDetailPage.tsx:703-716` | ❌ | ❌ | ❌ | ❌ none exists |
| 5 | `features/apex-load/ApexLoadWorkspacePage.tsx:566-585` | ❌ | ❌ | ❌ | ❌ none exists |
| 6 | `features/runs/EngineStrip.tsx:115-127` | ❌ | ❌ | ❌ | ❌ none exists |
| 7 | `features/runs/PromptTabsEditor.tsx:64-82` | ❌ | ❌ | ❌ | ⚠️ `:85`, `:98` exist but are unlinked |
| 8 | `features/new-test/NewRunWizard.tsx:498-519` | ✅ `:514` | ✅ `:515` | ✅ `:453-459` | ✅ `:81-82` |
| 9 | `features/context/EvidenceTab.tsx:239-242` | ✅ | ✅ | ✅ `:151-154` | ✅ + `aria-labelledby` |

Rows 8 and 9 are compliant hand-rolls (the contract explicitly exempts the wizard: *"Wizard retains its existing accessible step tabs"*). **Rows 1–7 are the finding.**

Consequences, in order of severity:

- **No roving `tabIndex`** means each tab is its own tab stop. `/apex-load` and `/admin/connections` are 2 extra stops each; `/runs/:threadId`'s EngineStrip adds 4. The contract's "one tab stop" is violated in 7 places.
- **Six of the seven have no `role="tabpanel"` element anywhere.** `aria-selected="true"` therefore announces a selected tab that controls nothing, and there is no way for a screen-reader user to move from the tab to the content it switched.
- **`role="tab"` with `disabled`** at `PromptTabsEditor.tsx:76` and `PromptDetailPage.tsx:366` removes the tab from the accessibility tree entirely rather than marking it `aria-disabled`, so the tab count changes under the user.

### Target sizes (same root cause: the local classes don't inherit `ux.css`)

`theme/ux.css:27-30` gives `.workspace-tab` `min-height: 36px`, satisfying the contract's "32px small buttons". The seven local classes set none:

| Class | padding | font-size | `min-height` | approx. height |
|---|---|---|---|---|
| `.workspace-tab` (`theme/ux.css:27`) | 8/14 px | 11 px | **36 px** | 36 px ✅ |
| `.prompt-tab` (`features/prompts/prompts.css:416`) | 8/14 px | 13 px | — | ~36 px ✅ |
| `.prompt-review-tab` (`features/runs/prompt-tabs-editor.css:8`) | 8/14 px | 11 px | — | ~33 px ⚠️ |
| `.adm-tab` (`features/admin/admin.css:280`) | 5/12 px | 12 px | — | ~28 px ❌ |
| `.ctx-tab` (`features/context/context.css:51`) | 4/10 px | 11 px | — | ~25 px ❌ |
| `.engine-metric-tab` (`features/runs/live.css:256`) | 3/10 px | **10.5 px** | — | ~22 px ❌ |

`.engine-metric-tab` at ~22 px fails WCAG 2.5.8 Target Size (Minimum, 24×24 px) outright, and `.ctx-tab` at ~25 px clears it by 1 px. `.engine-metric-tab`'s `font-size: 10.5px` is also the app's smallest text and the only fractional font size in the codebase.

### Related, same area

`features/runs/RunDetailPage.tsx:299-316` renders the run's section switcher as `nav[aria-label="Run sections"]` containing `.btn.btn-secondary` with `aria-current="page"` — **not** tabs. That is a defensible choice (the non-embedded branch uses real `<Link>`s to real routes, so `aria-current="page"` is correct), but it means the app teaches users three different interaction models for "switch the view in place": contract tabs, hand-rolled tabs, and current-page buttons.

### Recommended change

1. Replace the markup in the 7 non-compliant blocks with `<TabList …>` and give each a linked panel, exactly as `SettingsPage.tsx:337-338` does. Files: `features/context/ContextPage.tsx`, `features/prompts/PromptDetailPage.tsx`, `features/admin/ConnectionsPage.tsx`, `features/admin/ConnectionDetailPage.tsx`, `features/apex-load/ApexLoadWorkspacePage.tsx`, `features/runs/EngineStrip.tsx`, `features/runs/PromptTabsEditor.tsx`.
2. Add a `disabled`-tab affordance to `components/ux/TabList.tsx` using `aria-disabled` + skip-on-arrow, so `PromptTabsEditor` and `PromptDetailPage` can migrate without losing their disabled states.
3. Delete `.ctx-tab`, `.adm-tab`, `.engine-metric-tab`, `.prompt-tab` and `.prompt-review-tab` from the five feature stylesheets; let `.workspace-tab` in `theme/ux.css` be the only tab rule. If EngineStrip genuinely needs a denser control, change it to a `RadioChoices`/segmented control rather than a 22 px tab.
4. Decide whether Run Detail's section nav stays buttons-with-`aria-current` and record that in the contract's Tabs row, so it reads as a deliberate exception rather than a third pattern.

---

<a id="con-03"></a>
## CON-03 — One concept, many user-visible words

**P1 · consistency, ease of use · every route**

Six concepts each carry 2–4 user-visible names. The route table, the sidebar, the page body and the dialogs frequently disagree with one another *on the same screen*.

### 3a. Test / Run / Pipeline / Thread

| String | Location | Word used |
|---|---|---|
| `handle('Tests')` for `/runs` | `routes/router.tsx:68` | Test |
| `handle('Tests')` for `/approvals` | `routes/router.tsx:57-58` | Test |
| `label: 'Tests'` (sidebar, `/runs`) | `components/layout/Sidebar.tsx:76` | Test |
| `label: 'New Test'` (sidebar CTA) | `components/layout/Sidebar.tsx:268` | Test |
| `handle('New Test')` | `routes/router.tsx:71` | Test |
| `"Test result: …"` (grid cell) | `features/runs/RunsListPage.tsx:124` | Test |
| `<th>Test result</th>` | `features/runs/RunsListPage.tsx:669` | Test |
| `Default rows in Test History` | `features/settings/SettingsPage.tsx:325` | Test |
| `Tests · All runs` / `Tests · Needs review` | `features/settings/SettingsPage.tsx:329` | **both** |
| `Open Test History` → `/runs` | `features/cycles/pages.tsx:57` | Test |
| `handle('Run Detail')`, `handle('Run Timeline')`, `handle('Compare Runs')` | `routes/router.tsx:81,96,76` | Run |
| `Recent Runs`, `<th>Run</th>`, `No runs yet.` | `features/home/HomePage.tsx:291,303,297` | Run |
| `All runs` (filter pill) | `features/runs/RunsListPage.tsx:499`, `features/approvals/ApprovalsInboxPage.tsx:322` | Run |
| `Open run`, `Re-run…`, `Copy run ID`, `Run information` | `features/runs/RunsListPage.tsx:129,209,208` | Run |
| `Run context`, `Run workspace`, `Run audit trail`, `Run sections` | `RunOverview.tsx:107`, `RunDetailPage.tsx:211,299`, `RunTimeline.tsx:13` | Run |
| `Launch run` (the New Test dialog's submit) | `features/runs/LaunchRunButton.tsx:252` | Run |
| `Start a new run` (empty-state CTA) | `features/runs/RunsListPage.tsx:568` | Run |
| `Go to runs`, `Back to runs` | `ComparePage.tsx:58,258`, `ApprovalsInboxPage.tsx:361` | Run |
| **`Start your first pipeline`** (Home first-run hero) | `features/home/HomePage.tsx:186` | **Pipeline** |
| `Launch your first pipeline run to see it here.` | `features/runs/RunsListPage.tsx:567` | **Pipeline + Run** |
| `What should this pipeline run test?` (placeholder) | `features/runs/LaunchRunButton.tsx:168` | **Pipeline + Run + Test** |
| `Pipeline Progress`, `aria-label="Pipeline progress"` | `features/runs/RunDetailPage.tsx:331,328` | Pipeline |
| `aria-label="Pipeline log"` | `features/runs/PhaseWorkspace.tsx:109` | Pipeline |
| `APEX pipeline` | `features/cycles/ExecutionWorkspace.tsx:217` | Pipeline |
| `<dt>Thread</dt>` + full thread id | `features/runs/RunRail.tsx:156` | **Thread** |
| `<dt>Thread ID</dt>` | `features/context/EvidenceTab.tsx:242` | Thread |
| `aria-label="Thread id filter"` | `features/logs/LogsPage.tsx:266` | Thread |
| `It has not run on this thread.` | `features/runs/PhaseWorkspace.tsx:264` | Thread |
| `aria-label="Loading thread state"` | `features/runs/PreflightModal.tsx:114` | Thread |

The sharpest instances: **`LaunchRunButton.tsx:168`** uses all three words in one sentence (*"What should this pipeline run test?"*). **Home's first-run hero** (`HomePage.tsx:186`) is the very first screen a new user sees and it calls the object a "pipeline" — a word that appears nowhere in the navigation. And `features/cycles/pages.tsx:57` labels a link to `/runs` "Open Test History", while the route handle and the sidebar both say "Tests" and the page's own filter pill says "All runs".

**Recommend: "Test" for the user-facing object, "Run" only for a single execution attempt of one, "Thread" never (it is a LangGraph implementation detail), "Pipeline" never.** Changes: `HomePage.tsx:186` → "Start your first test"; `RunsListPage.tsx:567,568` → "Launch your first test to see it here." / "New Test"; `LaunchRunButton.tsx:168` → "What should this test cover?"; `LaunchRunButton.tsx:252` → "Launch test"; `RunDetailPage.tsx:328,331` → "Test progress"; `PhaseWorkspace.tsx:109` → "Agent log"; `RunRail.tsx:156-157` and `EvidenceTab.tsx:242` → "Test ID"; `LogsPage.tsx:266` → "Test ID filter"; `cycles/pages.tsx:57` → "Open Tests". Route handles `Run Detail`/`Run Timeline`/`Compare Runs` → `Test Detail`/`Test Timeline`/`Compare Tests` in `routes/router.tsx:81,96,76`.

### 3b. Gate / Approval / Review / Decision

Two routes render the identical component under two titles:

- `routes/router.tsx:62-63` — `path: 'approvals/:threadId/:interruptId'`, `handle('Approval')`
- `routes/router.tsx:85-86` — `path: 'runs/:threadId/reviews/:interruptId'`, `handle('Review Run')`
- Both resolve to `ApprovalDetailPage`, which `features/approvals/pages.tsx:4` re-exports from `ApprovalsInboxPage`.

So the same screen is titled "Approval" or "Review Run" depending on which link the user followed. Meanwhile:

| Word | Occurrences |
|---|---|
| **Gate** | `ApprovalsInboxPage.tsx:359` "No gates awaiting review.", `:360` "New gates appear here…", `:377` `aria-label="Pending gates"`, `:402` "Select a gate from the queue", `:468` "Loading gate", `:475` "Gate failed to load", `RunRail.tsx:117` "Review gate", `PreflightModal.tsx:149` `aria-label="Gates mode"`, `GoldenConfigDetailPage.tsx:120` `aria-label="Gate policy"` |
| **Approval** | `router.tsx:63` `handle('Approval')`, `HomePage.tsx:223` "Approvals unavailable", `PhaseWorkspace.tsx:278` "Approvals", `RunRail.tsx:227,228,230` "Approvals history" / "Approvals" / "No approvals yet.", `orchestrationStatusLabel` → `'Awaiting approval'` (`runDisplay.ts:163`), `PhaseWorkspace.tsx:103` "…appears below after approval." |
| **Review** | `router.tsx:86` `handle('Review Run')`, sidebar badge `"${count} test(s) need review"` (`Sidebar.tsx:43`), `ApprovalsInboxPage.tsx:323` + `RunsListPage.tsx:500` "Needs review", `:386` `aria-label="Review workspace"`, `:370` "Loading reviews", `:353` "Reviews failed to load", `GateModule.tsx:401,404` "Review", `gateLabel()` → "Review prompts" / "Review phase results" / "Review required" (`runDisplay.ts:171`), `ConfigStep.tsx:340,349,350` "Review policy" / "Prompt review" / "Output review" |
| **Decision** | `RunDetailPage.tsx:302,309` "Decision" (the section-nav button), `ApprovalsInboxPage.tsx:340` "Decision receipt", `.gate-decision-context` / `.gate-primary-decision` CSS, `PhaseReviewPanel.tsx:82` "…later phases wait for the decision." |

One flow — a paused run awaiting a human — is called a *gate* in 9 strings, an *approval* in 7, a *review* in 14 and a *decision* in 4, and the Run Detail nav button that opens it says "Decision" while the route it navigates to is titled "Review Run" and the inbox it mirrors is titled "Approval".

**Recommend: "Review" as the user-facing noun and verb ("Needs review", "Review", "Reviewed"); "Decision" only for the recorded outcome ("Decision receipt" is good); "Gate" and "Approval" retired from user-visible copy** — `gate` stays as the internal `interrupt`/`GateKind` vocabulary, which is correct and should not change. Changes: `routes/router.tsx:63` `handle('Approval')` → `handle('Review')` and `:86` `handle('Review Run')` → `handle('Review')` (also collapsing the two-titles-one-screen problem); `ApprovalsInboxPage.tsx:359,360,377,402,468,475` gate→review; `RunRail.tsx:117,227,228,230` → "Review history" / "Reviews" / "No reviews yet."; `RunDetailPage.tsx:302,309` "Decision" → "Review"; `HomePage.tsx:223` → "Reviews unavailable"; `runDisplay.ts:163` `'Awaiting approval'` → `'Awaiting review'`; `PhaseWorkspace.tsx:278` → "Reviews".

### 3c. Tickets / Work items / Items — three names, one entity

- Sidebar: `label: 'Tickets'` (`components/layout/Sidebar.tsx:79`)
- Route handles: `handle('Tickets')` (`router.tsx:132`), `handle('Work Item')` (`router.tsx:142`), `handle('Saved Queries')` (`router.tsx:137`)
- Page `<h2>`: "Tickets" (`WorkItemsPage.tsx:790`, `SavedQueriesPage.tsx:358`), tablist `aria-label="Ticket sections"` (`:791`, `:359`)
- Create trigger: **"New item"** (`WorkItemsPage.tsx:799`); dialog submit: **"Create item"** (`:334`)
- Body copy, every instance: **"work item"** — `WorkItemsPage.tsx:276` "New work item", `:1069` "Run a query to see work items", `:1077` "No work items matched", `WorkItemDetailPage.tsx:279` "Loading work item", `:304` "Work item unavailable", `:327` "Work items" (breadcrumb), `WorkItemsStep.tsx:355` `aria-label="Work items"`, `:449` "…add a work item directly or continue without work items.", `:499` "No work items matched.", `ReviewStep.tsx:166` `title="Work items"`
- Also **"Defect"** for the same objects created from a report (`dev-data/telecomPrompts.ts:74` "Provide defect drafts only for…"), and the README's "Jira defect creation".

**Recommend: "Ticket" everywhere user-visible** (it is the shortest, it matches the navigation, and it is what the operators' own tools call them). Changes: `WorkItemsPage.tsx:276,799,334,1069,1077`, `WorkItemDetailPage.tsx:279,304,327`, `WorkItemsStep.tsx:355,449,499`, `ReviewStep.tsx:166`, and `routes/router.tsx:142` `handle('Work Item')` → `handle('Ticket')`. Keep `work_items` as the API/route/folder name — the mismatch there is invisible.

### 3d. Applications / Environments / Apps & Environments

- Sidebar: `label: 'Applications'` (`Sidebar.tsx:85`) for `/environments`
- Route handles: `handle('Applications')` for `/environments` (`router.tsx:147`), `handle('Environment')` for `/environments/:id` (`router.tsx:152`)
- Page `<h2>`: **"Applications & environments"** (`EnvironmentsPage.tsx:459`)
- Home quick-link: **"Apps & Environments"** (`HomePage.tsx:356`)
- Detail breadcrumb: "Applications" (`EnvironmentDetailPage.tsx:402`)
- Empty state: "No applications yet" (`:528`), but the sibling empty state is "No environments configured." (`:558`)

Four names for one nav destination: "Applications", "Environment", "Applications & environments", "Apps & Environments". These *are* two nested entities (an application groups environments — `EnvironmentsPage.tsx:77` says so), so the page `<h2>` is the honest label and the sidebar is the lossy one.

**Recommend: "Applications" in the sidebar and both route handles, "Applications & environments" as the page heading, and never the abbreviation "Apps".** Change `HomePage.tsx:356` → "Applications"; `router.tsx:152` `handle('Environment')` → `handle('Application environment')`.

### 3e. Test Presets / Golden Configs — UI-only agreement

The UI is already consistent: every user-visible string says "Test preset" (`GoldenConfigsPage.tsx:62,78,86,92`, `GoldenConfigDetailPage.tsx:249,259`, `ConfigStep.tsx:260,279`, `ReviewStep.tsx:211`, `router.tsx:122,127`, `Sidebar.tsx:91`). "Golden config" appears **zero** times in user-visible copy.

The mismatch is the URL only: `routes/router.tsx:121` `path: 'golden-configs'` and `:126` `golden-configs/:assistantId`. That surfaces in the address bar, bookmarks, shared links and support transcripts.

**Recommend: rename the route to `/test-presets` in `routes/router.tsx:121,126`, with a redirect from `/golden-configs*`.** The feature folder and component names can stay; they are invisible. P3 on its own — folded here because it is the same axis.

### 3f. Knowledge & Documents / Context / Evidence

- Sidebar + route handle: "Knowledge & Documents" (`Sidebar.tsx:88`, `router.tsx:157`) for `/context`
- Page `<h2>`: "Knowledge & Documents" (`ContextPage.tsx:38`) — agrees
- But the tablist is `aria-label="Knowledge sections"` (`ContextPage.tsx:41`) and the caption is "Reusable source material for planning and running tests." (`:39`) — a third framing
- The wizard step that consumes it is "Context" (`features/new-test/steps/ContextStep.tsx`), and the API/route/folder is `context`
- `/evidence` splits out of the same feature folder (`router.tsx:162` → `features/context/pages`) under the name "Evidence"

So the user meets this data as "Knowledge & Documents" in the nav, "Context" in the wizard, and "Evidence" in Investigation — three names for one store, with no on-screen statement that they are related.

**Recommend: keep "Knowledge & Documents" and "Evidence" as the two nav entries (they are genuinely different views), but rename the wizard step from "Context" to "Knowledge" and have it say where the material comes from.** Change `features/new-test/steps/ContextStep.tsx` step label and `features/new-test/wizardState.ts`'s step id label; add a one-line link from the wizard step to `/context`.

---

<a id="con-04"></a>
## CON-04 — The 404 stylesheet globally redefines `.dash-empty`, and the 404 copy is wrong

**P2 · correctness, consistency · `*` (any unmatched URL), then every route**

### Evidence

`src/theme/primitives.css:318-330` defines the shared empty-state primitive:

```
.dash-empty { background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 24px; text-align: center;
  color: var(--text-secondary); font-size: 14px; display: flex; … }
```

`src/components/FeaturePlaceholder.css:7-13` **redefines the same unscoped selector**:

```
.dash-empty { display: flex; flex-direction: column; align-items: center;
  gap: 10px; text-align: center; color: var(--text-secondary); }
```

It is not nested under `.feature-placeholder`. Both selectors have specificity (0,1,0), so the later-injected rule wins. `FeaturePlaceholder.css` is imported as a side effect by `components/FeaturePlaceholder.tsx:1`, which is reached only from `features/not-found/pages.tsx:1` — a lazy route chunk, therefore injected *after* the entry `theme/index.css`.

**Expected:** visiting an unknown URL shows a 404 and changes nothing else.
**Actual:** the moment the not-found chunk loads, `.dash-empty` loses `background`, `border`, `border-radius`, `padding: 24px` and `font-size: 14px` **app-wide for the rest of the session** — injected stylesheets are never removed. `.dash-empty` is used at ~40 call sites across 30 modules (`features/home/HomePage.tsx:78,185,297`, `features/runs/RunsListPage.tsx:554`, `features/approvals/ApprovalsInboxPage.tsx:358,402,531`, `features/admin/ConnectionsPage.tsx:391,398,405`, `features/logs/LogsPage.tsx:290,330`, `features/analytics/AnalyticsPage.tsx:910`, and 25 more). Every one of them flattens from a bordered card to unstyled centred text. Two further rules leak the same way: `.dash-empty h2 { font-size: 20px }` and `.dash-empty p { max-width: 46ch }` (`FeaturePlaceholder.css:16-24`), neither of which exists in `primitives.css`.

Same root cause as CON-01, same reason no test catches it (`test: { css: false }`).

### Second defect: the 404 tells users the feature is unfinished

`features/not-found/pages.tsx:4` renders `<FeaturePlaceholder title="Not Found" route="*" />`, and `components/FeaturePlaceholder.tsx:11-16` renders:

> **Not Found** — "This screen is scaffolded and lands in a later dashboard milestone." — `*`

`FeaturePlaceholder` is the D0 scaffold stub (its own docstring says so: *"D0 stub rendered by every routed screen … Real screens land in D1+"*). Every screen has since been built, so the only remaining consumer is the 404. The result is that a user who mistypes a URL, follows a stale link, or hits a renamed route is told the page is coming in a future release, and is shown a literal `*` in a pill. There is no "go home" link, no "check the URL" guidance, and no indication that the URL is simply wrong.

**Third, minor:** `FeaturePlaceholder.css:26-28` hardcodes `font-family: ui-monospace, SFMono-Regular, Menlo, monospace` instead of `var(--font-mono)` — a tokens-only violation, and in `apex-light` the mono token is `'IBM Plex Mono'`, so the pill does not match any other monospace text on screen.

### Recommended change

1. Delete the `.dash-empty`, `.dash-empty h2` and `.dash-empty p` blocks from `components/FeaturePlaceholder.css` (lines 7-24). If the placeholder needs different metrics, scope them: `.feature-placeholder .dash-empty { … }`.
2. Replace `features/not-found/pages.tsx` with a purpose-built 404: heading "Page not found", body naming the attempted path from `useLocation()`, and a primary `<Link to="/">` back to Home plus a secondary link to `/runs`. Delete `components/FeaturePlaceholder.tsx` and its stylesheet once nothing imports them.
3. Change `FeaturePlaceholder.css:26` to `var(--font-mono)` if the file survives.
4. Turn on CSS in the Vitest config for a small style-regression suite (`vite.config.ts` `test.css`), or add a CI grep asserting that selectors defined in `theme/primitives.css` are not redefined unscoped anywhere under `src/components` or `src/features`. This one check would have caught CON-01 and CON-04 both.

---

<a id="con-05"></a>
## CON-05 — Empty, loading, error, not-found and unconfigured are not visually distinguishable

**P2 · consistency, ease of use · every route**

### The four-plus states and their treatments

`.dash-empty` — a bordered, centred card — is used for **four semantically different states**:

| State | Example | Treatment |
|---|---|---|
| Empty (nothing exists yet) | `features/home/HomePage.tsx:297` "No runs yet." | `.dash-empty small` |
| Empty (filtered to nothing) | `features/runs/RunsListPage.tsx:560` "No runs match the current filters." | `.dash-empty` |
| **Loading** | `features/apex-load/ApexLoadWorkspacePage.tsx:478` "Loading APEX Load connections…" | `.dash-empty` |
| **Not found** | `features/work-items/WorkItemDetailPage.tsx:292` "Item not found" | `.dash-empty` |
| **Permission denied** | `features/admin/adminShared.tsx:18` non-admin fallback | `.dash-empty` |
| **Gate disappeared** | `features/approvals/ApprovalsInboxPage.tsx:531` | `.dash-empty small` |

A user who lands on a slow `/apex-load` cannot tell "still loading" from "nothing configured" — same card, same centring, same muted text. A viewer-role user on `/admin/connections` gets the same card as a user whose filters matched nothing.

Errors are the one state that *is* distinct, via `components/ProblemCard.tsx` (left `3px solid var(--danger)` border, `role="alert"`, optional retry) — and it is well adopted at **42 call sites**. That is the model the other states should follow.

A **fifth** treatment exists for "unconfigured": `features/cycles/pages.tsx:54-58` renders a bare `.glass-panel .cycle-panel` with `<h1>`, `<p>` and an unstyled `<Link>` — not `.dash-empty`, not `ProblemCard`, not `FeaturePlaceholder`. The other three unconfigured states (`ExecutionWorkspace.tsx:55`, `SettingsPage.tsx:329`, `cycles/pages.tsx:315`) are inline prose with no container at all.

### Loading: 12 bespoke skeletons, 6 heights, 2 inverted pulse envelopes

There is no shared skeleton component. Twelve feature-local classes exist, with no two agreeing:

| Class | Height | Animation |
|---|---|---|
| `features/logs/logs.css:215` `.logs-skeleton` | 38 px | `logs-pulse 1.4s` |
| `features/context/context.css:690` `.ctx-skeleton` | 44 px | `pulse 1.8s` |
| `features/environments/environments.css:399` `.env-skeleton` | 44 px | `pulse 1.8s` |
| `features/admin/admin.css:569` `.adm-skeleton` | 44 px | `pulse 1.8s` |
| `features/prompts/prompts.css:305` `.prompts-skeleton` | 44 px | `pulse` |
| `features/runs/RunsListPage.css:323` `.runs-skeleton` | 44 px | `pulse 1.8s` |
| `features/work-items/work-items.css:374` `.wi-skeleton` | 44 px | `pulse 1.8s` |
| `features/compare/compare.css:187` `.compare-skeleton` | 64 px | `pulse 1.8s` |
| `features/home/home.css:223` `.home-skeleton` | 120 px min | `pulse 1.8s` |
| `features/golden-configs/golden-configs.css:104` `.gc-skeleton` | 132 px | `pulse 1.6s` |
| `features/runs/run-detail.css:162` `.run-detail-skeleton` | 320 px min | `pulse 2s` |
| `features/approvals/approvals.css:191` `.approvals-skeleton` | (grid only) | `pulse 1.6s` on `-block` |

Four durations (1.4 s, 1.6 s, 1.8 s, 2 s) and — more visibly — **two opposite opacity envelopes**:

- `theme/primitives.css:41` `@keyframes pulse`: `1 → 0.4 → 1` (starts bright, dims)
- `features/logs/logs.css:198` `@keyframes logs-pulse` and `features/analytics/analytics.css:259` `@keyframes analytics-pulse` (byte-identical to each other): `0.5 → 1 → 0.5` (starts dim, brightens)

So `/logs` and `/analytics` skeletons pulse out of phase with every other screen's. A fourth near-duplicate, `components/runs/PhaseStrip.css:253` `@keyframes phaseStripPulse` (`1 → 0.45 → 1`), differs from `pulse` by 0.05 opacity.

A **third** loading treatment is plain text with no skeleton and no container, concentrated in `cycles` and `apex-load`: `ProviderValidation.tsx:32`, `CycleResults.tsx:33`, `ScriptStorage.tsx:44`, `ExecutionWorkspace.tsx:65`, `cycles/pages.tsx:216,503`, `ApexLoadWorkspacePage.tsx:478`, `NewRunWizard.tsx:597`, `ReleaseMappingPicker.tsx:66`, `PromptsStep.tsx:177`. A **fourth** is `.gate-spinner` (`hitl/hitl.css`, 2 call sites).

Positive note: `aria-busy` and `aria-label="Loading …"` are applied consistently on ~22 of the skeleton containers, so the *announced* loading state is sound even where the visual one is not.

### Recommended change

1. Add `components/ux/StateCard.tsx` with an explicit `kind` prop — `'empty' | 'filtered' | 'loading' | 'not-found' | 'unauthorized' | 'unconfigured'` — each with a distinct icon and tone, and a required `action` slot. Give it one stylesheet in `src/theme/` so it cannot repeat CON-01.
2. Migrate the ~40 `.dash-empty` call sites to it, starting with the four mis-typed states: `ApexLoadWorkspacePage.tsx:478` (loading), `WorkItemDetailPage.tsx:292` (not-found), `adminShared.tsx:18` (unauthorized), `ApprovalsInboxPage.tsx:531` (stale).
3. Add `components/ux/Skeleton.tsx` with `rows`/`height` props and one `@keyframes` in `theme/primitives.css`. Delete the 12 feature skeleton classes and the 3 duplicate keyframes (`logs-pulse`, `analytics-pulse`, `phaseStripPulse`).
4. Replace the 10 plain-text "Loading …" strings in `cycles` and `apex-load` with `<Skeleton>`, so `/cycles` stops being the one feature with no loading shape.
5. Give `features/cycles/pages.tsx:54-58` the `unconfigured` variant of `StateCard`.

---

<a id="con-06"></a>
## CON-06 — One dialog shape reimplemented six times; `--modal-backdrop` has zero consumers

**P2 · consistency · `/admin/*`, `/environments`, `/context`, `/work-items*`, `/prompts*`, `/cycles*`, `/approvals`, `/runs*`**

`components/Dialog.tsx` is a genuinely good primitive — focus trap, Escape, backdrop click, an inert stack for nested dialogs, a `MutationObserver` for late-appended body children, body-scroll lock, focus restore. All **22** dialogs use it. The problem is everything it delegates: `Dialog` takes `className` and `overlayClassName` as *required* strings and owns no styling, so each feature invented its own.

### Ten overlay classes for one scrim

| Overlay class | Scrim | Defined at |
|---|---|---|
| `adm-overlay` | `rgba(0, 0, 0, 0.45)` | `features/admin/admin.css:493` |
| `env-overlay` | `rgba(0, 0, 0, 0.45)` | `features/environments/environments.css:241` |
| `ctx-overlay` | `rgba(0, 0, 0, 0.45)` | `features/context/context.css:644` |
| `wi-overlay` | `rgba(0, 0, 0, 0.45)` | `features/work-items/work-items.css:201` |
| `prompt-modal-overlay` | `rgba(0, 0, 0, 0.45)` | `features/prompts/prompts.css:314` |
| `approvals-overlay-backdrop` | `rgba(0, 0, 0, 0.35)` | `features/approvals/approvals.css:214` |
| `cycle-dialog-overlay` | `#0008` (= 53 %) | `features/cycles/cycles.css:134` |
| `navigation-overlay` | `var(--bg-overlay, var(--bg-secondary))` | `components/layout/AppShell.css:98` |
| `preflight-overlay` | — | `features/runs/preflight.css` |
| `launch-modal-overlay` | — | `features/runs/*` |

**`--modal-backdrop` is defined in all six themes** — `theme/tokens.css:62` (`rgba(0,0,0,0.75)`), `themes.css:67,139,218,293`, `apex-light.css:50` — and is consumed by **zero** components. Three of those theme values are *not black*: `solarized-dark` is `rgba(0,43,54,0.8)`, `solarized-light` is `rgba(253,246,227,0.8)` (a **light** scrim, by design), and `apex-light` is `rgba(26,26,46,0.24)`. So in `solarized-light` every dialog gets a hard black 45 % scrim where the theme intends a pale cream one, and in `apex-light` it gets 45 % black where the theme specifies 24 %. These seven literals are also the bulk of the app's tokens-only violations (the full list outside `src/theme/` is 10 lines: the 7 above plus `auth/ApiKeyGate.css:9`, `components/layout/AppShell.css:27` and `features/compare/compare.css:295`).

*Load answer: APEX Load is the style reference here; converging these on `var(--modal-backdrop)` is the one-line path to matching it. The full scrim comparison belongs to the Load reviewer.*

### Three z-index scales

`components/Dialog.tsx:193` sets the portal host to `zIndex: '1000'`. Inside it, `features/approvals/approvals.css:213` sets `z-index: 300` and `features/cycles/cycles.css:133` sets `z-index: 2000`. Those are scoped inside the portal so they do not actually conflict today, but they encode three incompatible mental models of the stacking order.

### Six parallel copies of the panel, caption, actions and error rows

The same four elements are reimplemented per feature. Call-site counts:

| Role | `admin` | `env` | `ctx` | `wi` | `prompt` | `cycle` |
|---|---|---|---|---|---|---|
| Panel | `adm-modal` | `env-modal` | `ctx-modal` | `wi-modal` | `prompt-modal` | `cycle-dialog` |
| Title | `adm-panel-title` | `env-panel-title` | `ctx-modal-title` | `wi-modal-title` (5) | `prompt-modal-title` (2) | — |
| Caption | `adm-modal-caption` (6) | `env-modal-caption` | `ctx-modal-caption` | `wi-modal-caption` (4) | `prompt-modal-caption` | — |
| Actions | `adm-panel-actions` (14) | `env-panel-actions` (4) | `ctx-actions` (2) | `wi-modal-actions` (5) | `prompt-modal-actions` (4) | `cycle-actions` (7) |
| Error | `adm-inline-error` (10) | `env-inline-error` (5) | `ctx-inline-error` (4) | `wi-inline-error` (9) | `tonal-card danger` | `tonal-card danger` |

The five inline-error rules are near-identical but not identical: `features/context/context.css:668`, `features/environments/environments.css:377` and `features/work-items/work-items.css:237` all set `display:flex; align-items:center; justify-content:space-between; gap:8px; padding:8px 12px; border:1px solid var(--danger-border); border-radius:8px`, while `features/admin/admin.css:240-248,261-265` omits `justify-content: space-between` — so an admin inline error's dismiss affordance sits beside the text instead of right-aligned. The prompts and cycles features use `.tonal-card danger` instead, inheriting CON-01.

### Recommended change

1. Replace the ten `overlayClassName` strings with one `.modal-overlay` rule in `src/theme/primitives.css` using `background: var(--modal-backdrop)`, and make `overlayClassName` optional in `components/Dialog.tsx` with that as the default. Delete the 7 hardcoded scrims.
2. Add `.modal-panel`, `.modal-title`, `.modal-caption`, `.modal-actions` and `.inline-error` / `.inline-ok` to `theme/primitives.css`; delete the 6 panel/title/caption families and the 5 inline-error families from the feature stylesheets. ~50 class references change; the markup shape is already identical, so the diff is mechanical.
3. Remove the `z-index` declarations from `features/approvals/approvals.css:213` and `features/cycles/cycles.css:133` — `Dialog`'s portal already owns stacking.
4. Replace `var(--bg-overlay, …)` at `components/layout/AppShell.css:98` with `var(--modal-backdrop)`; `--bg-overlay` is defined in no theme (see CON-09).

---

<a id="con-07"></a>
## CON-07 — Destructive-confirm strength and button variant are arbitrary

**P2 · consistency, correctness · `/admin/*`, `/environments`, `/context`, `/work-items*`, `/prompts*`, `/runs*`, `/settings`**

### Three confirm strengths, two type-to-confirm conventions

| Action | Confirm strength | Evidence |
|---|---|---|
| Abort a running test | **Type the literal word `ABORT`** | `hitl/GateActionBar.tsx:48` `text.trim().toUpperCase() === 'ABORT'`, placeholder `"Type ABORT"` (`:56`) |
| Delete a connection | **Type the resource's name** | `features/admin/ConnectionDetailPage.tsx:524-531`, `aria-label="Type the connection name to confirm"` |
| Delete an environment | **Type the resource's name** | `features/environments/EnvironmentsPage.tsx:388-398` |
| Delete a consumer | Plain two-button dialog | `features/admin/ConsumersPage.tsx:436-468` |
| Delete a document | Plain two-button dialog | `features/context/DocumentsTab.tsx:34-77` |
| Delete a saved query | Plain two-button dialog | `features/work-items/SavedQueriesPage.tsx:226-262` |
| Archive a prompt | Plain button, no dialog | `features/prompts/PromptDetailPage.tsx:329-332` |
| Remove a host / a scope | Plain button, no dialog | `features/environments/environmentsForm.tsx:45`, `features/admin/ConsumersPage.tsx:123` |

`grep -rn "toUpperCase() ===\|confirmText\|requireText"` returns exactly one hit in the whole app: `GateActionBar.tsx:48`. So type-to-confirm exists in three places under two conventions (a fixed keyword vs. the resource name), and the hardest gate is on *abort*, which is recoverable, while **delete consumer** — which `ConsumersPage.tsx:443` itself describes as "permanently removes **{name}** and revokes its key", i.e. it locks out an API client — has no typed confirmation at all.

### `btn-danger` is used 5 times; 5 destructive actions use `btn-ghost`

`btn-danger` call sites: `GateActionBar.tsx:34` (Abort), `:64` (Confirm abort), `ConnectionDetailPage.tsx:692` (Delete), `ApexLoadWorkspacePage.tsx:794` (Reject), `SettingsPage.tsx:231` (**Sign out**).

Destructive actions styled `btn-ghost` instead: `features/context/DocumentsTab.tsx:376` "Delete…", `features/environments/EnvironmentsPage.tsx:187` "Delete…", `features/admin/ConsumersPage.tsx:525` "Delete…", `features/work-items/SavedQueriesPage.tsx:328` "Delete…", `features/environments/environmentsForm.tsx:45` "Remove", `features/admin/ConsumersPage.tsx:123` "Remove", `features/compare/ComparePage.tsx:164` "✕".

Conversely **"Sign out" is the only `btn-danger` in Settings** and it is not destructive — and the same action in the sidebar (`components/layout/Sidebar.tsx:331-334`) uses `sidebar-identity-action sidebar-identity-action-button`, not a `.btn` at all. One action, two looks, one of them flagged as dangerous.

### Confirm-dialog button order is inconsistent

| Dialog | Order |
|---|---|
| All 6 delete dialogs | `[Cancel btn-ghost btn-sm]` then `[Delete X btn-danger btn-sm]` ✅ consistent |
| `features/prompts/RollbackConfirm.tsx:54-59` | `[Cancel btn-ghost]` then `[Set vN active btn-primary]` — no `btn-sm`, unlike all six deletes |
| `hitl/GateActionBar.tsx:52-76` (AbortConfirm) | `[input]` then `[Confirm abort btn-danger]` then **`[Cancel btn-ghost]` last** — the only dialog where Cancel is on the right |
| `GateActionBar.tsx:129-220` | Primary first, danger last, with a `.gate-actions-spacer` between |

### Icons and state verbs inside gate buttons

`hitl/GateActionBar.tsx` is the only place in the app that puts glyphs in button labels: `'▶ Execute Phase'` (`:149`) and `'↻ Re-run with Changes'` (`:181`). The same primary button swaps both its glyph and its verb on `dirty` — `'▶ Execute Phase'` ↔ `'Save Edit & Re-review'` — so the operator's muscle memory for the primary action's position does not carry a stable label.

### Where errors appear: this part is good

There are **no toasts anywhere** in the app, and four file headers state this is deliberate (`ConnectionDetailPage.tsx:7` "inline result panel, deliberately NOT a toast", `EnvironmentDetailPage.tsx:9`, `ConsumersPage.tsx:8`, `ConnectionsPage.tsx:7`). The split is coherent: route-level failure → `ProblemCard` (42 sites); in-form/in-dialog failure → inline error with `role="alert"`; stale-but-usable data → `CachedDataWarning`. That is a sound three-tier model and should be written into the contract.

### Recommended change

1. Extract `components/ux/ConfirmDialog.tsx` taking `{ title, body, confirmLabel, tone, requireTypedConfirmation?: string }`. Migrate all 6 delete dialogs plus `RollbackConfirm` to it, fixing the button-size inconsistency in passing.
2. Pick one type-to-confirm convention — **the resource's own name** — and apply it by blast radius: add it to *delete consumer* (`features/admin/ConsumersPage.tsx:436`), keep it on connection and environment delete, and leave it off document and saved-query delete. Change `hitl/GateActionBar.tsx:48,56,57` from the literal `ABORT` to the test's title for consistency, or document the keyword as a deliberate exception in the contract's Dialogs row.
3. Promote the four `Delete…` row triggers to `btn-danger btn-sm` (`DocumentsTab.tsx:376`, `EnvironmentsPage.tsx:187`, `ConsumersPage.tsx:525`, `SavedQueriesPage.tsx:328`) and add the ellipsis to `ConnectionDetailPage.tsx:692` so "opens a confirm" reads the same everywhere.
4. Change `features/settings/SettingsPage.tsx:231` from `btn-danger` to `btn-secondary`, and make `components/layout/Sidebar.tsx:331` use `btn btn-secondary btn-sm` so the two sign-outs match.
5. Reorder `hitl/GateActionBar.tsx`'s `AbortConfirm` to `[Cancel][Confirm abort]`, and drop the `▶`/`↻` glyphs at `:149,181`.

---

<a id="con-08"></a>
## CON-08 — Seven timestamp formatters, five renderings, and no timezone anywhere

**P2 · consistency, correctness · every route**

### The formatters

| # | Function | Output | Location |
|---|---|---|---|
| 1 | `formatTimestamp` | `Oct 17, 01:09:24 PM` — zero-padded 12-hour, seconds, **no year** | `features/runs/runDisplay.ts:220-230` |
| 2 | `formatRelative` | `just now` / `5m ago` / `3h ago` / `2d ago`, then `Oct 17` past a week | `utils/time.ts:10-33` |
| 3 | `formatTimestamp` (second, unrelated) | `Oct 17, 1:09 PM` — **not** zero-padded, no seconds | `features/context/EvidenceTab.tsx:40-45` |
| 4 | `formatLogTime` | `2026-10-17 13:09:24` — ISO-ish, **24-hour** | `features/logs/LogsPage.tsx:39-43` |
| 5 | `bucketTick` | `13:09` (hour bucket) or `Oct 17` (day bucket) | `features/analytics/AnalyticsPage.tsx:81-88` |
| 6 | bare `new Date(x).toLocaleString()` | `10/17/2026, 1:09:24 PM` — full locale, **with year** | 14 call sites, below |
| 7 | bare `.toLocaleTimeString()` | `1:09:24 PM` — time only | `features/approvals/ApprovalsInboxPage.tsx:341` |

Formatter 6's 14 call sites: `features/cycles/ProviderValidation.tsx:95,96`, `ExecutionWorkspace.tsx:86,329,331,377`, `cycles/pages.tsx:236,310`, `ProviderScriptImport.tsx:311`, `CycleResults.tsx:45,94,96`, `ScriptStorage.tsx:71`, `features/runs/RunOverview.tsx:139`, `features/new-test/NewRunWizard.tsx:581`.

So the same instant reads five different ways depending on the screen:

- `/runs` grid, Created column → **`Oct 17, 01:09:24 PM`**
- `/runs` grid, Updated / `/work-items` / `/prompts` / `/context` → **`24m ago`**
- `/evidence` detail → **`Oct 17, 1:09 PM`**
- `/logs` → **`2026-10-17 13:09:24`**
- `/cycles` → **`10/17/2026, 1:09:24 PM`**

Formatter 1's zero-padded 12-hour clock (`hour: '2-digit'` with `hour12` left to the locale) is the specific oddity the brief flagged: `01:09:24 PM` is a hybrid nobody writes by hand, and it sits in the same table row as a relative "24m ago", so one row shows two unrelated time vocabularies.

### No timestamp states its timezone

`grep -rn -i "timeZone\|timeZoneName\|UTC\|hour12\|getTimezoneOffset"` over `src/` returns **zero** timezone-related hits outside unrelated word matches. Every timestamp renders in the browser's local zone with no indicator and no option. For a tool whose job is correlating orchestration events with provider execution logs and ELK observations across regions, an unlabelled local time is a correctness risk, not a cosmetic one: two operators in different zones reading the same run will report different incident times.

Ten sites partially mitigate by putting the raw ISO string in a `title=` attribute: `features/home/HomePage.tsx:117`, `features/context/DocumentsTab.tsx:369`, `features/environments/EnvironmentsPage.tsx:171`, `features/admin/ConsumerDetailPage.tsx:83`, `features/admin/ConnectionsPage.tsx:89`, `features/prompts/PromptsPage.tsx:125,174`, `features/runs/RunsListPage.tsx:167`, `features/approvals/ApprovalsQueue.tsx:80`, `features/work-items/SavedQueriesPage.tsx:301`. That helps a mouse user, but `title` is not reachable by keyboard or touch, it shows an unformatted `2026-10-17T13:09:24.123456+00:00`, and the other 23+ timestamp renderings have no tooltip at all.

### Durations, bytes and other units

`formatDuration` (`features/runs/runDisplay.ts:202-218`) is the single duration formatter and is used consistently — `850ms` / `4.2s` / `1m 15s` / `1h 4m`. Good.

`formatBytes` exists **three times, byte-for-byte identical**: `features/context/contextLogic.ts:13-17` (exported), `features/artifacts/ArtifactViewerPage.tsx:55-59` (private), `features/new-test/steps/ContextStep.tsx:21-25` (private). Output is the same, so there is no user-visible divergence — but `features/cycles/ScriptStorage.tsx:61` bypasses all three and renders `{artifact.size_bytes.toLocaleString()} bytes`, so a 1.2 MB script shows as `1,258,291 bytes` where a 1.2 MB document shows as `1.2 MB`.

### Recommended change

1. Create `src/utils/datetime.ts` as the single source, exporting exactly three functions: `formatAbsolute(iso)` → `Oct 17, 2026, 13:09` (24-hour, no seconds, year included), `formatPrecise(iso)` → `2026-10-17 13:09:24 UTC+1` for logs and audit trails, and the existing `formatRelative`.
2. Delete formatters 1, 3, 4, 5 and replace all 14 bare `toLocaleString()` calls and the one `toLocaleTimeString()` call with them. Files: `features/runs/runDisplay.ts:220`, `features/context/EvidenceTab.tsx:40`, `features/logs/LogsPage.tsx:39`, `features/analytics/AnalyticsPage.tsx:81`, `features/approvals/ApprovalsInboxPage.tsx:341`, plus the 14 sites listed above.
3. Append the resolved zone abbreviation from `Intl.DateTimeFormat().resolvedOptions().timeZone` to `formatAbsolute` and `formatPrecise`, and state the zone once per page in the Topbar or on the Settings "Personal preferences" tab. Add a "Show times in UTC" preference to `theme/preferences.ts` alongside `rows` and `reduceMotion` — this is exactly the kind of personal preference the contract's "Personal preferences" row already covers.
4. Replace the two private `formatBytes` copies with an import from `features/context/contextLogic.ts` (or move it to `src/utils/`), and use it at `features/cycles/ScriptStorage.tsx:61`.

---

<a id="con-09"></a>
## CON-09 — Four tokens are consumed without a usable fallback but defined in one theme or none

**P2 · correctness, consistency · all six themes**

I extracted every `--token:` declaration from every `:root` / `:root[data-theme=…]` block across `theme/tokens.css`, `themes.css`, `apex-light.css`, `accessibility.css`, `ux.css`, `primitives.css` and `index.css`, and cross-referenced every `var(--token)` reference in `src/**/*.css` and `src/**/*.tsx`. The base `:root` block defines 72 tokens; the five `[data-theme]` blocks override subsets of them, which is correct — an un-overridden token correctly inherits the `:root` value, and because the values use `var()` indirection (e.g. `--run-calendar-cron-bg: color-mix(in srgb, var(--success) 11%, var(--bg-secondary))`) they re-resolve per theme. Those are fine.

The real gaps are tokens that appear in **no** `:root` block:

| Token | Defined in | Consumed at | Fallback? | Effect |
|---|---|---|---|---|
| `--accent-results` | **`apex-light` only** (`theme/apex-light.css:21`) | `features/home/home.css:66`, `components/runs/PhaseStrip.css:146,147,148` | **none** | In the other **5 themes**: `border-top-color: var(--accent-results)` → invalid at computed-value time → falls back to `currentColor`; `color-mix(in srgb, var(--accent-results) 32%, var(--border))` → the whole declaration is dropped. The PhaseStrip "results ready" phase and Home's results card lose their distinguishing colour entirely in `dark`, `light`, `solarized-dark`, `solarized-light` and `monokai-dimmed`. |
| `--subtle-fill` | **`apex-light` only** (`theme/apex-light.css:52`) | 9 sites | 7 have one | `features/context/context.css:600` has **no fallback** → background drops to `unset` in 5 themes. The other 7 (`theme/primitives.css:220,434`, `wizard.css:163`, `prompt-tabs-editor.css:10`, `run-detail.css:188,361`, `hitl/hitl.css:80`) correctly supply `var(--bg-secondary)` / `transparent`, and `primitives.css:9-13` documents this as intentional parity with Load. |
| `--bg-app` | **nowhere** | `features/new-test/wizard.css:447` | **none** | `background: var(--bg-app)` is dead in **all six** themes — the element renders transparent. |
| `--shadow-lg` | **nowhere** | `features/admin/admin.css:590` | **none** | `box-shadow: var(--shadow-lg)` is dead in all six themes. Only `--shadow-sm` and `--shadow-md` exist. |
| `--bg-overlay` | nowhere | `components/layout/AppShell.css:98` | `var(--bg-secondary)` | Harmless, but see CON-06 — should be `--modal-backdrop`. |
| `--text-tertiary` | nowhere | `features/work-items/work-items.css:331` | `var(--text-secondary)` | Harmless; remove the dead reference. |
| `--panel-border` | nowhere | `features/context/context.css:148` | **`rgba(255,255,255,0.12)`** | A literal white fallback, so it is both a tokens-only violation and wrong in the three light themes, where a 12 %-white border on a white card is invisible. |

Secondary, informational: `--run-calendar-*` (6 tokens) are overridden with hardcoded `rgba()` literals in `solarized-dark` (`themes.css:148-153`) and with hex in `apex-light` (`apex-light.css:76-81`), while `light`, `solarized-light` and `monokai-dimmed` inherit the `:root` `color-mix()` form. That mixed strategy works but means two themes must be hand-maintained when the base formula changes.

### Recommended change

1. Add `--accent-results` to `theme/tokens.css` (`:root`) and to each of the four `themes.css` blocks. `--chart-5` is the natural per-theme value — `apex-light` already sets both to `#6f42c1`.
2. Add `--subtle-fill` to `:root` in `theme/tokens.css` as `rgba(255,255,255,0.02)` (and per-theme equivalents), or give `features/context/context.css:600` the same `var(--subtle-fill, var(--bg-secondary))` fallback the other seven sites use.
3. Delete the dead references: `features/new-test/wizard.css:447` → `var(--bg-primary)`; `features/admin/admin.css:590` → `var(--shadow-md)`; `features/work-items/work-items.css:331` → `var(--text-muted)`; `features/context/context.css:148` → `1px solid var(--border)`.
4. Add the token-coverage check to CI. The script I used is ~40 lines: parse `--x:` declarations per `:root` block, parse `var(--x` references across `src/`, and fail when a referenced token is absent from `:root` and lacks a fallback. That is the only automated guard against a theme silently falling back.

---

<a id="con-10"></a>
## CON-10 — README and code disagree on the themes, and every cold load animates dark→light

**P2 · correctness · all routes**

### Documentation drift

`apps/dashboard/README.md` ("Theming") states *"Five themes, switched via `data-theme`"* and tabulates `dark` *(default)*, `light`, `solarized-dark`, `solarized-light`, `monokai-dimmed`. The architecture map in the same file says `theme/ — design tokens + 5 themes (see Theming)`, and `docs/adr/0006-dashboard-architecture.md` says *"APEX Load's CSS custom-property token system ported verbatim (5 themes, tokens-only component CSS)"*.

The code ships **six**, and the default is not `dark`:

- `src/theme/useTheme.ts:17-24` — `THEMES = ['apex-light', 'dark', 'light', 'solarized-dark', 'solarized-light', 'monokai-dimmed']`
- `src/theme/useTheme.ts:32` — `export const DEFAULT_THEME: ThemeName = 'apex-light'`
- `src/theme/index.css:16` — `@import './apex-light.css'`

The sixth theme is the contract's documented domain exception (*"Orchestration retains … its Light (CTx) compatibility theme"*), so its existence is correct — the documentation is simply three places out of date, and a reader following the README would expect a dark app and find a light one.

A related labelling issue: `THEME_LABELS` (`useTheme.ts:35-42`) maps `apex-light` → `"Light (CTx)"` and `light` → `"Light"`. The theme `<select>` in `components/layout/Sidebar.tsx:310-327` and the Settings cards therefore offer two adjacent options both beginning "Light", distinguished only by a parenthetical acronym no on-screen text explains.

### Theme and reduce-motion are applied after a network round-trip

`index.html` sets no `data-theme` on `<html>`. `src/main.tsx:16-17` **awaits `loadRuntimeConfig()`** — a `fetch('/config.json')` — before `createRoot(...).render(...)`. `applyTheme` then runs in a `useEffect` (`useTheme.ts:88-90`), as does `document.documentElement.dataset.reduceMotion = …` (`useTheme.ts:73-76`).

So the sequence on every cold load is:

1. `theme/index.css` applies → `:root` has no `data-theme` → the **dark OLED** token set is live, and `tokens.css:111-113` paints `body { background: var(--bg-primary) }` = `#050508`.
2. The browser blocks on `fetch('/config.json')`.
3. React mounts; the effect sets `data-theme="apex-light"` → `#f5f7fa`.
4. `tokens.css:124` — `body { transition: background-color var(--transition-fast), color var(--transition-fast) }` — **animates** that jump over 150 ms.

The default-themed user therefore sees a black screen for the duration of a network request, then a 150 ms fade to near-white. And because `data-reduce-motion` is also only set in step 3, a user who asked for reduced motion still gets the fade (the `@media (prefers-reduced-motion: reduce)` rule at `theme/accessibility.css:89-91` covers the OS-level preference, but not the app-level one at the moment it matters most).

### Recommended change

1. Update `apps/dashboard/README.md` ("Theming" table, the `theme/` line in the architecture map) and `docs/adr/0006-dashboard-architecture.md` to say six themes with `apex-light` as the default, and note why `apex-light` exists with a pointer to the contract's domain exception.
2. Add a tiny blocking inline script to `index.html` (before the stylesheet) that reads `localStorage['apex.theme']` and `localStorage['apex.preferences.v1']` and sets `data-theme` and `data-reduce-motion` on `<html>` — the standard no-flash pattern. `getStoredTheme()` (`useTheme.ts:36-43`) and `readPreferences()` (`theme/preferences.ts:62`) already encapsulate the parsing, so the script is ~6 lines and `DEFAULT_THEME` stays the single source of truth.
3. Alternatively or additionally, move `loadRuntimeConfig()` off the critical path so `createRoot` is not gated on a network request.
4. Rename the two "Light" labels in `useTheme.ts:36-37` to something a user can tell apart — e.g. `apex-light` → `"Light (CTx)"` stays, `light` → `"Light (Pearl)"` matching the name already in `themes.css:14`.

---

<a id="con-11"></a>
## CON-11 — Each contract "Library control" reaches exactly one table

**P2 · consistency, ease of use · 18 table routes**

The contract's Library-controls row specifies *"Search, status/project filters, active-filter indicator, comparison guidance, named overflow actions and Rows / Previous / Next."* There are 18 modules rendering `.data-table`. Adoption:

| Control | Adopted by | Count |
|---|---|---|
| `.ux-filters` filter affordance (`theme/ux.css:134-143`) | `features/runs/RunsListPage.tsx:542` only | **1 / 18** |
| Active-filter indicator | `RunsListPage.tsx:542` (`Filters · Active`) only | **1 / 18** |
| Rows-per-page control | `RunsListPage.tsx:469` only | **1 / 5** paginated |
| Sortable column headers | `features/analytics/AnalyticsPage.tsx:596-597` only | **1 / 18** |
| `Previous` / `Next` | runs, logs, analytics, work-items, cycles (+ `ProviderScriptImport`) | 5 / 18 |
| `OverflowMenu` row actions | runs, environments, consumers, saved-queries | 4 / 18 |

Everything else hand-rolls. `features/admin/ConnectionsPage.tsx:357` has `.adm-registry-search`; `features/logs/LogsPage.tsx:211,227` has `.logs-toolbar-row`; `features/environments/EnvironmentsPage.tsx:462` has `.env-toolbar-actions`; `features/context/DocumentsTab.tsx:233-257` has inline label/select pairs. `features/context/EvidenceTab.tsx:119` *computes* `activeFilterCount` but only uses it to enable a Clear button (`:172`) and to swap the empty-state copy (`:188`) — it never marks the filter affordance as active. `features/analytics/AnalyticsPage.tsx:857` has a "Clear filters" button with no indicator.

### The contract's pagination clause does hold

> *"Specialized Orchestration tables retain their existing pagination limits; the personal row default applies to Test History."*

Verified correct. `theme/preferences.ts:6` declares `rows: 10 | 25 | 50 | 100` with default `10`; `features/settings/SettingsPage.tsx:325` is the only writer and its help text is accurate (*"Applies to Test History unless its Rows control overrides this value. Specialized tables retain their own limits."*). `features/runs/RunsListPage.tsx:278` and `:390` are the only readers, and they correctly let an explicit `?limit=` in the URL win over the preference. The specialized limits are indeed retained and are per-table constants: `features/logs/logsFilters.ts:11` `LOGS_PAGE_SIZE = 50`, `features/cycles/api.ts:36,245` `limit: 20`, `features/apex-load/ApexLoadWorkspacePage.tsx:120` `limit: 25`, `features/context/EvidenceTab.tsx:74` `limit: 100`, `features/home/homeLogic.ts:15,20,22,24,26` (50/5/5/8/5). **This clause passes.**

### Cycles pagination diverges on every axis

Four of the five paginated tables share a shape: `<footer class="X-pagination">` + `<span class="X-pagination-caption">` showing an item range + two `.btn.btn-ghost.btn-sm` buttons — `RunsListPage.tsx:466-490`, `LogsPage.tsx:181-203`, `AnalyticsPage.tsx:642-663`, `WorkItemsPage.tsx:1098-1125`. The fifth, `features/cycles/pages.tsx:248-263`, differs on all of them:

- `<div class="cycle-actions">`, not a `<footer class="…-pagination">`
- `.btn.btn-secondary`, not `.btn.btn-ghost.btn-sm` — so the pager is visually heavier than the page's own actions
- `<span>Page {offset / 20 + 1}</span>` — a page *number*, where every other table shows an item range (`1–50 of 312 entries`)
- no `type="button"` on either button (`:249`, `:257`)
- `disabled={query.data?.length !== 20}` on Next (`:257`) — this disables Next whenever the server returns a short page, which also silently hides the *existence* of further pages if the API ever returns fewer than the requested 20 for a non-final page
- `20` is written as a literal four times (`:252`, `:256`, `:260`, and the request at `features/cycles/api.ts:36`) rather than a named constant like `LOGS_PAGE_SIZE`

`features/cycles/ProviderScriptImport.tsx:118-139` repeats the same `Page {Math.floor(offset / 20) + 1}` shape with the same four literals.

### Row actions split three ways

- `OverflowMenu` (`⋯`, `role="menu"`, Escape/outside-click/arrow keys): `RunsListPage.tsx:205`, `EnvironmentsPage.tsx:175`, `ConsumersPage.tsx:515`, `SavedQueriesPage.tsx:305`
- A single inline `btn-ghost btn-sm`: `features/context/DocumentsTab.tsx:374-381` ("Delete…")
- No actions at all, just an external link: `features/work-items/WorkItemsPage.tsx:395-397`

**`OverflowMenu` is exported from `features/runs/PreflightModal.tsx:222`** and imported by three other features (`features/environments/EnvironmentsPage.tsx:29`, `features/admin/ConsumersPage.tsx:34`, `features/work-items/SavedQueriesPage.tsx:25`). `PreflightModal.tsx:13-26` imports `useThreadState`, `useRerun`, `assessPlan`, `Dialog`, `CachedDataWarning` and `./preflight.css` (302 lines), so `/environments`, `/admin/consumers` and `/work-items/saved` each pull the entire runs preflight machinery and leak `preflight.css` into their route just to render a `⋯` button. ADR-0006 puts shared controls in `components/`; this is a shared control living in a feature's modal file.

### Recommended change

1. Extract `components/ux/TableToolbar.tsx` (search + filter `<details>` + active-filter badge + Clear) and `components/ux/TablePager.tsx` (range caption + optional Rows + Previous/Next), styled from `src/theme/`. Migrate the 18 table modules; start with the five paginated ones so `cycles` stops being an outlier.
2. Move `OverflowMenu` out of `features/runs/PreflightModal.tsx` into `components/ux/OverflowMenu.tsx` with its own stylesheet, and update the four importers. This removes the preflight dependency from three unrelated chunks.
3. Give `features/cycles/pages.tsx:248-263` and `features/cycles/ProviderScriptImport.tsx:118-139` the shared pager; add `CYCLES_PAGE_SIZE = 20` to `features/cycles/api.ts` and use it in all five places; base Next's disabled state on a `total` from the API rather than `length !== 20`.
4. Add `aria-sort` + header buttons to Test History (`features/runs/RunsListPage.tsx:655-672`) — it is the app's workhorse table and the only one where column sorting would be used daily — copying the pattern from `AnalyticsPage.tsx:596-597`. Either extend it to the other tables or make the Analytics table's sortability visually obvious as the exception.
5. Surface the active-filter state on `/evidence`: `features/context/EvidenceTab.tsx` already has `activeFilterCount` at `:119`, so this is a one-line label change.

---

<a id="con-12"></a>
## CON-12 — The same metric renders at two different precisions

**P3 · consistency · `/`, `/analytics`, `/runs/:threadId`, `/runs/compare`**

Error rate — one metric, one meaning — is formatted at two precisions in five places:

| Precision | Location | Renders |
|---|---|---|
| **1 dp** | `features/home/homeLogic.ts:88` — `((totals.errors / totals.events) * 100).toFixed(1)` | `2.3%` |
| **1 dp** | `features/analytics/analyticsFormat.ts:21` — `formatPercent`, `.toFixed(1)` | `2.3%` |
| **2 dp** | `features/runs/EngineStrip.tsx:51` — `(v * 100).toFixed(2)` | `2.34%` |
| **2 dp** | `features/runs/PhaseWorkspace.tsx:148` — `(kpis['error_rate'] * 100).toFixed(2)` | `2.34%` |
| **2 dp** | `features/runs/ActivityFeed.tsx:107` — `(stats.error_rate * 100).toFixed(2)` | `2.34%` |
| **2 dp** | `features/compare/compareModel.ts:89` — `(v * 100).toFixed(2)` | `2.34%` |

Home reports a run's error rate as `2.3%`; opening that same run's detail page reports `2.34%`. Three further precisions are in use for other percentages: 0 dp (`EngineStrip.tsx:97` progress, `ActivityFeed.tsx:98`, `WorkItemsStep.tsx:462` and `WorkItemsPage.tsx:933` confidence) and raw passthrough (`features/runs/RunOverview.tsx:139` — `${summary.outcome.evidence.coverage_percent}%` with no formatting at all, so whatever the API sends is printed verbatim).

### Percentage points vs percent — this part is correct

`features/compare/compareModel.ts:137` gets it right: a delta on `error_rate` is rendered as `"… percentage points"` while deltas on other measures use the measure's own formatter, and `:135` separately reports the relative change as a `%`. That is the correct distinction and the only place in the app that needed it. **This passes.**

### Recommended change

Add `formatRate(value)` (2 dp, for error rates) and `formatPercent(value)` (1 dp, for shares and coverage) to a shared `src/utils/number.ts`, and use them at the six error-rate sites plus `RunOverview.tsx:139`. 2 dp is the right default for error rates — at the scale of a load test, the difference between 0.04 % and 0.00 % is the finding. Also move `features/analytics/analyticsFormat.ts`'s `formatTokens` / `formatCost` / `formatLatency` there, since `features/cycles/CycleResults.tsx:6` has independently rolled its own `Intl.NumberFormat(undefined, { maximumSignificantDigits: 6 })`.

---

<a id="con-13"></a>
## CON-13 — Test History has two columns headed "Run", and one plural header over a single value

**P3 · correctness, accessibility · `/runs`**

`features/runs/RunsListPage.tsx:655-672` renders the Test History header row:

```
663:  <th>Run</th>              ← the run title + id + phase
664:  <th>Project</th>
665:  <th>Applications</th>     ← plural
666:  <th>Progress</th>
667:  <th>Created</th>
668:  <th>Duration</th>
669:  <th>Test result</th>
670:  <th>Status</th>
671:  <th className="runs-actions-cell">Run</th>   ← the actions column
```

Two problems:

1. **Two columns are headed "Run"** (`:663` and `:671`). A screen-reader user navigating the table by column hears "Run" for both the identity column and the actions column, and `:671`'s cell contains an "Open run" button plus an overflow menu — it is an actions column, not a second identity column. (Confirmed independently; cited here because the fix belongs with the header row.)
2. **`<th>Applications</th>` (plural) sits over a single-valued cell.** `features/runs/RunsListPage.tsx:161` renders `<span className="runs-application-cell">{run.app_id ?? EM_DASH}</span>` — one app id. The card variant of the same data (`:128`) calls it `<dt>Application</dt>`, singular. So the same field is labelled "Applications" in the table and "Application" in the card, and the plural implies a list the cell cannot contain.

### Recommended change

In `features/runs/RunsListPage.tsx`: change `:671` to `<th className="runs-actions-cell"><span className="sr-only">Actions</span></th>` (matching the pattern already used at `:660` for the compare-select column), and change `:665` to `<th>Application</th>`.

---

<a id="con-14"></a>
## CON-14 — Page-heading practice splits three ways, and `cycles` emits a second `<h1>`

**P3 · consistency, accessibility · all routes, `/cycles*` specifically**

`components/layout/Topbar.tsx:22-24` is the documented page-heading authority (the contract's Shell row: *"Existing Sidebar, Topbar and TopbarContributionProvider remain the page-heading authority"*). It renders `<h1 className="topbar-page-title">` from the deepest route handle. Below it, pages take three different approaches:

**(a) No page heading** — the Topbar `<h1>` is the only one. `/` , `/runs`, `/approvals`, `/settings`, `/analytics`, `/logs`. Correct per the contract.

**(b) An `<h2 className="*-page-title">` that duplicates or diverges from the Topbar:**

| Route | Topbar `<h1>` (route handle) | Page `<h2>` |
|---|---|---|
| `/context` | "Knowledge & Documents" | "Knowledge & Documents" (`ContextPage.tsx:38`) — **duplicate** |
| `/evidence` | "Evidence" | "Evidence" (`EvidencePage.tsx:10`) — **duplicate** |
| `/golden-configs` | "Test Presets" | "Test presets" (`GoldenConfigsPage.tsx:62`) — case differs |
| `/environments` | "Applications" | "Applications & environments" (`EnvironmentsPage.tsx:459`) |
| `/admin/connections` | "Connections" | "Connection registry" (`ConnectionsPage.tsx:319`) |
| `/admin/consumers` | "Consumers" | "API consumers" (`ConsumersPage.tsx:561`) |
| `/apex-load` | "APEX Load" | "APEX Load workspace" (`ApexLoadWorkspacePage.tsx:514`) |
| `/work-items` | "Tickets" | "Tickets" (`WorkItemsPage.tsx:790`) — **duplicate** |

**(c) A second `<h1>`** — only `features/cycles`: `pages.tsx:55` ("Test Cycles"), `:187` ("Test Cycles"), `:527` (`{cycle.name}`). Each produces two `<h1>` elements in one document, and `:55`/`:187` duplicate the Topbar's text verbatim.

Three duplicated titles say the same thing twice on screen; five divergent ones mean the browser tab, the breadcrumb trail and the page body disagree about what the user is looking at.

### Recommended change

1. Change `features/cycles/pages.tsx:55,187,527` from `<h1>` to `<h2 className="cycle-page-title">`, matching the other 8 features.
2. Delete the three duplicate headings (`ContextPage.tsx:38`, `EvidencePage.tsx:10`, `WorkItemsPage.tsx:790`) and let the Topbar be the title, as on `/runs` and `/approvals`.
3. For the five divergent ones, make the route handle carry the fuller label and delete the page `<h2>` — `routes/router.tsx:147` `'Applications'` → `'Applications & environments'`, `:182` `'Connections'` → `'Connection registry'`, `:192` `'Consumers'` → `'API consumers'`, `:177` `'APEX Load'` → `'APEX Load workspace'`, `:122` `'Test Presets'` → keep and fix the page's casing.
4. Add a line to the contract's Shell row stating that a routed screen must not render its own `<h1>` or repeat the handle text, so this does not drift again.

---

<a id="con-15"></a>
## CON-15 — Button copy: mixed casing, two labels for one action, glyphs in labels

**P3 · consistency · multiple routes**

I extracted all 197 `.btn` call sites with their labels and variants. The New→Create verb pairing is actually sound — 5 entities use "New X" for the trigger and "Create X" for the dialog submit (`ConnectionsPage.tsx:325`/`:285`, `ConsumersPage.tsx:565`/`:239`, `WorkItemsPage.tsx:799`/`:334`, `PromptsPage.tsx:423`/`:319`, `EnvironmentsPage.tsx:469`/`:354`). Four things break out of it:

**1. "Application" has two trigger labels and two variants for one action.**
- `features/environments/EnvironmentsPage.tsx:464` — `btn-secondary`, "New application" (toolbar)
- `features/environments/EnvironmentsPage.tsx:537` — `btn-primary`, "Create application" (empty state)

Same action, same page, two labels and two prominences. Every other entity uses one trigger label.

**2. Title Case vs sentence case for the same pattern.** "New Test" is Title Case at 5 call sites (`HomePage.tsx:138,191,354`, `RunsListPage.tsx:540`, `LaunchRunButton.tsx:124`, plus `Sidebar.tsx:268` and `router.tsx:71`) while the other six "New X" buttons are sentence case. Other Title Case strays: `'Skip Phase'` (`GateActionBar.tsx:169`), `'Save Edit & Re-review'` (`:149`), `'Save Draft'` (`NewRunWizard.tsx:631`), `'Prompt Review'` (`PromptReviewSection.tsx:190`), `'Copy ID'` (`ReviewStep.tsx:42`), `'Compare Runs'` (`ComparePage.tsx:50,253`), `'Test History'` and `'Apps & Environments'` (`HomePage.tsx:356,359`).

The clearest instance: **"Reset Demo"** at `features/settings/SettingsPage.tsx:120` and **"Reset demo"** at `components/layout/Sidebar.tsx:149` — the same action, two casings, both reachable in the same session.

**3. Glyphs inside labels.** Five glyphs are used as or inside button text, each exactly once or twice: `▶` (`GateActionBar.tsx:149`), `↻` (`:181`), `✕` (`ComparePage.tsx:164` — the entire visible label; it does have `aria-label="Remove … from comparison"`, so this is cosmetic not an a11y defect), `⋯` (`PreflightModal.tsx:222`, the OverflowMenu trigger default), `?` (`ApprovalsInboxPage.tsx:328`). Nowhere else in 197 buttons is a glyph used, and the app has an `<Icon>` component (`Sidebar.tsx:50`) that is not reused outside the sidebar.

**4. The product name "Load" is capitalised inconsistently in adjacent lines.** `features/runs/RunOverview.tsx:121` — "Open load execution" (lowercase); `:142` — "Open Load {section}" (capitalised). Same file, 21 lines apart.

### Recommended change

1. Pick sentence case for all button and control labels (it is already the majority — 6 of 7 "New X", all 6 "Create X", all 22 "Cancel", every "Save …"). Change the 5 "New Test" sites plus `Sidebar.tsx:268` and `routes/router.tsx:71`, and the 8 Title Case strays listed above. Fix `SettingsPage.tsx:120` to "Reset demo".
2. Make `features/environments/EnvironmentsPage.tsx:464` and `:537` both read "New application"; keep `btn-primary` in the empty state (where it is the only action) and `btn-secondary` in the toolbar — that variant difference is defensible, the label difference is not.
3. Replace the `▶` and `↻` glyphs at `hitl/GateActionBar.tsx:149,181` with plain verbs, and route `✕` / `⋯` / `?` through the existing `<Icon>` component so icon usage is one system.
4. Fix `features/runs/RunOverview.tsx:121` to "Open Load execution".
5. Add the casing rule to the contract's Buttons/fields row so it is checkable in review.

---

<a id="con-16"></a>
## CON-16 — ID truncation happens in one feature only, and truncated IDs cannot be copied

**P3 · consistency, ease of use · `/cycles*`, `/runs*`, `/evidence`, `/logs`, `/runs/new`**

### Truncation is confined to `cycles`, always at 8 characters

Eleven call sites, all `id.slice(0, 8)`, all in `features/cycles` plus one in the wizard:

`features/cycles/ExecutionWorkspace.tsx:136,157,176`, `features/cycles/pages.tsx:294,338,401,466,573,668`, `features/cycles/ProviderScriptImport.tsx:227`, `features/cycles/CycleEditor.tsx:285`, `features/new-test/NewRunWizard.tsx:581`.

Everywhere else, IDs render in full: `features/runs/RunRail.tsx:157-159` (`<dt>Thread</dt>` + full UUID), `features/runs/RunsListPage.tsx:155` (`<code className="runs-run-identity">{run.thread_id}</code>`), `features/context/EvidenceTab.tsx:242` (`<dt>Thread ID</dt>`), `features/compare/ComparePage.tsx:160` (`compare-col-id`), `.approval-identity` (`theme/ux.css`). `theme/ux.css` even has dedicated rules — `.runs-run-identity { overflow-wrap: anywhere; white-space: normal }` and `.approval-identity { overflow-wrap: anywhere }` — to make full UUIDs wrap rather than truncating them, which is a deliberate choice in the opposite direction from `cycles`.

**No truncated ID is copyable.** None of the 11 `slice(0, 8)` sites has a copy button, a `title` attribute carrying the full value, or a link. So on `/cycles` the user sees `a3f91b2c` and has no way to obtain the full identifier the API needs.

### Copy affordances take three shapes with two feedback behaviours

| Shape | Location | Feedback |
|---|---|---|
| `btn-ghost btn-sm` labelled "Copy ID" with `aria-label={`Copy ${id}`}` | `features/new-test/steps/ReviewStep.tsx:42` | Sets `copyStatus`, with a graceful "Copy unavailable; select the ID to copy it" fallback (`:34`) |
| Bespoke `.copy-button` labelled "Copy" | `features/runs/RunRail.tsx:162-168` | **None** — `void navigator.clipboard?.writeText(...)` with no success or failure signal |
| `OverflowMenu` item "Copy run ID" | `features/runs/RunsListPage.tsx:209` | **None** |
| Automatic, on dialog open | `features/admin/ConsumerKeyLifecycle.tsx:31` | `"Copied"` / `"Copy"` toggle at `:60` |

`.copy-button` (`features/runs/run-detail.css:705-713`) is also the app's smallest interactive target: `padding: 2px 6px; font-size: 10px` with no `min-height`, computing to roughly 18 px tall — well under the contract's 32 px small-button floor and under WCAG 2.5.8's 24 px minimum. It is not a `.btn`, so `theme/ux.css:10-16` never applies to it. And because it lives in `run-detail.css`, it inherits CON-01's chunk problem wherever it might be reused.

### Recommended change

1. Extract `components/ux/CopyableId.tsx`: renders a `<code>` with the full value, an optional visual truncation via CSS (`text-overflow: ellipsis`, so the DOM text stays complete and selectable), a `btn-ghost btn-sm` copy action with an `aria-live` confirmation, and `ReviewStep.tsx:34`'s clipboard-unavailable fallback — which is the only correct handling of the four.
2. Use it at all 11 `cycles` truncation sites and the four existing copy sites. Delete `.copy-button` from `features/runs/run-detail.css:705-720`.
3. Decide one policy — I recommend full ID in the DOM, visually truncated by CSS, always copyable — and record it in the contract's Library-controls row.

---

<a id="con-17"></a>
## CON-17 — Shared helpers duplicated verbatim instead of imported

**P3 · consistency · multiple**

Five verbatim duplications. None causes a visible difference *today*, which is exactly why they will drift:

1. **`formatBytes` ×3**, byte-identical: `features/context/contextLogic.ts:13-17` (exported), `features/artifacts/ArtifactViewerPage.tsx:55-59`, `features/new-test/steps/ContextStep.tsx:21-25`. Covered in CON-08 along with the fourth site that bypasses all three.
2. **`TOOLTIP_CONTENT_STYLE` ×2**, byte-identical (same 5 properties, same order): `features/runs/EngineStrip.tsx:56-62` and `features/analytics/AnalyticsPage.tsx:69-75`. These are the Recharts tooltip surface for every chart in the app; if one is restyled the two charts diverge silently.
3. **`@keyframes pulse` ×2**, byte-identical: `theme/primitives.css:41-44` and `components/layout/Sidebar.css:441-444`. Harmless because identical, but 22 `animation: pulse …` declarations across 15 stylesheets now depend on CSS injection order for which definition wins.
4. **Three dead `@media (prefers-reduced-motion: reduce)` blocks**: `features/runs/live.css:60-64`, `hitl/hitl.css:397-401`, `components/runs/PhaseStrip.css:263-267`. All three are fully subsumed by the global rule at `theme/accessibility.css:89-91` (`*, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important }`). They are not wrong, just misleading — a reader would conclude reduced motion is handled per component.
5. **Five near-identical inline-error rules** — covered in CON-06.

### Recommended change

Move `TOOLTIP_CONTENT_STYLE` into a shared `features/charts/chartTheme.ts` (or `src/utils/`) and import it in both chart modules; delete `components/layout/Sidebar.css:441-444`; delete the three redundant reduced-motion blocks; apply CON-08's `formatBytes` consolidation.

---

# Checked and found consistent

These were examined and **pass** — recorded so the report says what is already right and so a future reviewer does not re-derive them.

### Primitives with real adoption
- **`ProblemCard`** (`components/ProblemCard.tsx`) — **42 call sites** across 28 modules, every one passing `title` + `message` and most passing `onRetry`. `role="alert"` is on the component, not the call site, so it cannot be forgotten. It is also the only state treatment with a distinct visual signature (`border-left: 3px solid var(--danger)`).
- **`Dialog`** (`components/Dialog.tsx`) — **all 22** dialogs use it. The implementation is unusually careful: a token-keyed `activeDialogs` stack with `inert` + `aria-hidden` on non-top dialogs (`:252-263`), a `MutationObserver` that inerts body children appended *after* the dialog opened (`:216-230`), body-scroll lock with restore, Tab wrapping in both directions (`:142-166`), focus restore guarded against `[inert]` ancestors (`:99`), and `closeOnBackdrop` / `closeOnEscape` correctly disabled while a mutation is pending at 6 call sites.
- **`.data-table .num`** (`theme/primitives.css:424-427`) is the single numeric-column rule in the app — `text-align: right` plus `font-variant-numeric: tabular-nums`. Used at `features/context/DocumentsTab.tsx:354`, `features/environments/EnvironmentsPage.tsx:170,572`, `features/environments/EnvironmentDetailPage.tsx:230,238`, `features/analytics/AnalyticsPage.tsx:625-631`. No competing definition exists; the other `text-align: right` hits in feature stylesheets are on unrelated selectors.
- **`formatDuration`** (`features/runs/runDisplay.ts:202-218`) — one implementation, one output grammar (`850ms` / `4.2s` / `1m 15s` / `1h 4m`), em dash for absent values, used everywhere durations appear.
- **Phase identity** — `PHASE_LABELS` (`features/runs/runDisplay.ts:14-22`) is keyed off `PhaseName` from `@apex/pipeline-events`, and the module docstring states the rule (*"Phase identity comes from @apex/pipeline-events — never redeclared"*). `grep` confirms no second phase-label map exists; the wizard, gate, timeline, rail and Analytics all read this one. The seven labels are stable across every surface.
- **`EM_DASH` / missing-value rendering** — `—` is used consistently for absent values, and the vocabulary for *why* a value is absent is unusually disciplined: `executionResultLabel` (`runDisplay.ts:149-155`) distinguishes `Inconclusive` / `Not assessed` / `Not applicable` / `Not available`, matching the contract's Results row (*"Missing assessment remains unknown"*).

### Error surfacing
- **No toasts anywhere.** `grep -rn -i "toast\|snackbar"` returns only four comments *stating the decision* (`features/admin/ConnectionDetailPage.tsx:7`, `features/environments/EnvironmentDetailPage.tsx:9`, `features/admin/ConsumersPage.tsx:8`, `features/admin/ConnectionsPage.tsx:7`). The resulting three-tier model — `ProblemCard` for route-level failure, inline `role="alert"` for in-form failure, `CachedDataWarning` for stale-but-usable — is coherent and consistently applied.
- **Live regions are thorough**: ~240 `role="alert"` / `role="status"` / `aria-live` attributes across 60 modules. Every mutation error path I sampled had one. The `aria-busy` + `aria-label="Loading …"` pairing is on ~22 loading containers.
- **Delete-dialog copy is formulaic in a good way**: all six use `<h2>Delete {entity}</h2>` + "This permanently removes **{name}**" + a sentence on what is *not* affected ("Runs that already consumed it keep their copies", "Runs that used it are unaffected", "and its host mappings", "and its scan history", "and revokes its key"). That last clause is genuinely useful and present in every one.
- **"Delete…"** with an ellipsis is used consistently for row triggers that open a confirm (4 of 5 sites). Only `features/admin/ConnectionDetailPage.tsx:692` omits it.

### Charts
- Every series colour resolves through `var(--chart-1..6)` — no literal colours in any chart. `features/analytics/AnalyticsPage.tsx:51-58` defines `CHART_COLORS` as the six tokens; `features/runs/EngineStrip.tsx:40-53` assigns per-metric tokens.
- Axes, grid and tooltips are token-driven and identical across both chart modules: `CartesianGrid stroke="var(--border)" strokeDasharray="3 3"`, `XAxis`/`YAxis` `stroke="var(--text-muted)" tick={{fontSize:10}} tickLine={false} axisLine={false} minTickGap={32}`.
- **The palette is not exhausted.** `features/analytics/AnalyticsPage.tsx:300-301` caps the stacked time series at `rankedKeys.slice(0, 5)` and folds the remainder into a single `other` field — 6 fields against a 6-colour ramp, so `CHART_COLORS[index % 6]` never wraps. The bar chart (`:400`) caps at `TOP_N_BARS = 10` but uses one fixed `fill="var(--chart-2)"`, so no collision there either. This is the one place I expected to find a palette bug and did not.
- **Reduced motion is a non-issue for charts**: `isAnimationActive={false}` is on all 5 `<Tooltip>` and all 6 series components. Charts never animate in any theme or motion setting.
- Chart series carry `name` props, so tooltips and the one `<Legend>` (`AnalyticsPage.tsx:436`) read human labels, not `series_0`.

### Theme and preference plumbing
- **No literal colours in any component CSS except the 10 listed in CON-06/CON-09.** The tokens-only rule is otherwise held across ~30 feature stylesheets, which is a strong result for a codebase this size.
- All six themes define the full 20-token accessibility override set (`theme/accessibility.css`) including `--accent-contrast`, `--danger-contrast` and `--focus-ring` — so `.btn-primary` / `.btn-danger` text contrast and the focus ring are explicitly tuned per theme rather than inherited.
- **`data-reduce-motion` is honoured.** `theme/ux.css:188-193` applies `animation: none !important; transition: none !important; scroll-behavior: auto !important` to `*`, `*::before` and `*::after` under `:root[data-reduce-motion='true']`, mirroring `theme/accessibility.css:89-91` for the OS-level media query. Both paths are set by `theme/useTheme.ts:73-76` and `theme/preferences.ts:59`. The only caveat is timing, reported in CON-10.
- **No JS-driven motion to leak past the CSS gate.** The five `scrollIntoView` / `requestAnimationFrame` uses (`NewRunWizard.tsx:133,224`, `ApprovalsQueue.tsx:40`, `RunsListPage.tsx:302`, `GateModule.tsx:342,357`, `AppShell.tsx:39`) are all either focus management or `scrollIntoView({ block: 'nearest' })` with no `behavior: 'smooth'`. `grep` confirms zero `behavior: 'smooth'` and zero Web Animations API calls in `src/`.
- **Preferences are robust**: `theme/preferences.ts` validates `rows` against the allowed set on read (`:28`), falls back to defaults on a JSON parse failure (`:31-33`), degrades to `sessionOnly` when `localStorage` throws (`:56`), and syncs across tabs via a `storage` listener (`:36-40`). `useSyncExternalStore` with a stable server snapshot avoids hydration mismatch.
- **Contract clause verified:** *"Specialized Orchestration tables retain their existing pagination limits; the personal row default applies to Test History."* The code does exactly this — see CON-11's detailed walk-through.

### Routing
- Every one of the 36 routes has a `handle(title)`, so the Topbar never falls back to the generic `'APEX Orchestration'`.
- `RouteErrorBoundary` is attached at both levels (`routes/router.tsx:43,49`) and renders `ProblemCard`, so a thrown route error gets the same treatment as a fetch error.
- `isNavItemActive` (`components/layout/Sidebar.tsx:181-188`) correctly treats `/approvals*` as part of the `/runs` nav item and correctly excludes `/runs/new` — so the unified Tests nav item highlights accurately across all five of its routes.

---

# What I could not verify from source alone, and why

Everything below needs a browser. I have stated the expected observation for each so verification is a yes/no check.

### Needs a browser — rendering and computed style
1. **CON-01's blast radius, confirmed visually.** Load `/prompts` in a fresh session (no `/runs` visit), trigger an API failure, and confirm the error banner has no border or background. Then visit `/runs/:threadId` and return to `/prompts`; the banner should now be styled. Also check `/` with a failing `/v1/pipelines`. *I verified the import graph and the `cssCodeSplit` default but cannot observe computed style.*
2. **CON-04's persistence.** Load any unmatched URL (e.g. `/zzz`), then navigate to `/runs` and `/admin/connections`, and confirm the `.dash-empty` cards have lost their border, background and 24 px padding for the rest of the session. *CSS injection order is deterministic in theory; only the browser proves which rule wins.*
3. **Tab target sizes (CON-02).** Measure `.ctx-tab`, `.adm-tab` and `.engine-metric-tab` with devtools. My heights are computed from `padding` + `font-size` + `line-height: 1.5`; `border` and inherited `line-height` could shift them by a few px either way. The claim that `.engine-metric-tab` fails WCAG 2.5.8 (24 px) depends on that measurement.
4. **CON-10's theme flash.** Hard-reload with a cold cache and the default (unset) theme and confirm the dark→light fade. Throttling the network will make it obvious. Also confirm whether `data-reduce-motion` suppresses it (I expect not, since the attribute is set after the transition fires).
5. **Scrim appearance per theme (CON-06).** Open any delete dialog in `solarized-light` and confirm the hardcoded `rgba(0,0,0,0.45)` reads as wrong against that theme's intended `rgba(253,246,227,0.8)`. Same check in `apex-light` (intended 0.24 vs actual 0.45).
6. **Skeleton phase mismatch (CON-05).** Open `/logs` or `/analytics` beside any other list route and confirm the skeletons pulse in opposite directions.
7. **Contrast of the undefined-token fallbacks (CON-09).** With `--accent-results` undefined in `dark`, `components/runs/PhaseStrip.css:146-148` falls back to `currentColor` for the border and drops the `color-mix` declarations. Whether the resulting "results ready" phase segment is merely undistinguished or actually illegible needs a look. `scripts/check_dashboard_contrast.py` may already cover the token set; it will not cover a token that resolves to nothing.

### Needs a browser — keyboard and assistive tech
8. **Tab-stop counts (CON-02).** Tab through `/apex-load`, `/admin/connections` and `/runs/:threadId` and count stops. Expected: 2 extra on each admin/load page, 4 extra on Run Detail from the EngineStrip metric tabs.
9. **Screen-reader announcement of the unlinked tabs (CON-02).** With VoiceOver or NVDA on `/context`, confirm that activating a tab announces a selection but offers no route to the panel, and that the `disabled` tabs on `/prompts/:ns/:name` and the Prompt Review editor vanish from the tab count rather than announcing as unavailable.
10. **Duplicate column headers (CON-13).** Confirm in a screen reader's table-navigation mode that `/runs` announces "Run" for both column 1 and the last column.
11. **Double `<h1>` on `/cycles` (CON-14).** Confirm with a heading-outline tool that two level-1 headings are present.

### Needs a running backend
12. **Live timestamp strings (CON-08).** My renderings are derived from the `Intl` option objects. Confirm the exact output of `formatTimestamp` (expected `Oct 17, 01:09:24 PM` in `en-US`) beside `formatRelative` in the same `/runs` row, and the `/cycles` `toLocaleString()` form. Locale-dependent, so note the browser locale when checking.
13. **Error-rate precision (CON-12).** Find one run with a non-zero error rate and read its value on `/` versus `/runs/:threadId`. Expected `2.3%` vs `2.34%`.
14. **Cycles pagination edge case (CON-11).** With more than 20 cycles and a server that returns a short non-final page, confirm that Next disables prematurely because of `disabled={query.data?.length !== 20}` at `features/cycles/pages.tsx:257`.
15. **`.tonal-card.success` (CON-01).** Action a gate from the `/approvals` inbox and confirm the "Decision receipt" banner has no success tint even on that route, since no `.tonal-card.success` rule exists anywhere.

### Deliberately out of scope
- The **APEX Load gap analysis** and the token / typography / modal-scrim / chart-palette comparison against `Project_Stormrunner/dashboard` — owned by another reviewer. Where a finding here has a Load answer I noted it in one line (CON-06's `--modal-backdrop`).
- **Home's missing Test result column** versus `/runs` — owned by another reviewer.
- Anything marked fixed in the prior audits listed in the brief. I found no regressions against them; the 11 tablists, the `.dash-empty` call sites and the `.tonal-card` sites are all pre-existing structure those audits did not cover.
