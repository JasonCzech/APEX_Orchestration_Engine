# Re-review — after the remediation branch

17 October 2026 · verifying `codex/holistic-ux-remediation-2026-10-17` @ `822b412`
(26 commits, 325 files, +10,141 / −3,596) against the findings in [REPORT.md](REPORT.md).

**Verdict: the remediation is real, and it is good work.** I re-tested the P0 and every P1 by
driving the built application rather than reading the diff, and 24 of 27 re-tested findings are
genuinely fixed — several more thoroughly than I recommended. The headline accessibility defect
is gone, compare no longer recommends a failed run, Home's three contradictory failure numbers
are now one honest number, and the gate editor both keeps your draft and stops eating your Tab
key. Concurrency, the strongest part of the original build, is not regressed.

**One P1 is not fixed and is reported as fixed.** Every gate decision is still attributed to
`actor: "unknown"`. The mechanism was built correctly; it is never reached, because the one route
that records approvals is the one route that does not take the caller's identity. Two further
claims in their write-up do not hold as stated.

## How this was verified

Their branch, built from source (`npm run build:dashboard`), migration `0039` applied, backend
run from the worktree with `APEX_CYCLES_ENABLED=true`, and the production bundle served behind the
same-origin proxy that reproduces the documented deploy contract — plus a second isolated origin
for role testing. The servers from the first review were stopped first so nothing was tested
against stale code. Evidence below is DOM probes, API payloads and measured contrast, not their
test output.

---

## Fixed — verified

### The P0 is gone, and the editor still works

Six Tab presses from inside the prompt editor now move focus out (`SUMMARY`, "Technical phase
output"), and the prompt is **unchanged**: 735 → 735 characters, no leading whitespace, primary
action still "Execute phase". The important check is that they did not fix a keyboard trap by
disabling the editor: `contentEditable` is still `true`, typing 25 characters lands, and the
action correctly flips to "Save edits and re-review". The editor also gained
`aria-label="System prompt"`, closing A11Y-11 at the same time. The `▶` and `↻` glyphs are gone
from the button names (A11Y-06 secondary). *(A11Y-04, A11Y-11)*

### The gate now protects an operator's work

Editing the prompt and clicking another queue row raises **"Unsaved review edits"** with
*Continue reviewing* / *Leave and keep draft*, and navigation is blocked. Choosing "leave and keep
draft", visiting another gate and returning restores the edit intact (760 chars, text present,
action still "Save edits and re-review"). That is more than I asked for — I recommended a guard
plus a per-`interrupt_id` buffer, and both are there. *(G-02)*

**Skip phase** is now `btn-danger` and opens a confirmation: *"Skip this phase? This decision is
irreversible. The phase will not execute, and later phases that require its output cannot run."*
*(G-01)*

A **revise on your own gate** no longer claims "actioned elsewhere": the row stays live, selection
stays put, and the canonical URL follows the newly issued interrupt
(`a8b760b8…` → `f54a4569…`) so the approver is not stranded. The badge dips for a few seconds
while the gate reopens, then settles correctly at 2. *(G-03)*

After a terminal decision the queue now advances to the **next open gate** rather than jumping to
index 0. *(A-02)*

### Nothing-is-announced is solved

A `role="status" aria-live="polite" .sr-only` region now exists and **updates on every `j`/`k`**,
announcing the run, the gate kind and phase, every phase's state, and the transport status:

> "RR output gate revise. Review phase results open on Story Analysis. Story Analysis: Results
> ready. Test Planning: Pending. … Snapshot refresh active"

Focus now lands on the selected `li[role=option]`, and its accessible name carries project, run id
and age: *"RR output gate revise. Review phase results. Story Analysis. Project demo. Run
01a14bc9-dd30-…. Updated 8m ago."* — exactly the gap I recorded. *(A11Y-06, A-01)*

### The product stopped stating facts it had not established

| Finding | Before | Now |
|---|---|---|
| R-01 | failing phase read "Pending"; live chip active on a dead run; three "yet" strings | `Status: Failed`, `Recorded snapshot`, errors shown, no "yet", no "still collecting" |
| R-01 reasons | no failure reason anywhere | five explicit lines, e.g. *"Cannot run phase 'script_scenario': prerequisite 'test_planning' ended with status 'failed'."* |
| COR-05 | "7/7 phases finished" on a failed run | "1/7 phases succeeded · 5 failed · 1 skipped" |
| R-02 | `DURATION 16m 58s` for a sub-second phase | **"Elapsed time (includes reviews)"** |
| COR-03 | "Test result: Failed" mid-execution | "Test result: Not available" |
| COR-02 | failed run's `0 ms` / `0.00%` tinted green as Baseline | **"— No collected result"**, baseline moves automatically, selector labels it *"(no collected result)"* |
| COR-01 | idle runs all read "Not running" | "Orchestration failed" / "Orchestration complete" |
| H-01 | FAILURES 4 · Failed 0 · Phases failed 0 | **"Runs needing investigation 5"**, captioned *"test failures or orchestration errors in loaded runs"* |

H-01's arithmetic now checks out: 4 runs with a failed test result + 1 orchestration-failed run
with no result = 5. Home's Recent runs table gained the **Test result** column it was missing, so
"BSSe and MQ release rehearsal · Orchestration complete · **Failed**" — the exact case I flagged —
is now visible. Execution health states its window and its exclusions explicitly
(*"Only succeeded and failed phases are counted here; skipped, aborted, waiting and active phases
are excluded"*), closing AGT-12 and COR-08. Timestamps carry year and timezone throughout
(`Oct 17, 2026, 05:38:13 PM EDT`). *(CON-08, COR-21)*

### Permissions are explained before the work, not after it

`/runs/new` as the unscoped operator now opens with a `role="alert"` banner carrying the server's
own words — *"Pipeline scope is not authorized. Ask an administrator to grant access to the
project you need."* — plus *"Draft saving requires project-wide access."* The six-step wizard is
no longer a trap. *(P-01)*

### Shared primitives actually reach surfaces now

Every tablist I probed (`/context`, prompt detail, `/admin/connections`, the gate's Prompt review
tabs, Phase workspace tabs) uses `workspace-tabs ux-underline-tabs` with `aria-controls` on every
tab, exactly one tab stop, and panels that exist. The gate editor's four tabs went from four tab
stops to one. *(CON-02)*

`/runs` headers are now `Run · Project · Application · Progress · Created · Elapsed snapshot ·
Test result · Status · **Actions**` — no duplicate "Run", singular "Application", and **8 sortable
header controls** where sortable headers previously reached one table in eighteen. *(CON-13, CON-11)*

### Contrast and charts

| | Before | Now |
|---|---|---|
| `.dash-badge` | 2.98 / 2.19 / 2.04:1 in the three dark themes | **4.96 – 10.70:1 across all six** |
| contrast script | 43 pairs/theme | **125 pairs/theme, min 4.58:1** |
| chart series | 6 defined, colour-only encoding | **8 defined in all six themes**, plus SVG `pattern` fills and `stroke-dasharray` variation |

The badge fix is better than my suggestion: rather than forcing one token, dark themes now use
black on the accent and light themes white. Charts are no longer distinguishable by colour alone —
a three-series chart renders six patterns and distinct dash arrays. axe-core returned **zero
violations** across `/`, `/runs`, a gate route and `/analytics` in `apex-light`, `dark` and
`monokai-dimmed`. *(A11Y-01, A11Y-03, LOAD convergence)*

The Load convergence is visibly in: sentence case throughout ("Test operations dashboard",
"Applications & environments"), and the `light` theme's accent is now `#00388f` — Load's cobalt.

### Not regressed

Two operators deciding the same gate still behaves correctly: the loser receives a decision
receipt and the superseded treatment, and the API records **exactly one** approval. The fix for
G-03 separated "my own action reopened this" from "someone else decided this" without breaking the
genuine case. Their dashboard suite runs **1,098 tests across 135 files, all passing**, matching
their claim.

---

## Not fixed

### L-01 — every approval is still attributed to "unknown" — P1

Two decisions made through the UI as `apex-admin` on their branch:

```
approve     01a14bc9-dce0…  {"gate":"prompt_review","action":"approve",    "actor":"unknown"}
skip_phase  01a14bc9-dcfd…  {"gate":"prompt_review","action":"skip_phase", "actor":"unknown"}
```

Their write-up says *"the authenticated consumer is named when identifiable"*, and commit `f1a32f3`
is titled *"Record authenticated reviews and evidence-backed pipeline outcomes"*.

The mechanism is built and correct in isolation — `gates.py:32 resolve_actor(config)` reads
`configurable.langgraph_auth_user_id`, `gates.py:83` passes it to `make_approval`, and
`auth/handlers.py` stamps that key in `on_threads_create_run`. It is never reached, because the
resume path never carries the caller:

```
routers/pipelines.py:737   async def resume_gate(thread_id, interrupt_id, body, service)
                           ← no `identity: CurrentIdentity` parameter
services/pipeline_read.py:1722
                           async def resume_gate(self, thread_id, interrupt_id, action, extras)
                           ← none here either
                    :1773  run_config = _run_config_from_state(state)   # from durable state
                    :1778  client.runs.create(config={"configurable": run_config, …})
```

Every gate decision takes that path, and the configurable handed to the graph is rebuilt from
persisted state. The asymmetry is visible in the same file — three sibling routes already thread
identity through (`:537 principal_id`, `:657 actor=identity.name or identity.consumer_id`,
`:697 principal_id`). Only the route that records approvals omits it.

That the plumbing works elsewhere in their own branch is easy to see: `/cycles` renders
**"Owner: apex-admin"**.

**Fix:** add `identity: CurrentIdentity` to the resume route, pass it into
`PipelineReadService.resume_gate`, and set `configurable["langgraph_auth_user_id"]` on the config
built at `pipeline_read.py:1778`, mirroring `rerun_pipeline`'s existing `principal_id` plumbing.

Their "Boundaries" note covers only *historical* actors. Decisions made today, on this branch,
are still anonymous — so the governance reviewer's first question still has no answer.

### ARE-07 — the route works; the 500 underneath it does not — P2

User-facing, this is fixed: `/test-presets` (renamed from `/golden-configs`) renders its preset
list, and the browser's own `POST /assistants/search` returns 200.

But their doc states the *"/assistants/search deployment 500 was not reproduced on the integrated
main"*. It reproduces on their branch in one command, for every body shape:

```
POST :2024/assistants/search  {"graph_id":"pipeline","limit":20}  → 500
POST :2024/assistants/search  {}                                  → 500
```

Backend traceback: `langgraph_api/api/assistants.py:227 request.json(...)` →
`apex/app/security.py:388 raise _RequestInputRejected` → *"ExceptionGroup: unhandled errors in a
TaskGroup"*. `security.py:510` does map that exception to 422, but the native LangGraph route reads
the body inside an anyio TaskGroup, which wraps it in an `ExceptionGroup`; a bare
`except _RequestInputRejected` does not match, so it escapes as 500.

`security.py` is **unchanged on this branch**, so this is pre-existing rather than a regression —
and it is the actual explanation of the original symptom. The page works because the client's
request shape changed, not because the server stopped mis-mapping. Any rejected or oversized body
on a native route (`/assistants/*`, `/threads/*`) still returns 500 instead of 422, which defeats
the dashboard's ability to tell "your request was invalid" from "the server broke".

**Fix:** `except*` the grouped form at `security.py:510`, or unwrap `ExceptionGroup` before
matching, keeping the existing 422 mapping.

### S-01 — honest in connected mode, still over-promising in demo — P3

Connected mode is materially better: the empty feed now reads *"No live activity was captured in
this session. Open Agent reasoning for the durable evidence and full transcript."* — it states the
fact and points at the durable evidence, which also closes AGT-02's signposting gap.
`ACTIVE_RUN_POLL_MS` dropped from 5,000 ms to 1,000 ms and the launch response's `run_id` is now
returned by `launchRun.ts`.

The stream still does not attach for a simulated run (chip stayed `idle | Snapshot refresh active`
for a full run), which is expected when phases finish in milliseconds. But in **demo** mode,
`/runs/run-active` — an explicitly executing run — still shows *"No live activity for this phase
yet."* and still promises *"Activity streams in while the run is executing"*, with no jump-to-live
control. The honest copy is conditional on the run being finished, so the one surface built to
demonstrate live monitoring is the one that still over-promises.

---

## New, minor

- **Application renders two ways on one screen.** Run detail's scope row resolves
  "Application **ACC**" while the Run context panel beside it shows the raw UUID
  `02971bd36e5347dcb7d8bebc7abcf34b`. Previously both said "Not selected"/"Not recorded" — the
  inconsistency has moved rather than closed, and now leaks an id. P3.
- **apex-light chart series remain below 3:1 against their surface** — `#00a8e0` at 2.74 and
  `#e5920a` at 2.49. Dark (3.95) and monokai (3.19) clear it. Patterns and dash arrays mean colour
  is no longer load-bearing, so this is cosmetic rather than blocking. P3.
- **The queue announcement is long.** Every `j`/`k` reads the run, gate, all seven phase states and
  the transport status — roughly forty words per keystroke for an approver working at speed.
  Verbosity beats silence, but a shorter form with the phase detail on demand would serve the
  triage case better. P3.

---

## Where the work exceeded the recommendation

Worth recording, because these were judgement calls made well:

- Draft **retention** on the gate guard, not merely a warning.
- The canonical review URL **following** the reopened interrupt after a revise, so the approver is
  never stranded on a dead id.
- Per-theme badge contrast colours instead of one forced token.
- **Patterns and dash arrays** on charts — a real non-colour encoding, not just better hues.
- A **version-skew notice** ("A dashboard update is available… Saved drafts will remain
  available") that I hit by swapping the bundle under a live tab.
- Contrast coverage nearly tripled, 43 → 125 pairs per theme.

## Scope of this re-review

I re-tested the P0 and all P1s, plus the P2s and P3s that were cheap to verify, by driving the
built application. I did not re-run the five area sweeps, so the long tail of P2/P3 findings in
`CONSISTENCY.md`, `CORRECTNESS.md`, `ACCESSIBILITY.md`, `AGENT_UX.md` and `AREAS.md` is unverified
here — their own suite covers much of it and passes. Cycles received a smoke check only; its
27 findings were not individually re-tested. No live provider, real engine, external ticket
creation or APEX Load handoff was exercised, as before. No application, backend or
`package.json` code was changed by me.
