# Lead reviewer findings — holistic UX review, 17 October 2026

These are the findings the lead reviewer observed first-hand in the browser and corroborated
against source or the `/v1` API, kept separate from the five area sweeps so the provenance of
each finding is clear. Everything here was reproduced at least twice or confirmed against the
API; corrections made after independent verification are marked inline.

Environments: demo (production build, `dataMode: demo`, ACC add-a-line v2.0, `:4173`);
connected (production build + same-origin API proxy reproducing the documented deploy contract,
real API-key gate, `:4175`, with an isolated second origin on `:4177` for role switching);
backend `langgraph dev` on `:2024` with `APEX_CYCLES_ENABLED=true`.

See [REPORT.md](REPORT.md) for the synthesis, [VERIFICATION.md](VERIFICATION.md) for the
independent reproduction pass.

---

## H-01 — "FAILURES" on Home counts orchestration errors, beside a panel reporting 0 test failures

API truth (GET /v1/pipelines?limit=50, admin, connected):
  total 39 · thread_status {interrupted:20, error:4, idle:15} · execution_result {null:39}

UI, same viewport, same fleet:
  FAILURES tile          4   caption "in the loaded recent runs"   tone=danger
  TEST RESULTS panel     Passed 0 / Not available 39 / Failed 0    caption "up to the latest 50 loaded runs"
  EXECUTION HEALTH       Phases succeeded 12 / Phases failed 0

Source: features/home/homeLogic.ts failedRuns() = execution_result==='failed' || thread_status==='error'
        features/home/HomePage.tsx:152 failures = failedRuns(items, items.length)
        features/home/HomePage.tsx:201 failedCount = execution_result==='failed' only
        features/home/HomePage.tsx:241 <MetricCard label="Failures" hint="in the loaded recent runs">

---

## R-01 — A failed run presents its failing phase as "Pending", keeps a live-refresh chip, and never says why

Run: 01a14ad7-34e0-7160-94d9-fe5cb8470596 "ACC add-a-line peak certification — candidate B"
Connected mode, admin, apex-light, 1440x900.

API truth (GET /v1/pipelines/{id}):
  thread_status: "error"
  phase_strip[4]:                    {phase: execution, status: "running"}
  values.phase_results.execution:    {status: "pending"}
  no errors[] recorded on any phase

UI (/runs/{id}/phases/execution):
  header            ORCHESTRATION FAILED · Test result: Not available · 4/7 phases finished
  phase strip       Execution — PENDING          <- the phase the run died in
  live chip         SNAPSHOT REFRESH ACTIVE      <- on a terminal run
  STATUS            Pending     DURATION —   STARTED —
  "What happened"   "No operator-readable summary has been recorded for this phase yet."
  Pipeline Log      "No live activity for this phase yet. Activity streams in while the run is executing"
  Overview          "4 of 7 selected phases finished. Execution evidence is summarized separately below."
  Evidence coverage "Evidence may still be collecting or the run may not have produced any."

So the same phase carries three different statuses (running / pending / dead), the one the UI
chooses is the most innocuous, and three strings use "yet"/present-continuous for a terminal run.
The failure reason is not reachable anywhere in the UI.

Also on the same screen: header says Application "Not selected", Run context says "Not recorded".

---

## L-01 — Every approval is attributed to "unknown"  [P1, governance]

Route /runs/{id}/phases/story_analysis?tab=details, connected, authenticated as apex-admin.

UI renders, verbatim:
    APPROVALS
    PROMPT_REVIEW   APPROVE   unknown   Oct 17, 01:25:25 PM

API agrees — every approval record carries actor:"unknown":
  01a14ad6-1752…  {"gate":"prompt_review","action":"approve","actor":"unknown","at":"…17:25:25Z"}
  01a14ad6-1f91…  {"gate":"prompt_review","action":"skip_phase","actor":"unknown","at":"…17:40:43Z"}

Both decisions were made by me in the browser over an authenticated session whose consumer the
backend resolves on every request (/v1/system/info returns consumer {name:"apex-admin",role:"admin"}).

The governance reviewer's first question — who approved this gate — has no answer anywhere in
the product. This is the single highest-impact finding for that persona.

Secondary on the same panel:
- The gate name renders as the raw enum "PROMPT_REVIEW"; every operator-facing surface calls the
  same thing "Review prompts".
- revise/discuss decisions are not recorded in approvals[] at all (verified: after a successful
  "Re-run with Changes", story_analysis.approvals is []), so a reviewer cannot see that a human
  sent the agent back.

## R-02 — Phase DURATION counts the time a human spent deciding  [P2]

Same screen: STATUS Complete · ATTEMPT 1 · DURATION 16m 58s · STARTED Oct 17, 01:08:27 PM.
The stub agent runs in well under a second; the gate sat open from 01:08:27 to my approval at
01:25:25. "Duration" is wall-clock from phase start to phase end, so it silently includes human
wait time. In a performance-testing product this is the wrong default reading, and nothing on
the screen says which it means. Home's AVG DURATION tile does disclose this ("creation to last
update, not execution duration"); the phase panel does not.

## G-03 — A revise on your own gate is reported as "actioned elsewhere"  [P2]

Steps: open the phase_review gate on 01a14ad6-23a2-7c32-8bee-78a027fb5d2a, click
"Request changes", type instructions, click "↻ Re-run with Changes".

Observed: the queue row greys out and is labelled "actioned elsewhere"; the pending badge drops
15 -> 14; no decision receipt appears; selection jumps to queue index 0.

API after the action: thread_status still "interrupted", pending_gate is a NEW interrupt
(c567f1f9dbb94bf6edc1ef1964f95f50, kind phase_review, same phase) replacing 266ecf8d….
So the gate reopened correctly and still needs a decision.

On reload the row returns as a normal open row and the badge returns to 15, so the state is a
transient client-side mislabel — but until the operator refreshes, a gate that is still waiting
has vanished from the queue, and they have been told another operator decided it.

gateModuleContract.ts specifies the intended behaviour: "modify/discuss/revise emit no terminal
outcome for the queue: the gate reopens … selection stays put", and reserves "actioned elsewhere"
for {type:'superseded'} — a 409 CAS loss. The GateModule is keyed by interrupt_id, so when the
revise produces a new id the old instance sees its interrupt vanish and reports superseded.

This also poisons the genuine concurrency signal: "actioned elsewhere" is exactly the message
that must be trustworthy when two operators really do collide.

Fix: distinguish "my own action produced a new interrupt for this thread+phase" from "the
interrupt vanished and someone else's decision is recorded", before emitting superseded.

## Minor, same area
- Clicking "Request changes" opens the instructions field but does not focus it; the keyboard
  path (invoke('revise')) does focus it. Same action, two behaviours.
- "↻ Re-run with Changes" has U+21BB in its accessible name, like "▶ Execute Phase" (A-06).

---

## G-02 — A prompt edit at a gate is discarded silently by one click, with no guard  [P1]

Connected, admin, apex-light, 1440x900.
Run 01a14ad6-1b76-7d40-943e-0bf969efa44f, prompt_review gate ddbedf2b0b74e7b1e96b1d674d974a76.

Steps
1. Open the gate, click into SYSTEM PROMPT (CodeMirror), append a line of operator instruction.
   -> an "edited" chip appears beside the provenance chip and the primary action changes from
      "▶ Execute Phase" to "Save Edit & Re-review". Both are good.
2. Without saving, click any other row in the Needs review queue.
   -> navigates away immediately. No dialog, no warning, no toast, no retained-draft marker.
3. Navigate back to the same gate URL.
   -> probe: edit gone (/UX REVIEW EDIT/ absent from the editor), "edited" chip gone,
      primary action back to "▶ Execute Phase".

The operator's hand-written modification to the prompt that will drive the agent is lost to a
single click, with no way to recover it.

This is not a missing capability — the app ships one and uses it elsewhere.
components/NavigationGuards.tsx implements a priority-ordered blocker, and
components/useNavigationBlocker.ts is consumed by exactly four surfaces:
    features/cycles/ProviderScriptImport.tsx:38   "Unsaved provider import"
    features/cycles/CycleHandoff.tsx:33           "Unsaved cycle follow-up"
    features/cycles/CycleEditor.tsx:107           "Unsaved deliverable changes"
    features/admin/ConsumerKeyLifecycle.tsx:109   one-time API key
Nothing in hitl/, features/runs/, features/approvals/ or features/new-test/ registers one.

ADR-0006 states the resume design "preserves the operator's draft for retry" on network
failure. The same draft is not preserved against an ordinary click.

Fix: register a guard from hitl/GateModule.tsx (or PromptTabsEditor) whenever the editor is
dirty, reusing the Cycles dialog copy; and keep the dirty buffer per interrupt_id so returning
to the gate restores it.

Related, same action bar: "Skip Phase" remains enabled while an edit is pending and would
discard it just as silently.

---

## A-01 — Inbox queue navigation (j/k) never moves focus and is silent to assistive technology

Route /approvals (redirects to /runs/{id}/reviews/{interruptId}?source=queue), connected, admin.

Markup is correct:
  ul.approvals-queue[role=listbox][aria-label="Needs review queue"]
   > li.approvals-row[role=option][aria-selected][aria-label="CAS demo. Review phase results. Story Analysis."]
  roving tabindex is maintained (selected row tabIndex=0, others -1)

Defect: pressing j moves aria-selected from row 0 to row 1 and swaps the entire right-hand
Review Workspace, but document.activeElement stays on <main class="main-content">.
The selected option never receives DOM focus, and the only aria-live regions present are the
AWAITING APPROVAL status badge and CodeMirror's internal .cm-announced divs — neither announces
the queue change. A screen-reader user pressing j/k hears nothing at all.

Secondary: the option's accessible name omits project, age and thread id, so the two
"CAS demo" rows (019eb83e-7b01… and 019eb83e-cbdd…) are indistinguishable by name,
although the visible row shows the differing UUID.

Fix: on selection change, call .focus() on the newly selected li[role=option]; include the
run's project and waiting time in the option's aria-label.

Verified by DOM probe, not by eye.

## A-02 — After a decision the queue selection jumps to the top, not to the next open gate

Observed: selection was on queue index 3 ("ACC add-a-line — prompt gate approve"). Clicked
"▶ Execute Phase". The row greyed to "actioned", the badge went 17 -> 16, a decision receipt
appeared ("Approved prompts and started the phase for … · 1:25:24 PM"), and the selection
moved to index 0 (CAS demo, 019eb83e-7b01…) — the top of the queue — not index 4.

features/approvals/gateModuleContract.ts documents the intended behaviour:
  "{type:'resumed', action} … -> gray the queue row ('actioned'), auto-advance the selection
   to the next open gate."

Needs a second observation to rule out a race.

## C-01 — Two clock formats in the same screen
Home / run tables: "Oct 17, 01:07:24 PM" (zero-padded 12-hour)
Decision receipt:  "1:25:24 PM"          (not padded)

### A-02 CONFIRMED (second observation)
After clicking "Skip Phase" on queue index 5 the selection again moved to index 0, not index 6.
Receipt: "Skipped the phase and continued the run for ACC add-a-line — prompt gate skip · 1:40:42 PM".
Badge 16 -> 15, row greyed and labelled "actioned".
Two observations (approve from index 3, skip from index 5), both landing on index 0, so this is
reproducible and not a race. With 17 open gates an approver must re-traverse the queue after every
decision: clearing it costs O(n^2) keystrokes rather than O(n).
gateModuleContract.ts documents the intended behaviour as "auto-advance the selection to the next
open gate".

### G-01 CONFIRMED by execution
"Skip Phase" fired immediately on a single click. No confirmation dialog, no typed confirmation,
no undo — against "Abort" in the same action row, which requires typing ABORT.

---

## A11Y-04 — CONFIRMED BY ME. Keyboard trap in the prompt editor that silently corrupts the prompt  [P0, WCAG 2.1.2]

Connected build, admin, apex-light, 1440x900.
Run 01a14ad6-3c64-74d1-9861-2e80b657453d, prompt_review gate 27be803f8fce3d52292d97d56092e146.

Baseline: system prompt 735 chars, begins "You are the APEX story_analysis agent."

Tab trail from the run title heading (13 presses to reach the editor):
  Logs -> Load integration -> Decision -> Overview -> Workspace -> [Timeline]
  -> ... -> SYSTEM PROMPT tab -> ... -> DIV.cm-content   (role="textbox", tabIndex=-1)

Then SIX further Tab presses, measured:
  focus after 6 Tabs : DIV.cm-content      (never left)
  still in editor    : true
  prompt length      : 735 -> 747  (delta +12 = 6 Tabs x 2 spaces)
  prompt now begins  : "            You are the APEX story_analy"
  primary action     : flipped from "▶ Execute Phase" to "Save Edit & Re-review"

So a keyboard-only approver who tabs past the editor cannot get out, silently inserts leading
whitespace into the prompt that will drive the agent, and flips the gate into an edit state
they never requested.

Escape does release it — Escape then Tab lands on... "Save Edit & Re-review".
  escapedTo: BUTTON.btn.btn-primary.btn-sm | "Save Edit & Re-review"
  12 inserted spaces still present (747)
  any on-screen hint about Escape: NONE (no .editor-hint / .keyboard-hint in the DOM)

That makes it worse than a plain trap: the first control after escaping is Save, so the natural
next keystroke commits the corrupted prompt as a human-authored run override.

WCAG 2.1.2 allows a trap only if the escape method "is advised to the user". It is not, so this
fails regardless of Escape working.

Cause: PromptTabsEditor.tsx mounts <CodeMirror> from @uiw/react-codemirror without
indentWithTab={false}; that wrapper defaults it to true, binding Tab to indent.
Output gates are unaffected — their composer is a plain <textarea>.

Fix: pass indentWithTab={false}; if Tab-to-indent is wanted, keep it and add a visible,
persistent hint ("Press Escape then Tab to leave the editor") plus an aria-describedby pointing
at it. Either way the gate must not enter the edited state from navigation keystrokes alone.

Note: the "edited" chip did NOT appear for this whitespace-only change although the primary
action did flip to Save — worth checking whether the chip and the dirty flag use different
predicates.

## A-01 — CORRECTED. My earlier measurement was taken at a different moment.

I originally recorded that j/k "never move DOM focus", measured as activeElement =
main.main-content. Re-measured carefully on /approvals in the connected build:

  focus before j : H2.run-detail-title
  focus after  j : H2.run-detail-title
  focus inside the queue      : false
  selected row focused        : false
  selected row                : "CAS demo Jun 11 019eb83e-cbdd-…"

So focus DOES sit on a heading that re-renders per selection (the route changes, AppShell
focuses main, and the run title heading takes focus), not on main.main-content as I first wrote.

The accurate finding: focus never enters the queue and never follows the selected
li[role=option]. A screen-reader user parked on that heading hears the newly selected run's
TITLE, but nothing about queue position, gate kind, phase, or how many reviews remain — and the
roving tabindex on the options is therefore never exercised. Combined with the confirmed absence
of any live region for the queue, the practical effect is close to what I first described, but
the mechanism is different and the report must say so.

---

## COR-02 — VERIFIED MYSELF, and it is worse as baseline  [P1]
Screenshot 14-compare-failed-run-ranked-best.jpg
Demo mode, /runs/compare?ids=run-aborted,run-baseline-a,run-conditional, run-aborted as baseline.

run-aborted's own column says: ORCHESTRATION FAILED · EXECUTION FAILED (attempt 2) ·
POSTMORTEM RUNNING · RESULT FAILED.
Its KPI cells are tinted GREEN and labelled "Baseline":
    TPS AVG 82.7 · P95 0 ms · ERROR RATE 0.00% · VUSERS PEAK 300
The two runs that actually completed are tinted PINK and labelled "Worse" against it:
    baseline-a  P95 2410 ms  "+2410 ms (percentage not available) · Worse"
                ERROR RATE 0.60%  "+0.60 percentage points (percentage not available) · Worse"
    run-conditional P95 3650 ms "+3650 ms … · Worse"

So a run whose execution failed and which collected no results supplies 0 ms / 0.00% as the
reference, and real measurements are rendered as regressions against it. A release manager
reading this is pointed at the failed run.

Sharpest detail: the delta text says "percentage not available" because the baseline is 0, so
the code already knows 0 cannot be divided by — the arithmetic guard exists, the semantic guard
does not. Nothing checks whether execution succeeded before treating a KPI as a measurement.

Fix: gate KPI participation on the execution phase having succeeded (or on an explicit
"collected" flag); render uncollected KPIs as "—"/Not collected and exclude them from
best/worst ranking and from baseline eligibility, as the UX contract's "missing assessment
remains unknown" requires.

## COR-03 — VERIFIED MYSELF  [P1]
Demo /runs/run-active header, verbatim:
    "ACC add-a-line active ramp  AWAITING APPROVAL  Test result: Failed  4/7 phases finished"
Execution has not finished and the run is sitting at a gate, yet the header publishes a
definitive failed test result. I had independently recorded the same row on /runs earlier:
    "ACC add-a-line active ramp | run-active | Execution | 4/7 | … | FAILED | RUNNING"

## COR-01 — VERIFIED MYSELF  [P2]
Same three-run comparison: run-conditional's header chip reads "NOT RUNNING" (green) on compare,
while /runs shows the same run as "ORCHESTRATION COMPLETE" and its test result as FAILED.
Three screens, three different characterisations of one run.

Also visible on compare and not yet filed: POSTMORTEM shows "RUNNING" with duration "—" on a run
whose header says ORCHESTRATION FAILED — the same terminal-state defect as R-01, now in compare.

---

## Walkthrough f (concurrency) — PASSES. Record this as a strength.
Screenshot 15-concurrency-superseded-correct.jpg

Setup: the same prompt_review gate (run 01a14ad7-0e5b-7b80-8425-be56c37738a4,
interrupt 90d47e63595becb6ac0596db552d8c3c) open in two independent browser tabs against the
connected production build.

Sequence: tab A clicked "▶ Execute Phase"; 4 s later tab B, still showing the stale gate,
clicked "Skip Phase".

Tab B result — correct in every respect:
  - decision receipt: "Decision was completed elsewhere for ACC add-a-line — concurrent
    decision · 2:07:41 PM"
  - the queue row greyed and was labelled "actioned elsewhere"
  - pending badge 15 -> 14
  - no error toast, no crash, no retry prompt

API confirms exactly one decision landed:
  story_analysis.approvals = [ {gate:"prompt_review", action:"approve",
                                actor:"unknown", at:"2026-10-17T18:07:38Z"} ]
  attempt 1, status succeeded.
The skip was rejected by the compare-and-set route and left no trace. Nothing was decided twice.

This is the pessimistic-resume design from ADR-0006 working as specified, and it is the
strongest part of the HITL implementation.

### But it makes G-03 worse, not better
The identical treatment and the identical phrase — greyed row, "actioned elsewhere", badge
decrement — is used for BOTH:
  (a) a genuine CAS loss to another operator   [correct, as here]
  (b) your own revise producing a new interrupt id   [wrong, see G-03]
So the one signal that must be trustworthy in a multi-operator product is also emitted when
nobody else was involved. An approver cannot tell "someone else decided this" from "my own
change is in flight".

### Third variant of R-01 seen here
After the run later errored: thread_status "error", current_phase "script_scenario" (which the
strip reports as succeeded), execution "pending". current_phase points at a phase that finished
while the phase that actually stopped the run reads pending.

---

## S-01 — The live stream never attaches for short runs; the Activity panel promises updates that never arrive  [P2]

### Connected mode
Launched "SSE transport probe C" from inside the page and SPA-navigated to its run detail in the
same tick, then sampled the transport chip every 400 ms for 18 s.
API: thread busy 18:12:25.59 -> 18:12:33.44 (7.85 s), finishing idle / execution_result "passed".
Chip transitions observed: exactly one — `idle | SNAPSHOT REFRESH ACTIVE`. Never connecting,
never live.

Cause (source): streaming/useActiveRun.ts discovers the run to stream by polling
client.runs.list every ACTIVE_RUN_POLL_MS = 5_000 ms, and is enabled only once the snapshot
reports thread_status 'busy'. A run shorter than roughly one poll interval plus a snapshot
round-trip is over before the page finds a run id to attach to.

### Demo mode — unambiguous, and worse
/runs/run-active ("ACC add-a-line active ramp"), the ACC dataset's flagship live run:
  grid STATUS            RUNNING
  phase                  Execution / EXECUTING
  transport chip         idle | SNAPSHOT REFRESH ACTIVE
  Pipeline Log           "No live activity for this phase yet. Activity streams in while the
                          run is executing; the other tabs show the durable snapshot."
  jump-to-live control   not present in the DOM
The run is executing. The copy says activity streams in while executing. It does not.
The demo that exists to show the product cannot demonstrate live monitoring.

### What this blocks — record in the coverage matrix
Walkthrough (e) could not be completed. Reconnect with joinStream(last_event_id),
jump-to-live, and loss/duplication of events were NOT verified, because no live stream could be
established in either data mode. The reconnect machinery itself reads correct in source
(jittered backoff, healing snapshot refetch, monotonicity guard) and the correctness reviewer
separately confirmed the ADR-0006 snapshot-is-canonical invariant holds — but the UI path was
not exercised.

Caveat to state in the report: with the simulated engine every phase completes in seconds. A
real LoadRunner/APEX Load execution runs for minutes, where the 5 s discovery poll would attach
normally. The severity is therefore environment-dependent for connected mode; the demo-mode
failure is not.

Fix: attach the stream from the launch response's run_id (POST /v1/pipelines already returns
`run_id` and `stream_url`) instead of rediscovering it by poll; shorten the first discovery
probe; and make the Activity panel's empty copy conditional on the thread actually being busy.

## S-02 — Live-transport status is well modelled but never announced  [P3]
features/runs/LiveStatusChip.tsx defines six named states with distinct labels and explanatory
title text: idle "Snapshot refresh active", connecting "Connecting live updates",
live "Live updates connected", reconnecting "Reconnecting live updates",
ended "Recorded snapshot", error "Live updates unavailable".

Keeping transport state as its own named fact, separate from orchestration status, is exactly
right and should be recorded as a strength.

Two gaps:
1. Each state's meaning lives only in a `title` attribute — not keyboard reachable, not reliably
   announced.
2. The component deliberately omits role="status" (documented in its own comment: "the run
   rail's gate banner owns that landmark … the chip's churn would spam screen readers"). The
   trade-off is defensible, but the consequence is that losing the live stream is announced to
   nobody. The brief asks specifically for stream-status announcements.
Fix: keep the chip silent, and announce transport transitions once, debounced, in the existing
run-rail status region instead.

---

## Walkthrough g (re-run through preflight) — largely PASSES. Record as a strength.
Screenshot 16-preflight-rerun-prerequisites.jpg
Connected, admin. Run 01a14ae9-2277-7522-a0e2-ec36bb937315 (idle, execution_result "passed").

"Re-run selected plan" opens a modal titled "Re-run phases" with the explanation
"Prerequisites resolve against phases earlier in this plan or already succeeded on this thread."
Seven phase toggles, then a per-phase readiness list:
  STORY ANALYSIS     No prerequisites.
  TEST PLANNING      Runs after Story Analysis in this plan.
  ENV TRIAGE         No prerequisites.
  SCRIPT & SCENARIO  Runs after Test Planning in this plan.
  EXECUTION          Runs after Script & Scenario in this plan.
Gate mode: Inherit defaults / All gated / All auto. Actions: Cancel / Start phases.

Missing-prerequisite case handled well: deselecting Execution did not block the re-run; the
Reporting row changed to "Will reuse Execution artifacts (attempt …)". It explains the
consequence instead of refusing, which is the right call.

Accessibility: the dialog is correctly named — aria-labelledby resolves to the heading
"Re-run phases" (an earlier probe that read ":r1:" was reading the id, not the name).

Not retested: the 2026-09-14 P1 "Rerun expands scope by default" described a run whose original
plan was a SUBSET whose re-run preselected all seven phases. This run's original plan was all
seven, so preselecting all seven matches the button's own label ("Re-run selected plan") and is
correct here. Retesting that finding needs a successfully completed subset run, which this
environment could not produce. Report it as not retested rather than fixed or regressed.

No re-run was started; the dialog was cancelled.

---

## P-01 — The seeded operator cannot launch anything, and the wizard never says so  [P1]

Server truth (POST /v1/pipelines with a valid body):
  viewer        403  {"title":"Requires role 'operator' or higher"}          <- correct and clear
  operator      403  {"title":"pipeline scope is not authorized"}            <- unscoped operator
  admin         201

The seeded apex-operator has role "operator" and scopes []. Every pipeline launch it attempts is
refused for scope, so this identity cannot run a test at all.

UI as that operator (isolated origin 127.0.0.1:4177, real key gate):
  /runs/new loads the full six-step wizard — Scope, Config, Work Items, Context, Prompts, Review
  permission copy anywhere on the page: NONE (regex for not authoriz|permission|denied|forbidden
  matched nothing)

So an operator can complete the entire authoring flow and only discover at Launch that they were
never permitted to run anything. This is the same root cause the Areas reviewer recorded for
Cycles (ARE-13: UI gates on role, server gates on scope), now shown on the product's primary task.

Fix: resolve launch capability from the consumer's scopes, not role alone, and disable New Test
with an explanation naming the project the consumer would need. Mirror the server's own two
distinct messages — role insufficient vs scope not authorized — rather than one generic failure.

## P-02 — For catalog mutations the UI is stricter than the server  [P3]

auth/RequireRole.tsx isGlobalAdmin() requires scopes.length === 0, and RequireGlobalAdmin hides
catalog mutations from anyone scoped. But a project-scoped admin is permitted by the server:

  scoped admin (role admin, scopes [{project_id:"demo", app_id:null}])
  POST /v1/catalog/applications  ->  201 Created

So the UI withholds a capability the consumer actually has. Less dangerous than the reverse, but
it is the same class of defect pointing the other way: the dashboard's permission model and the
server's do not agree in either direction.

(The application created by this probe was deleted; the catalog is back to ACC only.)

## Walkthrough k (admin: consumer, key, rotate) — the one-time key dialog is the best confirmation in the product
Screenshot 17-consumer-key-shown-once.jpg

Creating a consumer opens a dialog titled "API key created":
  "API key for ux-review-scoped-admin. Store it now — it will never be shown again."
  the key in a <code class="adm-key">, with a Copy button
  a checkbox "I have stored this key somewhere safe"
  "I've stored it", which is DISABLED until the checkbox is ticked (verified: confirmDisabled
  true -> false on tick)
and features/admin/ConsumerKeyLifecycle.tsx:109 registers a navigation guard, one of only four
in the app.

This is exactly proportionate to an irreversible, unrecoverable secret. It is worth contrasting
with the gate prompt editor (G-02), which protects an operator's hand-written prompt with
nothing at all, and with Skip Phase (G-01), which is irreversible and unconfirmed.

Form quality: NAME, TYPE, ROLE and SCOPES all carry visible labels AND aria-labels
("Scope 1 project", "Scope 1 app (optional)"). Scope fields are free text, though the catalog
knows the valid project and application ids.

---

## CON-01 — CONFIRMED in the browser, including the intermittency  [P1]

Probe on a COLD load of /prompts (connected build, 127.0.0.1:4175, fresh tab):
  styleSheets: inline, google-fonts, index-RX0qwNdz.css, CodeViewer-DZKOXiGK.css, pages-DIrrMmVo.css
  rules matching ".tonal-card":        []   (none)
  rules matching ".tonal-card.danger": []   (none)
  computed style of a probe <div class="tonal-card danger">:
      border 0px none · background rgba(0,0,0,0) · padding 0px · border-radius 0px
  -> an error banner renders as plain unstyled body text.

Then, same tab, SPA-navigate to /runs/{id} and back to /prompts:
  after run detail: rule appears, from RunDetailPage-DtYzWDro.css
  back at /prompts:  rule still present
  same probe now computes: border 1px solid · background rgb(254,240,241) · padding 8px 12px

So the identical banner renders two different ways in the same session depending on whether
the user happened to open a run detail page first.

Root cause (source): .tonal-card / .warning / .danger are defined ONLY at
features/runs/run-detail.css:368,376,382, and run-detail.css is imported only by
features/runs/RunDetailPage.tsx:28. 27 call sites across 18 modules use the class, including
the shared components/CachedDataWarning.tsx and features/home/HomePage.tsx.
vite.config.ts:77 sets test.css=false, so no unit test can catch it.

Fix: move the .tonal-card rules into theme/primitives.css (they are a shared primitive, not a
run-detail one). Add a .tonal-card.success rule — 3 call sites use it and no rule exists.

## CON-04 — DOWNGRADED: the leak is real, the stated consequence is not  [P2 -> P3]

Claim: FeaturePlaceholder.css:7 redefines .dash-empty unscoped, "permanently flattening ~40
empty states app-wide for the session".

Probe at /environments, then /zzz-does-not-exist, then back to /environments:
  before 404: 1 rule  (index-RX0qwNdz.css)  computed: border 1px solid, bg #fff, padding 24px,
                                                      radius 12px, text-align center
  at 404:     2 rules (+ pages-C3rWx0TR.css) computed: IDENTICAL
  after:      2 rules, persists               computed: IDENTICAL

The second rule is
  .dash-empty { display:flex; flex-direction:column; align-items:center; gap:10px;
                text-align:center; color: var(--text-secondary); }
It adds flex layout and restates two properties. It does NOT remove the border, background,
padding or radius. Empty states are not flattened; they become flex columns with a 10px gap.

So: a genuine unscoped global redefinition leaking out of a feature module (architectural
violation, latent hazard, should be scoped) — but not a visible breakage. Downgrade to P3 and
restate the consequence honestly.

---

## Verification of LOAD_CONVERGENCE.md claims (done by me, not the agent)

CONFIRMED exactly:
- --modal-backdrop consumed 0 times in Orchestration CSS.
- 63 hard-coded `text-transform: uppercase`; 0 uses of --label-case.
- 5 tokens referenced but never declared: --bg-app, --bg-overlay, --panel-border,
  --shadow-lg, --text-tertiary. Exactly 2 have no fallback:
    features/new-test/wizard.css:447  background: var(--bg-app)      -> no background painted
    features/admin/admin.css:590      box-shadow: var(--shadow-lg)   -> shadow dropped
- [data-theme='light']: 57 shared tokens, 54 differing values (Load cobalt #00388f vs
  Orchestration indigo #4f46e5; Load opaque surfaces rgba(255,255,255,1) vs Orchestration
  translucent 0.85/0.95 — the glassmorphism Load has since abandoned).

CORRECTED:
- "--modal-backdrop declared twice" -> declared SIX times (theme/tokens.css:62,
  themes.css:67/139/218/293, apex-light.css:50). Still consumed zero times, which is the
  substantive point.
- "no global select, input or textarea rule at all" -> there IS one, theme/tokens.css:151:
    input, select, textarea { font-family: inherit; }
  It sets font-family only — no colour, background or border — so the conclusion (native OS
  select chrome renders light over the dark themes) stands, but the claim must be stated as
  "no global *styling* rule".

ADDITIONAL detail the agent missed:
- --panel-border's fallback (context.css:148) is rgba(255,255,255,0.12) — a white border that
  is invisible on the three light themes.

---
