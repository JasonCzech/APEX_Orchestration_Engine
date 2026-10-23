# New-test pipeline — focused UX, usability and completeness review

**Date:** 2026-10-22 · **Build:** `main` @ `aea2965` · **Reviewer:** Claude (review only; no application, backend or `package.json` changes)

**Scope:** the end-to-end path a tester takes to create and run a test: **New test wizard** (Scope → Config → Tickets → Knowledge → Prompts → Review) → **Launch** → **Run detail** (Overview, Workspace, Timeline, Review gate, Logs) → **recovery** (Create new test from this run, Re-run selected plan) → **Tests list**.

**Environment:** production dashboard bundle behind a same-origin proxy (deploy contract), local `langgraph dev` runtime (`langgraph-api 0.10.0`, in-memory store), Postgres/Redis in Docker, **Simulated** engine, signed in as global admin, light theme. Viewports were 1440×900, 1024×768 and 768×1024.

> **Status as of 2026-10-23.** The findings below describe `main` as it was on 2026-10-22.
>
> - F-01 through F-20 were fixed in PR #26, except F-19, which was not reproduced. [REMEDIATION.md](REMEDIATION.md) has the change behind each one.
> - A wizard launch now completes 7/7 on both runtimes:
>   - `langgraph dev`;
>   - the production Compose stack (Postgres runtime), after the fixes in PRs #29 and #30. See [PRODUCTION_RUNTIME_CHECK.md](PRODUCTION_RUNTIME_CHECK.md).
> - The [completeness check](#completeness-check--what-a-tester-needs-vs-what-exists) has a column with the current status.
> - Still open:
>   - the APEX Load handoff is unexercised;
>   - Compare doesn't show acceptance thresholds;
>   - there is no automated `/v1`-to-runtime integration test.

---

## Verdict

**The pipeline cannot currently be completed from the wizard.** Each of the two launches I made failed within about a second. A missing server-side prompt manifest caused both failures (F-01). The UI then tells the tester that *"No failure or stop reason was recorded"*, and both recovery buttons dead-end. Until F-01 is fixed, nothing after Launch can be used for a new test.

Apart from the launch, the **wizard is close to good**. The engine cards, structured workload, review policy, ticket search, document upload and prompt rail all work, and the Review step is thorough and honest. What holds it back:

1. A sticky readiness footer that takes up 16–27% of the screen.
2. A Scope step whose system-topology row is unstyled and whose readiness messages mislead.
3. Missing cross-field validation in Config.

The **post-launch screens** have the right structure. The phase strip, review gate and evidence coverage are all there. They do not, however, show the one thing a load tester came for: **observed KPIs against the thresholds they entered**.

| Area | Usability | Layout | Completeness |
|---|---|---|---|
| Scope | ◐ readiness messages mislead | ✕ unstyled participant row | ◐ no target-approval state |
| Config | ● clear engine and workload | ◐ footer covers fields | ◐ no ramp ≤ duration check |
| Tickets | ● search, select and add by key | ● | ◐ translated query is opaque |
| Knowledge | ● upload, parse, preview | ● | ◐ no reconciliation with thresholds |
| Prompts | ● vertical tab rail, keyboard OK | ● | ● |
| Review | ● comprehensive and honest | ◐ narrow card, duplicate text | ● |
| **Launch** | **✕ fails every time** | — | **✕** |
| Run detail (failed) | ✕ reason hidden, recovery dead-ends | ◐ CTA overlap | ✕ no failure event |
| Run detail (complete) | ◐ | ◐ header shifts per tab | ✕ no KPI vs threshold view |
| Review gate | ● clear decision copy | ◐ repeated copy, wrapped Abort | ◐ KPIs as raw string |
| Logs from run | ✕ shows unrelated entries | ● | ✕ window not applied |

● good · ◐ needs work · ✕ broken or missing

---

## Findings

Severity: **P0** blocks the workflow · **P1** major usability or trust problem · **P2** moderate · **P3** polish.

### P0

#### F-01 — Every wizard launch fails: "This test is missing its persisted prompt manifest"

- **What the user sees:** after **Launch test**, the run page shows *Orchestration failed · Phase plan not recorded for this run*. Every phase shows "?" and the timeline has 0 events. ([10](screens/10-run-failed-overview.jpg), [12](screens/12-run-failed-workspace.jpg), [13](screens/13-run-failed-timeline.jpg), [20](screens/20-relaunch-failed.jpg))
- **Reproduced 2 of 2 times:**
  - A fresh draft ("Workflow re-check 26.3.3", thread `9aced7dc…`).
  - A test cloned from completed run 26.3.1 (thread `5939bce3…`).
  - The backend log shows the same error for both: `ValueError(This test is missing its persisted prompt manifest)`, raised in the plan resolver (`src/apex/graphs/pipeline/graph.py:113-124`, `src/apex/graphs/pipeline/configurable.py:523`).
- **Why every launch fails:** the wizard always sends `prompt_selections` (`apps/dashboard/src/features/new-test/wizardState.ts:695`). The manifest is meant to be prepared server-side in the run-create auth hook (`src/apex/auth/handlers.py:2425-2443`).
- **Evidence from the stored run record:** I made a read-only copy of the dev server's run store and inspected it.
  - The failed run's `config.configurable` and `context` both contain `prompt_selections` and `model_by_phase`.
  - They contain **no `prompt_manifest`** and **none of the other values the hook stamps** (`apex_actor_id`, `langgraph_auth_user_id`).
  - `model_by_phase` is present only because the launch facade sets it itself (`src/apex/services/pipeline_read.py:921`).
  - Across all 63 runs in the store, no facade-created run carries a hook stamp.
  - So in this runtime, the hook's in-place changes to runs that the facade creates via `runs.create` (`pipeline_read.py:1249`) are never persisted. Either the hook is not invoked for these runs, or its changes are lost.
- **Not a data or seeding problem:** re-running `scripts/seed_prompts.py` reported every prompt as "already exists; unchanged", and the relaunch still failed.
- **Test gap:** `prompt_manifest` is only covered by unit tests that call the hook with a plain dict. No integration test launches through `/v1` against the real runtime and asserts the run reaches Story Analysis.
- **Not verified:** a production `langgraph up` / Postgres-backed runtime. The behaviour may differ there, but the local dev path is what every developer and demo uses.
- **Recommendation:**
  1. Freeze the manifest in the launch facade before `runs.create`. The facade already freezes the rest of the snapshot, so `prepare_prompt_manifest` belongs there. Keep the hook as a guard for direct native runs.
  2. Add an integration test that launches through the facade with default prompt selections and asserts the first phase starts.
  3. Separately, confirm that `apex_actor_id` attribution is actually persisted. It is missing for the same reason.

### P1

#### F-02 — The failure reason is hidden from the user

- The run page says *"No failure or stop reason was recorded"* even though the backend has a precise, user-safe reason.
- The failure is not a timeline event, so the Timeline shows "0 events · The snapshot contains no phase events".
- The primary CTA on the failed card is **Open Story Analysis**, which leads to an empty phase ("No result recorded for this phase").
- **Recommendation:**
  - Record a terminal failure with a category and plain-language message, for example *"Prompt selections could not be frozen at launch. Relaunch, or ask an admin to check prompt availability."*
  - Show it on the Overview card and as a Timeline event.
  - Change the CTA to the recovery action rather than an empty phase.

#### F-03 — Both recovery paths from a failed launch dead-end

- **Create new test from this run** on the failed run produced the alert *"The stored setup cannot be copied safely"*, with no reason and no next step.
- **Re-run selected plan** produced *"No phases selected."*, with no way to pick phases from that message.
- The tester's only option is to rebuild the test from scratch. On a completed run the clone does work; see F-10.
- **Recommendation:**
  - When a run fails before any phase starts, offer **"Edit and relaunch"**, which reopens the original draft payload.
  - Make every refusal name its reason and the next step.

#### F-04 — "Logs" for a run shows unrelated entries outside the run's time window

- The Logs page opens with *Investigating run · Workflow re-check 26.3.3*. The URL carries `from=2026-10-22T21:19:44Z&to=…21:19:45Z`.
- It still lists three **Dec 31, 2025** entries from `payment-svc` and `checkout-api` ([14](screens/14-run-logs-unrelated.jpg)).
- No time-window chip appears selected, yet the bar says "2 active filters".
- A tester would reasonably read these as the cause of the failure.
- **Recommendation:** apply the run's window. If the source is sample or fixture data, label it as such. If nothing matches, show an empty state ("No log entries recorded for this run's window").

#### F-05 — The sticky readiness footer takes up the working area

| Viewport | Footer height | Share of screen |
|---|---|---|
| 1440×900 | 123 px | 14% |
| 1024×768 | 207 px | 27% |
| 768×1024 | 207 px | 20% |

- At 1024×768, only the Title field is above the footer on a fresh test. Request and the topology section start beneath it ([21](screens/21-fresh-wizard-1024.jpg), [22](screens/22-fresh-wizard-768.jpg)).
- The same two-line disclaimer (*"Plan is valid. Execution connection metadata is valid. Provider connectivity, credentials and workload/release eligibility are checked later…"*) repeats on every step ([02](screens/02-config.jpg), [05](screens/05-prompts.jpg)).
- On Review the disclaimer appears twice, in the Readiness card and in the footer ([06](screens/06-review.jpg)).
- **Recommendation:**
  - Collapse the footer to one row: `Step 2 of 6 · ✓ Ready` or `Step 1 of 6 · 3 items need attention ▾`, with the issue list in a popover.
  - Show the server-recheck disclaimer once, on Review.
  - Target a footer under 64 px.

#### F-06 — Results are not shown against the acceptance thresholds the user entered

- The tester enters P95 ≤ 500 ms, error rate ≤ 5% and a minimum TPS in Config.
- After the run:
  - Overview shows only *Test acceptance: Passed — "simulated run; KPIs are synthetic"*, with no numbers ([17](screens/17-run-complete-overview.jpg)).
  - The Reporting review gate shows the KPIs only as text inside the agent's output: `error_rate=0.004, p95_ms=212, tps_avg=24, vusers_peak=10` ([18](screens/18-review-gate.jpg)).
  - Nothing compares the observed values with the requested ones.
- This is the core deliverable of a load test.
- **Recommendation:**
  - Add a **Requested vs observed** KPI table to Overview and to the Reporting/Execution review gate. Columns: metric, threshold, observed, pass/fail.
  - Mark the source (simulated or engine).
  - The data already exists in `results.json`.

### P2

#### F-07 — Config has no cross-field validation and weak duration defaults

- A ramp of 600 s with a 60 s duration is accepted.
- Duration defaults to 2 s (a simulator value) and allows 0.
- Durations are entered in seconds only. APEX Load uses a duration field with units.
- **Recommendation:**
  - Validate that ramp ≤ duration, both inline and in the readiness list.
  - Use realistic defaults per engine.
  - Reuse Load's duration input.

#### F-08 — The Scope participant row is unstyled

- The backend row is a `fieldset.wizard-field` with the **browser-default 2 px groove border**.
- Labels have no spacing, and the radio buttons sit flush against their text.
- **Remove participant** renders as a full-width outline button ([03](screens/03-scope-participant-row.jpg)).
- **Add backend** stays visible after the single allowed backend is added. The section title says "exactly one".
- **Recommendation:**
  - Render each participant as a card with a two-column grid for Project, Application and Environment, and a role segmented control.
  - Make Remove a small text or icon button.
  - Hide or disable Add when the layer's limit is reached.

#### F-09 — Scope readiness messages mislead

- **"Project is required"** stays in the list after Project is chosen in the backend row. It actually clears only when **Primary system under test** is ticked. Rename it, for example "Choose which system is primary".
- **"Retry participant verification"** is always shown, with no explanation of what it verifies or when it is needed.
- The step intro still says *"choose the project, application, and target environment"*. That predates the topology model.
- The Environment picker does not show target-approval state. PERFHYPER2 is not approved, which matters for non-simulated engines.
- *"Draft saving requires project-wide access"* is shown to a global admin before any project is chosen.

#### F-10 — Cloning an older run asks the user to rebuild its selection by hand

- The clone shows *"Legacy selection: demo / 02971bd36e5347dcb7d8bebc7abcf34b / no environment. Add it below and explicitly assign its layer and primary role."* ([19](screens/19-clone-legacy-selection.jpg))
- That message shows a raw application ID instead of "ACC", and offers no one-click way to use the old selection as the backend.
- The clone also keeps the source title unchanged, so two runs in the list now share the same name ([20](screens/20-relaunch-failed.jpg)).
- **Recommendation:**
  - Show readable names.
  - Add a **Use as backend (primary)** button.
  - Suffix the title, for example "(re-run 2)", or prompt the user to rename.

#### F-11 — The primary CTA overlaps the status sentence on run cards

- **Open Postmortem**, **Open Story Analysis** and **Review pending decision** are `display: inline` with vertical padding, so they overlap the sentence above them.
- Measured overlap: 7 px ([17](screens/17-run-complete-overview.jpg), [10](screens/10-run-failed-overview.jpg)).
- **Fix:** make the CTA `inline-flex` or `block` with a top margin.

#### F-12 — Run-detail navigation changes context unexpectedly

- A run that is **Awaiting review** opens on Overview, not on its **Review** tab, even though Review is listed first.
- The Review tab navigates to the review-queue page. The left list appears and the page title changes from "Run detail" to "Review".
- Workspace changes the page title to "Phase detail", and Timeline changes it to "Run timeline".
- The header action buttons move between right-aligned and wrapped-left depending on title length ([10](screens/10-run-failed-overview.jpg) vs [17](screens/17-run-complete-overview.jpg)).
- **Recommendation:**
  - Keep one page title ("Run detail") and one fixed action-bar position.
  - Default to Review when a decision is pending.
  - Open the queue view only from **Needs review**.

#### F-13 — The Tests list shows raw IDs and breaks its layout

- The **Application** column shows `02971bd36e5347dcb7d8bebc7abcf34b` instead of "ACC".
- The Progress cell wraps "○○○○○○○Phase plan not recorded for this run" across three monospace lines.
- **Open run** wraps onto two lines ([15](screens/15-tests-list.jpg)).
- In split view, status chips in the left list are cut off ("Orche…", "Orchestr…") ([16](screens/16-tests-split-workspace.jpg)).

#### F-14 — Internal terminology is exposed in the wizard

- Phase ownership is labelled **LangGraph** / **HITL**. The toggle also changes its own label *and* `aria-pressed`, so the state is encoded twice.
- Prompts says "each LangGraph phase", shows "default (default)", uses "Environment Context" (inconsistent casing), and starts each tab name with "✓".
- **Recommendation:** use "Agent runs it" / "Person completes it", and remove the framework name from user copy.

#### F-15 — The ticket query translation is opaque

- A natural-language ticket search is shown as a read-only **hex-ID chip**. The user cannot see or edit the query that was actually run.
- Selected-ticket chips show keys only, with no summary.

### P3

- **F-16** Knowledge: acceptance criteria parsed from uploaded documents are not reconciled with the structured thresholds in Config. There is no "doc says P95 ≤ 300 ms; Config says 500 ms" warning.
- **F-17** The step tabs carry no per-step state (✓, or an issue count). Readiness lives only in the footer.
- **F-18** Review: the Readiness card is a narrow column 9 lines tall that wraps a paragraph meant for a wide layout ([06](screens/06-review.jpg)).
- **F-19** Every navigation runs a whole-page fade of about 1–2 s that dims the page and looks like a disabled state.
- **F-20** Review gate: the "Accepting continues…" explanation appears twice; **Abort** wraps alone onto its own line; the "Agent evidence is unavailable in this review preview" notice takes space without offering a link to the evidence.

---

## What works well (keep)

- **Engine cards** in Config. They are clear, and Simulated is honestly labelled as the default.
- **Structured workload with honest disclosure.** Unsupported fields are called out, range validation runs inline and in the footer, and the help text explains that the structured values override the request text.
- **Review policy** (Review every phase / Run automatically / Custom) with one checkbox per gate, each with an accurate name for assistive technology.
- **Tickets:** the results table with checkboxes stays in sync with add-by-key.
- **Knowledge:** upload, parse and preview work, and unsupported types (CSV) are rejected with a clear message.
- **Prompts:** the vertical tab rail is well built (10 tabs, `aria-controls`, one tab stop, roving focus).
- **Review step:** comprehensive and careful with claims ("Requested inputs, not measured results…").
- **Keyboard:**
  - The step tabs use roving tabindex and arrow keys, and change step.
  - Readiness items are buttons that move focus to the field that needs attention (verified: "Title is required" → `#wizard-title`).
  - Every form control has a programmatic label.
- **Responsive:** no horizontal scrolling at 1024 or 768. At 768 the sidebar collapses to an **Open navigation** button.
- **Review gate copy:** it states exactly what Accept, Request changes and Abort do, and that accepting a phase does not approve a release.

---

## Completeness check — what a tester needs vs what exists

| Need | Status (2026-10-22) | Status (2026-10-23) |
|---|---|---|
| Define the system under test (layers, primary, traffic entry) | ✓ exists · F-08/F-09 | ✓ F-08/F-09 fixed |
| Choose engine and workload, with thresholds | ✓ · F-07 | ✓ F-07 fixed: ramp ≤ duration, unit choice |
| Attach tickets and documents as context | ✓ · F-15/F-16 | ✓ F-15/F-16 fixed |
| Choose prompts per phase | ✓ | ✓ |
| Choose which phases are agent-run vs human-completed, and review gates | ✓ · F-14 | ✓ F-14 fixed |
| Save and resume drafts | ✓ | ✓ |
| Launch | **✕ F-01** | ✓ 7/7 from the wizard on `langgraph dev` and on the production Postgres runtime |
| Watch live progress | not observable (F-01); the phase strip exists | ✓ seen on the production stack: "Running, 5/6", then "7/7 phases succeeded" |
| Decide at review gates | ✓ (older runs) | ✓ on new runs; a pending decision opens in Run detail ([after-04](screens/after-04-decision-first-review.jpg)) |
| See KPIs against thresholds | **✕ F-06** | ✓ Requested vs observed table ([after-03](screens/after-03-run-complete-kpis-1024.jpg)) |
| Understand a failure | **✕ F-02, F-04** | ✓ reason on Overview and Timeline; run-scoped Logs ([after-05](screens/after-05-failed-run-relaunch.jpg)) |
| Recover: edit and relaunch / re-run phases | **✕ F-03** · clone works only for completed runs (F-10) | ✓ **Edit and relaunch** for failed runs; clones keep their topology (F-10) |
| Compare runs | `Compare` exists on the Tests list (not exercised here) | ◐ Exercised: select 2–4 runs, then per-phase timing, KPI deltas against a chosen baseline, and artifact/warning counts ([after-06](screens/after-06-compare.jpg)). **Gap:** requested thresholds are not shown, so you can't see whether a delta crosses an acceptance limit. |
| Hand off to APEX Load for real execution | engine card and "Load integration" links exist (not exercised; Simulated only) | **Not exercised.** It needs a live APEX Load connection; only the Simulated engine has been run. |

---

## Recommended order

1. **F-01** — freeze the manifest in the launch facade, and add a facade-to-runtime integration test. Also verify actor attribution.
2. **F-02 / F-03** — record and show failure reasons, and add *Edit and relaunch*.
3. **F-05** — compact the footer (the biggest everyday usability win).
4. **F-06** — add the Requested vs observed KPI table.
5. **F-08 / F-09 / F-07** — Scope row styling and copy, and Config validation.
6. **F-04, F-10–F-15** — consistency and polish.
7. P3 items.

---

## Method and limits

- I walked through the wizard twice: a fresh draft, and a clone of completed run 26.3.1. I launched both.
- I inspected failed, completed and awaiting-review runs across Overview, Workspace, Timeline, Review and Logs.
- I diagnosed F-01 from the backend log, the code path, and a **read-only copy** of the dev server's run store. The store was not modified.
- Only the Simulated engine was used; APEX Load and LoadRunner execution were not exercised. The dark theme was not re-checked.
- The production runtime was not tested (see F-01). It was tested on 2026-10-23; see [PRODUCTION_RUNTIME_CHECK.md](PRODUCTION_RUNTIME_CHECK.md).
- No credentials or keys appear in this report or its screenshots.
- Earlier wizard screenshots (01, 02, 05, 06, 11) were captured at 1024×768. Run-page screenshots were captured at 1440×900.

### Screenshots

| File | Shows |
|---|---|
| [01-scope](screens/01-scope.jpg) | Scope step, footer over content |
| [02-config](screens/02-config.jpg) | Config step: engine cards, footer |
| [03-scope-participant-row](screens/03-scope-participant-row.jpg) | Unstyled backend fieldset (F-08) |
| [05-prompts](screens/05-prompts.jpg) | Prompt rail |
| [06-review](screens/06-review.jpg) | Review: narrow readiness card, duplicate text |
| [10-run-failed-overview](screens/10-run-failed-overview.jpg) | Failed launch, hidden reason (F-01/F-02) |
| [11-run-failed-1024](screens/11-run-failed-1024.jpg) | Same at 1024×768 |
| [12-run-failed-workspace](screens/12-run-failed-workspace.jpg) | All phases "Unknown" |
| [13-run-failed-timeline](screens/13-run-failed-timeline.jpg) | 0 events |
| [14-run-logs-unrelated](screens/14-run-logs-unrelated.jpg) | Logs outside the run window (F-04) |
| [15-tests-list](screens/15-tests-list.jpg) | Raw application ID, wrapped cells (F-13) |
| [16-tests-split-workspace](screens/16-tests-split-workspace.jpg) | Split view with cut-off chips |
| [17-run-complete-overview](screens/17-run-complete-overview.jpg) | "Passed" with no KPIs (F-06), CTA overlap (F-11) |
| [18-review-gate](screens/18-review-gate.jpg) | KPIs as a raw string in the review gate |
| [19-clone-legacy-selection](screens/19-clone-legacy-selection.jpg) | Raw-ID legacy selection (F-10) |
| [20-relaunch-failed](screens/20-relaunch-failed.jpg) | Second launch fails identically |
| [21-fresh-wizard-1024](screens/21-fresh-wizard-1024.jpg) | 207 px footer at 1024×768 (F-05) |
| [22-fresh-wizard-768](screens/22-fresh-wizard-768.jpg) | Tablet layout |
