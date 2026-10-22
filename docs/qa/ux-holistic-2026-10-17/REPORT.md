# APEX Orchestration — holistic UX review

> **Re-reviewed 17 October 2026.** The remediation branch `codex/holistic-ux-remediation-2026-10-17` fixes the P0 and almost all P1s; verified findings,
> three that do not hold, and three new minor ones are in [RE-REVIEW.md](RE-REVIEW.md).

17 October 2026 · consistency · thoroughness · correctness · ease of use · accessibility

**Verdict: the primitives are good and barely reach.** Almost every pattern this dashboard needs
already exists somewhere in it — a tabs component with a real keyboard contract, a dialog with a
careful inert stack, a `--modal-backdrop` token, a navigation guard, a type-to-confirm, a
pessimistic gate machine that handles two operators colliding exactly right. Each one is then
used in one or two places while the other twenty surfaces reimplement it locally, and the local
copies are where the defects live. The second theme is that the product states facts it has not
established: a failed run shows its failing phase as *Pending*, a run with no collected results
is ranked the best in a comparison, a test result is published before execution finishes, and
every approval in the system is attributed to `unknown`.

Nothing here argues for a redesign. The work is to make the existing primitives the only way to
build a surface, and to make every status the UI prints trace to a fact the API actually returned.

---

## What was reviewed

| | |
|---|---|
| Routes | 36 of 36 reached at least once; 33 exercised in depth |
| Findings | 120 (5 sweeps + the lead reviewer), of which 34 independently re-verified |
| Captures | 61, registered in [`capture-register.json`](capture-register.json) |
| Data modes | demo (ACC add-a-line v2.0) and connected (simulated engine, 39→50 seeded runs, 17 live gates) |
| Roles | viewer, operator, project-scoped admin, global admin — all four exercised against the real API-key gate |
| Themes | all six, plus the `system` preference |
| Viewports | 390 · 768 · 1440 · 200% zoom |

Detail lives in seven companion documents: [LOAD_CONVERGENCE.md](LOAD_CONVERGENCE.md),
[CONSISTENCY.md](CONSISTENCY.md), [CORRECTNESS.md](CORRECTNESS.md),
[ACCESSIBILITY.md](ACCESSIBILITY.md), [AGENT_UX.md](AGENT_UX.md), [AREAS.md](AREAS.md) and
[LEAD_FINDINGS.md](LEAD_FINDINGS.md), with [VERIFICATION.md](VERIFICATION.md) recording what
survived an independent reproduction pass.

### How this was set up

Connected-mode work ran against a **production build behind a same-origin proxy** reproducing the
deploy contract in `apps/dashboard/README.md` — SPA fallback, `/v1` proxied, the `/runs`
HTML-vs-API split keyed on `Accept`, unbuffered SSE. This matters: `apps/dashboard/.env.development.local`
sets `VITE_APEX_DEV_AUTH=true`, which auto-authenticates an admin and would have made the role
and scope findings impossible. A second isolated origin was used for role switching so sessions
did not collide.

---

## The findings that matter

### 1. A keyboard trap in the prompt gate that silently rewrites the prompt — P0, WCAG 2.1.2

Thirteen Tabs from the run title reach the system-prompt editor. Six more do not leave it. They
are swallowed by CodeMirror and inserted as indentation: the prompt grew 735 → 747 characters and
now begins with twelve leading spaces, and the primary action flipped from **▶ Execute Phase** to
**Save Edit & Re-review**. Escape then Tab does escape — onto the Save button, so the next
keystroke commits the corrupted prompt as a human-authored override. No hint anywhere advertises
Escape, which is why 2.1.2 fails regardless.

One missing prop: `<CodeMirror>` in `PromptTabsEditor.tsx` is mounted without
`indentWithTab={false}`, and `@uiw/react-codemirror` defaults it to true. Output gates are clean.
*(A11Y-04, confirmed by the lead and by verification. Capture `a11y-03`.)*

### 2. Every approval is attributed to "unknown" — P1

The APPROVALS panel renders, verbatim: `PROMPT_REVIEW · APPROVE · unknown · Oct 17, 01:25:25 PM`.
The API agrees — `actor: "unknown"` on every record — though the decisions were made over an
authenticated session whose consumer the backend resolves on every request. `revise` and `discuss`
are not recorded in `approvals[]` at all, so a reviewer cannot see that a human sent the agent
back. The governance reviewer's first question has no answer anywhere in the product. *(L-01.)*

### 3. The product states facts it has not established — P1

Five instances of one root cause: a value is printed as settled when the underlying data is
absent, pending, or contradicted.

- **A failed run shows its failing phase as "Pending"**, keeps a *SNAPSHOT REFRESH ACTIVE* chip on
  a terminal thread, and uses "yet" in three places. The failure reason is unreachable in the UI.
  The snapshot carries that phase as `running` in `phase_strip` and `pending` in `phase_results`;
  the UI picks the most innocuous of three. *(R-01, COR-06 — the two sources disagree on 18 of 45 runs.)*
- **Compare ranks a run with no collected results as the best run.** `run-aborted` reads
  ORCHESTRATION FAILED / EXECUTION FAILED / RESULT FAILED, yet supplies p95 `0 ms` and error rate
  `0.00%` as a green-tinted Baseline while the two runs that completed are tinted pink and
  labelled *Worse*. The delta text says "percentage not available" — the code already knows that
  baseline is not a measurement. *(COR-02. Captures 14, `corr-07`.)*
- **"Test result: Failed" before execution finishes**, on a run awaiting a gate whose own button
  says it "does not approve the test result". *(COR-03.)*
- **Home reports failure three times with three numbers** over one 39-run fleet: FAILURES **4**,
  Test Results *Failed* **0**, Execution Health *Phases failed* **0**. The red tile ORs
  orchestration errors with test failures; the panel beside it counts test failures only. *(H-01.)*
- **"7/7 phases finished"** on a failed run, because *finished* counts failed, aborted and
  skipped. *(COR-05.)*

### 4. Irreversible actions are protected in inverse proportion to their cost — P1

| Action | Reversible? | Protection |
|---|---|---|
| API key creation | No | Dialog, "never shown again", Copy, **a checkbox that gates the confirm button**, and a navigation guard |
| Abort a run | No | `btn-danger` + type `ABORT` |
| **Skip a phase** | **No** | **One click. Secondary styling. No confirmation.** |
| **Edit a prompt at a gate** | **No** | **None. One click on another queue row discards it silently.** |
| Cycles review actions (5) | approve: no | One unconfirmed click each |

The app ships a priority-ordered `NavigationGuards` system. Exactly four surfaces register one —
three in Cycles and the API-key dialog. Nothing in `hitl/`, `runs/`, `approvals/` or `new-test/`.
The product protects an unrecoverable secret perfectly and an operator's hand-written prompt not
at all. *(G-01, G-02, ARE-10. Captures 10, 11, `agent-06`.)*

### 5. The operator cannot launch anything, and the wizard never says so — P1

| Identity | `POST /v1/pipelines` |
|---|---|
| viewer | `403 Requires role 'operator' or higher` |
| **operator** (seeded, unscoped) | **`403 pipeline scope is not authorized`** |
| admin | `201` |

Signed in as that operator, `/runs/new` serves all six steps and a search for *not authorized /
permission / denied / forbidden* across the page matches nothing. The mismatch runs both ways: a
project-scoped admin is refused catalog mutations by the UI (`isGlobalAdmin` requires
`scopes.length === 0`) but **accepted by the server** — `POST /v1/catalog/applications` returned
`201`. Cycles has the same defect in the first direction, where a 403 renders as *404 "Cycle not
found"*. *(P-01, P-02, ARE-13. Capture `area-08`.)*

### 6. Error banners render unstyled on most routes, depending on navigation history — P1

`.tonal-card` and its `.warning`/`.danger` modifiers are defined **only** in
`features/runs/run-detail.css`, imported by one module, while 27 call sites across 18 modules use
the class — including the shared `CachedDataWarning` and Home. Measured on a cold load of
`/prompts`: no matching CSS rule exists and a probe element computes to no border, transparent
background, zero padding. Visit a run detail page, return, and the identical banner renders
bordered and pink. `vite.config.ts:77` sets `test.css = false`, so no unit test can catch it.
*(CON-01, confirmed by CSSOM probe including the intermittency.)*

### 7. Nothing is announced — P1

`RunRail` holds the only `role="status"` gate banner and **no route renders it**; `LiveStatusChip`
declines to be a live region in a source comment *because* "the run rail's gate banner owns that
landmark". Five gate changes produced zero live-region mutations. Gate openings, phase changes and
stream-status changes are all silent. The approvals queue is a correct `listbox` with a correct
roving tabindex whose selection never takes focus, so `j`/`k` swap the entire workspace and a
screen-reader user hears the new run's title and nothing else — not queue position, not gate kind,
not how many remain. *(A11Y-06, A-01.)*

### 8. The contract's primitives reach one or two surfaces each — P1/P2

`TabList` covers 2 of 11 tablists. `.ux-filters` covers 1 of 18 tables. The Rows control covers 1
of 5 paginated tables. Sortable headers, 1 of 18. `--modal-backdrop` is defined in all six themes
and consumed **zero** times while 7 dialogs hard-code three different black scrims. Seven of the
eleven tablists have no `aria-controls`, no roving `tabIndex` and no arrow keys; six have no
`role="tabpanel"` anywhere. *(CON-02, CON-06, CON-11.)*

### 9. Orchestration froze while Load moved on

The token sheet was ported **verbatim** from APEX Load (ADR-0006) and has not tracked it since.
Load declares 103 theme tokens to Orchestration's 77 — **34 Load-only**, and **130 value
divergences** across the shared ones. The `light` theme is now a different product: **54 of 57
shared tokens differ**, Load having replaced indigo glassmorphism with an opaque cobalt palette.
Load went flat (killing the mesh gradient and frosted top bar Orchestration still paints), went
sentence case (309 `text-transform` declarations routed through `--label-case`, against
Orchestration's 63 hard-coded `uppercase` and zero uses of the token), and built a six-step type
scale Orchestration lacks entirely — against 372 literal `font-size` declarations spanning 20
values including 9px. Orchestration has **no global styling rule** for `select`, `input` or
`textarea`, so native OS widgets render light over the dark themes; `color-scheme` is `normal` in
every theme. *(LOAD_CONVERGENCE.md, A11Y-14.)*

Convergence is not wholesale: keep `TabList` (Load has no shared tab component and Orchestration's
keyboard contract is better), keep the chart axis/grid wiring, and adopt `--accent-text` **before**
reverting `--accent`, or ~160 accent labels drop below 4.5:1.

### 10. Contrast and colour-only encoding

`.dash-badge` hard-codes `color: white` instead of `--accent-contrast` — 2.98 / 2.19 / 2.04:1
across the three dark themes, and 99 of 140 axe nodes from that one declaration. Chart series are
encoded by colour alone and **no series pair reaches 3:1 in any theme**, several measuring exactly
1.00:1. The prompt version diff renders changed lines at 1.11:1 and 1.16:1 — blank bars.
*(A11Y-01, A11Y-03, ARE-24.)*

### 11. Cycles is not production-ready

Never reviewed before and absent from `PARITY.md`. The data model, preflight-blocker engine and
deliverable versioning are better than most of the app, but it ships three self-declared
non-functional sub-features (*"Cycle dispatch is not enabled"*, *"Retrieval connectors are not
available in this editor yet"*, *"Publication is not configured"*), all five review actions fire
unconfirmed, archive renders before submit, the working navigation guard is bypassed by its own
"Cancel editing", and two of its three dialogs render unstyled native buttons. *(ARE-10…21.)*

### 12. Test Presets has been unreachable for over a month

`POST /assistants/search` returns **HTTP 500**, direct and proxied, while `/v1/pipelines` returns
200. The 2026-09-14 resources audit recorded the identical failure. *(ARE-07.)*

---

## What this product gets right

Worth protecting, because several of these are better than the surfaces around them.

- **Two operators colliding on one gate is handled correctly.** The loser gets *"Decision was
  completed elsewhere"*, the row greys, the badge decrements, and the API records exactly one
  approval. Nothing is decided twice. This is ADR-0006's pessimistic resume working as specified.
- **`x` arms a typed confirmation rather than aborting**, and approve/skip are deliberately never
  bound to bare letters — documented as a decision in `gateModuleContract.ts`.
- **Run detail's header separates four facts**: orchestration status, test result, phase progress,
  and a Test acceptance panel stating plainly that completing orchestration does not certify a
  release.
- **Prompt provenance is shown where the decision is made** — `catalog · phase/story_analysis/system@v1`
  with "Prompt source: catalog" and a Tools in Play panel.
- **Re-run preflight explains its own rule** and adapts rather than refusing: deselect Execution
  and the Reporting row becomes *"Will reuse Execution artifacts (attempt …)"*.
- **The one-time API key dialog** is exactly proportionate to an unrecoverable secret.
- **The live-transport status model** is six named states with distinct labels, kept separate from
  orchestration status.
- **The durable transcript path works end to end** — every phase, 200 `text/plain`, rendered with a
  TRANSCRIPT chip and Download.
- **`Dialog` is used by all 23 dialogs** with a genuinely careful inert stack, and `ProblemCard`
  reaches 41 sites. Deciding against toasts was right.
- **Chart palettes are token-driven and capped at 5+other**, so they never wrap.
- **The wizard's step tabs are fully compliant** — `aria-controls`, roving `tabIndex`,
  Arrow/Home/End, `hidden` panels. This is the model the other ten tablists should copy.

---

## Top 10 fixes, ranked by user impact against effort

| # | Fix | Impact | Effort | Findings |
|---|---|---|---|---|
| 1 | `indentWithTab={false}` in `PromptTabsEditor.tsx` | Removes a P0 trap that silently corrupts agent prompts | One prop | A11Y-04 |
| 2 | Move `.tonal-card` into `theme/primitives.css` and add `.success` | Restores every error banner on 21 of 36 routes | One file move | CON-01 |
| 3 | `.dash-badge` → `var(--accent-contrast)` | Clears 99 of 140 axe nodes | One line | A11Y-01 |
| 4 | Record the authenticated consumer as the approval `actor`, and record revise/discuss | Makes governance audit possible at all | Backend + render | L-01 |
| 5 | Derive phase status once, with `thread_status` as an input; suppress live chips and "yet" copy on terminal threads | Stops a failed run reading as pending, in every surface at once | One normaliser | R-01, COR-06, COR-05, AGT-05, AGT-08 |
| 6 | Gate KPI participation on execution having succeeded; exclude uncollected runs from ranking and baselines | Stops compare recommending the failed run | Small, local to `compareModel.ts` | COR-02, COR-12 |
| 7 | Confirm Skip Phase; register a navigation guard on the gate editor and keep the dirty buffer per `interrupt_id` | Closes the two unprotected irreversible paths | Reuses existing components | G-01, G-02 |
| 8 | Resolve launch capability from scopes, not role; mirror the server's two distinct refusals | Stops operators authoring tests they can never run | Auth shaping | P-01, ARE-13 |
| 9 | Adopt Load's `typography.css`, `--label-case`, `--modal-backdrop`, the six form-surface tokens and `color-scheme` | Closes most of the convergence gap and the native-widget bug in one pass; pure CSS | Token file + sweep | LOAD, CON-06, A11Y-14 |
| 10 | Focus the selected `li[role=option]` on `j`/`k`; announce gate/phase/stream changes in one live region | Makes the approver queue usable with a screen reader | Small | A11Y-06, A-01 |

Fixes 1–3 are single-line or single-file and together clear a P0 and the two widest-blast-radius
defects in the review. Fix 5 is the highest-leverage change in the list: one normaliser resolves
findings across five documents.

---

## Per-area scorecard

1 = unusable · 3 = works with friction · 5 = exemplary.

| Area | Consist. | Thorough. | Correct. | Ease | A11y | Note |
|---|---|---|---|---|---|---|
| Home | 2 | 3 | **1** | 3 | 2 | Three failure numbers for one fleet; no test-result column |
| Tests / run list | 3 | 4 | 3 | 3 | 2 | Two columns headed "Run"; PROGRESS carries no phase state |
| Run detail | 3 | 4 | **2** | 3 | 2 | Best four-fact header in the app; terminal states wrong below it |
| Compare | 2 | 3 | **1** | 3 | 2 | Ranks a failed run best; "Not running" erases outcomes |
| HITL — gates & inbox | 3 | 4 | 3 | 3 | **1** | Concurrency exemplary; P0 trap, no announcements, unguarded edits |
| New Test wizard | 3 | 3 | 2 | 3 | **4** | Step tabs are the app's best; readiness contradicts itself |
| Prompts | 3 | 4 | 3 | 3 | 2 | Diff unreadable at 1.11:1; create silently activates |
| Golden Configs | 2 | **1** | 2 | 2 | — | Unreachable in connected mode for over a month |
| Cycles | 2 | 2 | 2 | 2 | 2 | Strong model, unsafe lifecycle, three dead sub-features |
| Resources | 3 | 3 | 3 | 3 | 2 | Steady; dead `context_summary_ids` capability |
| Analytics / Logs | 2 | 2 | **1** | 2 | 2 | Phantom model facet, buckets a day early, gate wait as latency |
| APEX Load workspace | 2 | 2 | 3 | 2 | 2 | Operator destination behind an admin-only route |
| Admin / Settings | 4 | 4 | 3 | 3 | 3 | Key lifecycle is the best-protected flow in the product |
| Shell / theming | 2 | 3 | 3 | 3 | 2 | Six themes, 130 token divergences from Load, no type scale |

**Axis totals:** consistency 2.6 · thoroughness 3.0 · correctness 2.2 · ease of use 2.8 ·
accessibility 2.2. Correctness is the weakest axis and the one that most damages trust, because
every instance takes the form of the UI sounding more certain than its data.

---

## Coverage

### Walkthroughs

| | Walkthrough | Status |
|---|---|---|
| a | New test, draft → Review; abandon and resume | ✓ |
| b | Prompt gate: approve · modify+approve · skip | ✓ |
| c | Output gate: revise · discuss · approve | ~ revise and approve done; multi-turn discuss not exercised |
| d | Clear 5+ gates keyboard-only (j/k/o/m/x) | ~ shortcuts verified, 3 gates cleared; `m` is broken on the canonical review URL |
| e | Live run: stream, kill transport, reconnect, jump-to-live | ✗ **blocked — see below** |
| f | Concurrency: same gate in two browsers | ✓ passes |
| g | Re-run one phase through preflight, missing prerequisite | ✓ passes |
| h | Compare two runs against a chosen baseline | ✓ |
| i | Prompt catalog: edit, version, diff, roll back, playground | ✓ |
| j | Work items: NL query → edit → run → save → modify | ✓ |
| k | Admin: consumer, key shown once, rotate, connection, host mappings | ~ consumer/key/connections done; host mappings not exercised |
| l | Governance audit of a finished run | ✓ — and it fails, see finding 2 |
| m | Hand off to APEX Load and back | ✗ no Load connection configurable in this environment |

### Routes

33 of 36 exercised in depth. Not reached, with reasons:

- `/golden-configs/:assistantId` — `POST /assistants/search` returns 500 (ARE-07). The list page's
  error state was reviewed instead.
- `/work-items/:provider/:itemId` — no work-tracking provider bound in this environment.
- `/apex-load` beyond its unconfigured state — no Load console URL configurable, which also blocks
  walkthrough m and the handoff contract.

### Could not be verified, and why

- **Live streaming, reconnect, jump-to-live, event loss/duplication.** No live stream could be
  established in either data mode. In connected mode `useActiveRun` polls for a run id every 5 s
  and a simulated phase finishes in under 8 s; in demo mode `/runs/run-active` reads RUNNING and
  EXECUTING while its Pipeline Log says *"No live activity for this phase yet"* and no
  jump-to-live control exists in the DOM. The reconnect machinery reads correct in source and the
  correctness sweep separately confirmed the snapshot-is-canonical invariant holds — but the UI
  path was never exercisable. With a real engine, phases run for minutes and the poll would
  attach, so the connected-mode severity is environment-dependent; the demo-mode failure is not.
- **The 2026-09-14 "rerun expands scope by default" finding** — retesting needs a completed
  *subset* run, which this environment could not produce. Reported as not retested, not as fixed.
- **`aborted` phase state** — exists in neither dataset.
- **Real provider integrations, external ticket creation, production auth expiry** — all stubbed.
- MinIO is gone from Docker Hub, so the compose artifact store could not start; Postgres and Redis
  were run alone and the `minio-artifacts` connection disabled through the admin UI.

### Corrections applied after independent verification

| Claim | Outcome |
|---|---|
| A11Y-05 — `j`/`k` dropped at triage speed | **P1 → P3.** The stale-`stateRef` race is real in code, but at 0/5/10/16/25/30/120 ms all six presses land. Real keystrokes are 1:1 |
| A11Y-09 — 14 option rows each `tabIndex=0` | **P2 → P3.** False: measured 1 × `0` and 14 × `-1`, a correct roving tabindex |
| CON-02 — `.engine-metric-tab` "fails SC 2.5.8 outright" | **Deleted.** Measured 23.00 px with 56.87 px between centres: passes on the spacing exception |
| LOAD — `--modal-backdrop` "declared twice" | **Six times**, one per theme. Zero consumers either way |
| CON-04 — 404 stylesheet "flattens ~40 empty states" | **P2 → P3.** The unscoped redefinition is real; computed styles are identical before and after |
| A-01 — `j`/`k` "never move DOM focus" | **Restated.** Focus sits on `h2.run-detail-title`, not `main`. It never enters the queue, which is the real defect |
| CONSISTENCY — "~240 live regions" | **185** |

Two things chased and dismissed: two extra `none→succeeded` runs that looked like they realised
COR-06's hypothetical counter divergence are neutralised by `phases_plan`, and the connected fleet
has drifted to 50 runs while `GET /v1/pipelines` caps `limit` at 20, so full enumeration needs
pagination.

---

## Prior work

Findings already recorded in `docs/qa/ux-audit-2026-09-09`, `product-review-2026-09-09-round-2`,
the four `*-2026-09-14` audits, `docs/dashboard-usability-remediation.md`, `design-qa.md`, and
`PARITY.md`'s known partials F3/F4 are **not** repeated here. Those audits each declined to
certify screen readers, per-theme contrast, full keyboard traversal, zoom, reduced motion, role
and scope matrices, multi-operator concurrency, and UI-versus-API correctness — which is most of
this review's ground, so overlap is low.

Three regressions against work marked complete are reported with citations: comparison phase
normalisation and missing-result rendering (`dashboard-usability-remediation.md` lines 7, 9, 42 →
COR-01, COR-04, COR-06, COR-12); the Test Presets 500, unchanged since 2026-09-14 (ARE-07); and
`PARITY.md` row 3a's claim that the runs grid carries a phase-strip micro-viz, which is now dead
code (AGT-15).

## Scope limits

A design and correctness review, not a WCAG conformance certification. No application, backend or
`package.json` code was changed. Artifacts left in the connected environment: one cycle, two
unlaunched wizard drafts, one saved query, one prompt rolled back to v1, and roughly 50 seeded
pipeline runs. One probe application created during the scope test was deleted; the catalog holds
ACC only.
