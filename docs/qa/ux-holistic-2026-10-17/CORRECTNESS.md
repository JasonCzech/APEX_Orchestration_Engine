# Correctness review — UI values vs API values

Holistic UX review, 2026-10-17. Scope: every run-state, counter, timestamp, KPI,
comparison delta and analytics total shown on Home, `/runs`, run detail, `/runs/compare`
and `/analytics`, checked against `GET /v1/pipelines`, `GET /v1/pipelines/{thread_id}`,
`GET /v1/analytics/usage`, `GET /v1/analytics/agents` and the live SSE stream.

**Environments as observed**

| | |
|---|---|
| connected | `http://127.0.0.1:4175/`, admin role, 45 pipelines (19 `idle`, 18 `interrupted`, 8 `error`) |
| demo | `http://127.0.0.1:4173/`, project-wide admin presenter, 8 pipelines, offline ACC add-a-line v2.0 |
| API | `http://127.0.0.1:2024`, same-origin at `:4175/v1` |

The connected fleet had drifted from the brief's 39-run description by the time of the
review (45 runs, and `/v1/analytics/usage` now reports `phases_failed: 5` rather than `0`).
All payloads below were re-pulled at report time and the counts re-derived from that
snapshot. Demo-mode payloads are quoted from `apps/dashboard/src/dev-data/store.ts`,
which **is** the demo API.

**Already-confirmed findings are not repeated.** Extensions to them are flagged inline.

---

## Summary

| ID | Sev | Area | One line |
|---|---|---|---|
| COR-01 | P1 | compare | Every `idle` run's compare column header reads "Not running"; a run `/runs` calls ORCHESTRATION FAILED (red) reads NOT RUNNING (green) |
| COR-02 | P1 | compare | A run whose results were never collected is ranked **best** on p95 and error rate, in green, as "100.0% · Better" |
| COR-03 | P1 | /runs, run detail | "Test result: Failed" is asserted while the execution phase is still `running` and before the operator's gate decision |
| COR-04 | P2 | /runs vs run detail | Same run, two test-result labels: grid "Not available", detail "Not applicable" (10 of 45 runs) |
| COR-05 | P2 | /runs, run detail, header | "x/y phases finished" counts failed, aborted and skipped phases as finished — a failed run reads "7/7 phases finished" |
| COR-06 | P2 | all run surfaces | Phase status is read from two sources that disagree on 18 of 45 runs; the grid counter and the detail counter are computed from different ones |
| COR-07 | P2 | /analytics | Day buckets are shifted one day earlier: API `2026-10-17T00:00:00Z` renders as "Oct 16" |
| COR-08 | P2 | /analytics | The page names no time window at all; no preset is selected and `data.window` is never shown |
| COR-09 | P2 | /analytics | The MODELS filter offers four hardcoded models that exist in neither dataset; every option yields an empty window |
| COR-10 | P2 | /analytics | AVG LATENCY 53.0s with p95 5.6s — human gate wait is recorded as agent latency |
| COR-11 | P2 | run detail | Same page: header badge "ORCHESTRATION FAILED" vs overview headline "Stopped by operator"; badge tone is neutral where `/runs` uses danger |
| COR-12 | P2 | compare | "Artifacts · Warnings" prints `0 · 0` for phases the same table marks NOT SELECTED five rows above |
| COR-13 | P2 | run detail | "0/0 phases finished" and an empty phase strip when `phases_plan` is absent from the snapshot |
| COR-14 | P2 | run detail (demo) | Engine strip reads "completed · 1%" — `progress_pct` emitted as a 0–1 fraction where the contract is 0–100 |
| COR-15 | P3 | /runs | The DURATION column is unlabelled while the identical metric on Home is captioned; it means opposite things for stopped vs waiting runs |
| COR-16 | P3 | /analytics | Under one no-data condition, tokens render `0` and cost `$0.00` but latency and error rate render `—` |
| COR-17 | P3 | /analytics | "AGENTS / RUNS 7 / 11" captioned "0 models" while the table below lists a model row |
| COR-18 | P3 | compare vs run detail | Same phase status, different words: `succeeded` is "SUCCEEDED" on compare and "COMPLETE" on run detail |
| COR-19 | P3 | compare | Tied KPI values: two cells show 82.7, only the first is tinted "best" |
| COR-20 | P3 | run detail (demo) | Dialogue entries are timestamped ~56 minutes before the run was created |
| COR-21 | P3 | all | `formatTimestamp` renders neither a year nor a timezone |
| COR-22 | P3 | /analytics | Cost is silently omitted when `cost_visible: false` rather than shown as withheld |

**Regressions against `docs/dashboard-usability-remediation.md`:** COR-01, COR-04, COR-06
and COR-12 each contradict a claim that document makes (cited in each finding).
**Not regressed, verified intact:** comparison baseline selection, direction labels,
absolute-vs-relative differences, neutral equal/missing cases, and percentage-point
handling for error rate (line 15 of that document). The arithmetic is reproduced by hand
in the "Verified correct" table below.

---

## COR-01 — Compare calls every `idle` run "Not running", contradicting every other screen

- **Severity** P1
- **Area / route** `/runs/compare?ids=…` column headers
- **Mode / role** both; reproduced connected (admin) and demo (presenter)
- **Screenshots** `corr-03-compare-connected-not-running-and-zero-counts.jpg`, `corr-02-compare-not-running-and-pp-deltas.jpg`

### Repro

1. `/runs` — note the STATUS badge for `01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90`.
2. Open `/runs/compare?ids=01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90,019eb83c-21cf-7da2-9f07-a131d7475f79,01a14ad7-437e-7ea1-a913-d1e90b3adf72`.
3. Compare the column header badge with the grid badge for the same run.

### API value vs UI value

`GET /v1/pipelines/01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90`:

```json
{ "thread_status": "idle",
  "phase_strip": [
    {"phase":"story_analysis","status":"skipped","attempt":1},
    {"phase":"test_planning","status":"failed","attempt":1},
    {"phase":"env_triage","status":"succeeded","attempt":1},
    {"phase":"script_scenario","status":"failed","attempt":1},
    {"phase":"execution","status":"failed","attempt":1},
    {"phase":"reporting","status":"failed","attempt":1},
    {"phase":"postmortem","status":"failed","attempt":1}] }
```

| Surface | UI string | Tone |
|---|---|---|
| `/runs` STATUS column | `ORCHESTRATION FAILED` | `danger` (red) |
| Home Recent Runs | `ORCHESTRATION FAILED` | `danger` (red) |
| `/runs/:id` header badge | `ORCHESTRATION FAILED` | `neutral` (grey — see COR-11) |
| **`/runs/compare` column header** | **`NOT RUNNING`** | **`success` (green)** |

The same divergence applies to all 19 `idle` runs in the fleet, including the twelve that
read `ORCHESTRATION COMPLETE` elsewhere and the three demo baselines in
`corr-02-…jpg`, all three of which read `NOT RUNNING` in green on compare.

### Expected vs actual

`orchestrationStatusLabel(status, strip)` only consults the phase strip when
`status === 'idle'`; without a strip it falls through to the raw LangGraph status map,
where `idle` maps to "Not running". `ComparePage.tsx:179` calls it with **one argument**:

```tsx
{orchestrationStatusLabel(detail?.thread_status)}
```

Expected: the compare header reads the same outcome as every other surface
("Orchestration failed" / "Orchestration complete" / "Orchestration stopped") with the
matching tone. Actual: a terminal failure is presented as a neutral "not running" state
in a success-green badge, on the one screen whose purpose is choosing between runs.

`docs/dashboard-usability-remediation.md` line 9 states that "Detail, inspector,
**comparison**, and phase counters share phase normalization" — compare's header does not.

### Recommended change

`apps/dashboard/src/features/compare/ComparePage.tsx:179` — pass the normalized strip, as
`entriesFor` already computes elsewhere in the same component:

```tsx
{orchestrationStatusLabel(detail?.thread_status, run.query.data ? normalizedPhases(run.query.data.state, detail?.phase_strip ?? [], run.query.data.stateParseFailed) : undefined)}
```

and replace `threadStatusTone(detail?.thread_status)` on line 177 with
`orchestrationTone(...)` from `features/runs/runDisplay.ts` so the colour follows the label.

---

## COR-02 — Compare ranks a run with no collected results as the best run

- **Severity** P1
- **Area / route** `/runs/compare?ids=…` Engine KPIs block
- **Mode / role** demo (presenter)
- **Screenshot** `corr-07-compare-ranks-uncollected-run-as-best.jpg`

### Repro

1. Demo mode, `/runs/compare?ids=run-baseline-a,run-conditional,run-aborted`.
2. Read the P95 and ERROR RATE rows.

### API value vs UI value

`run-aborted` ("Shared environment contention rehearsal"), `thread_status: "error"`,
`state.run_aborted: true`, `phase_results.execution.status: "failed"`
(`dev-data/store.ts:468`, `:487`):

```jsonc
"test_summary": {
  "engine": "apexload",
  "passed": false,
  "kpis": { "tps_avg": 82.7, "p95_ms": 0, "error_rate": 0, "vusers_peak": 300 },
  "sla_breaches": [],
  "notes": "Collection did not complete."
}
```

| Row | baseline-a (baseline) | run-conditional | **run-aborted** |
|---|---|---|---|
| EXECUTION phase | SUCCEEDED | SUCCEEDED | **FAILED** |
| RESULT | PASSED | FAILED | **FAILED** |
| P95 (lower is better) | 2410 ms | 3650 ms `--worst` | **0 ms `compare-cell--best`** — "−2410 ms (100.0%) · Better" |
| ERROR RATE (lower is better) | 0.60% | 2.80% `--worst` | **0.00% `compare-cell--best`** — "−0.60 percentage points (100.0%) · Better" |

The run's own Test acceptance card on `/runs/run-aborted` reads
`Test acceptance: Failed — "Collection did not complete."`

### Expected vs actual

Expected: a KPI from an execution phase that did not reach a successful terminal state is
absent (`—`) and takes no part in best/worst ranking. Actual: the sentinel zeros are
treated as real measurements, so the run that produced no data is tinted green as the best
latency and the best error rate in the comparison, described as a 100% improvement. An
operator picking a release candidate from this table is pointed at the run with no evidence.

Two layers are at fault:

- `features/compare/compareModel.ts:95` `kpiValue()` accepts any finite number, and
  `:110` `bestWorst()` ranks it, with no reference to the execution phase's status,
  `test_summary.passed`, or `sla_breaches`.
- `dev-data/store.ts:487` encodes "unknown" as `0` for `p95_ms` and `error_rate` when
  `status === 'error'`.

### Recommended change

- `apps/dashboard/src/features/compare/ComparePage.tsx` — build `summaries` only from
  execution entries whose `status === 'succeeded'`; otherwise pass `undefined` so the row
  renders `—` and `bestWorst` skips the column. Add a small "no result collected" marker in
  the cell so the gap is explained rather than blank.
- `apps/dashboard/src/dev-data/store.ts:487` — emit `null` (not `0`) for `p95_ms` and
  `error_rate` when `status === 'error'`, matching "Collection did not complete."

---

## COR-03 — "Test result: Failed" is asserted before execution finishes and before the gate decision

- **Severity** P1
- **Area / route** `/runs` TEST RESULT column; `/runs/:id` header
- **Mode / role** demo (presenter)
- **Screenshot** `corr-06-test-result-failed-before-gate-decision.jpg`

### Repro

1. Demo mode, `/runs`. Read the row for `run-active` ("ACC add-a-line active ramp").
2. Open `/runs/run-active/phases/execution` while the stream is in the ramp.

### API value vs UI value

`run-active`, `thread_status: "busy"` (`dev-data/store.ts:467-490`):

```jsonc
"execution": {
  "phase": "execution",
  "status": "running",
  "ended_at": null,
  "duration_s": null,
  "test_summary": { "passed": false, "kpis": {…}, "sla_breaches": ["oms_create_order_p95_ms","error_rate"] }
}
```

`syncSummary` (`store.ts:2497-2499`) derives `execution_result` from
`test_summary.passed` alone, so the summary row carries `execution_result: "failed"`
while the phase is still running.

| Surface | UI string |
|---|---|
| `/runs` row | `PROGRESS 4/7` · `DURATION 1h 3m` · **`TEST RESULT: FAILED`** · `STATUS: RUNNING` |
| `/runs/run-active` header (during ramp) | `RUNNING` · **`Test result: Failed`** · `4/7 phases finished` |
| phase strip, same header row | `Execution — EXECUTING` |
| after the output gate opened | `AWAITING APPROVAL` · **`Test result: Failed`** · `4/7 phases finished` · `Execution — RESULTS READY` |

The gate panel on the same screen reads "Execution completed successfully…" and its
primary button is captioned *"Accepts this phase output; it does not approve the test
result or a release."* — while the header has already published the test result.

### Expected vs actual

The product's headline principle is that completing orchestration does not mean the test
passed; the inverse holds too. Expected: no verdict while the execution phase is
non-terminal — "Not available" with a "execution in progress" qualifier. Actual: a final,
red, unqualified `FAILED` on a run that is 4/7 phases in, mid-ramp, and whose operator has
not yet made the decision the screen is asking for.

Three other demo runs show the same pattern: `run-prompt-review` (5/7, AWAITING APPROVAL,
FAILED), `run-report-review` (5/7, AWAITING APPROVAL, FAILED) and `run-engine-recovery`
(6/7, AWAITING APPROVAL at a **provisioning-recovery** gate, FAILED) — the last reports
"the test failed" for a run whose engine could not be provisioned.

### Recommended change

`apps/dashboard/src/features/runs/runDisplay.ts:135` `executionResultLabel()` — take the
execution phase entry and return `'Not available'` (or a new `'In progress'`) whenever
`!isPipelinePhaseComplete(state?.phase_results?.execution?.status)`, before consulting
`run.execution_result`. All four `/runs` call sites and the detail header then follow.

---

## COR-04 — Same run, two test-result labels on two screens

- **Severity** P2
- **Area / route** `/runs` TEST RESULT column vs `/runs/:id` header
- **Mode / role** connected (admin)
- **Screenshot** `corr-08-failed-run-7of7-neutral-badge.jpg` (detail side)

### Repro

1. `/runs` — find `019eb83c-21cf-7da2-9f07-a131d7475f79` ("M2 demo"). TEST RESULT reads `NOT AVAILABLE`.
2. Open `/runs/019eb83c-21cf-7da2-9f07-a131d7475f79`. The header reads `Test result: Not applicable`.

### API value vs UI value

```json
{ "thread_status": "idle", "execution_result": null,
  "values": { "phases_plan": ["story_analysis"],
              "phase_results": { "story_analysis": { "status": "succeeded", … } },
              "external_results": null, "engine_handle": null } }
```

| Surface | UI string |
|---|---|
| `/runs` TEST RESULT | `NOT AVAILABLE` |
| `/runs` compact card | `Test result: Not available` |
| `/runs` master list item | `Test result: Not available` |
| **`/runs/:id` header** | **`Test result: Not applicable`** |
| `/runs/:id` Test acceptance card | `Not applicable` — "Execution was not selected; there is no test acceptance decision for this run." |

### Expected vs actual

`executionResultLabel(run, state)` distinguishes the two via `executionWasOmitted(state)`,
which needs `phases_plan`. `RunsListPage.tsx:69`, `:124`, `:172` and `:249` all call it as
`executionResultLabel(run)` — **one argument** — because the grid only holds the summary,
which has no `phases_plan`. So the grid can never say "Not applicable".

Affects **10 of 45 runs** (every run whose plan excludes `execution`):
`019eb81a-d9ed`, `019eb83c-21cf`, `019eb83d-e8a6`, `019eb83d-ed5f`, `019eb83e-2ee6`,
`019eb83e-7613`, `019eb83e-7b01`, `019eb83e-c8c7`, `019eb83e-cbdd`, `019eb97c-7a7f`.

`docs/dashboard-usability-remediation.md` line 7 fixes the canonical label for a missing
result as **"Not available"**; the later "Not applicable" branch was added to the detail
path only, so the two screens now disagree about the same run.

### Recommended change

Either (a) add `phases_plan` to the pipeline summary payload so the grid can make the same
distinction, or (b) — cheaper, no API change — move the omitted-execution wording out of
`executionResultLabel` into the detail-only Test acceptance card, and let **both** screens
show "Not available" as line 7 specifies. `apps/dashboard/src/features/runs/runDisplay.ts:135`
and `apps/dashboard/src/features/runs/RunsListPage.tsx`.

---

## COR-05 — "x/y phases finished" counts failed, aborted and skipped phases as finished

- **Severity** P2
- **Area / route** `/runs` PROGRESS column; `/runs/:id` header; run overview card; phase-navigation summary
- **Mode / role** connected (admin)
- **Screenshot** `corr-08-failed-run-7of7-neutral-badge.jpg`

### Repro

1. `/runs` — row `01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90`: `PROGRESS 7/7`, `STATUS ORCHESTRATION FAILED`.
2. Open the run. Header: `7/7 phases finished`. Overview: "Orchestration failed — 7 of 7 selected phases finished."

### API value vs UI value

```jsonc
"phase_results": {
  "story_analysis":  { "status": "skipped",   "duration_s": 1933.010819 },
  "test_planning":   { "status": "failed", "errors": ["Cannot run phase 'test_planning': prerequisite 'story_analysis' ended with status 'skipped'."] },
  "env_triage":      { "status": "succeeded" },
  "script_scenario": { "status": "failed", "errors": ["…prerequisite 'test_planning' ended with status 'failed'."] },
  "execution":       { "status": "failed", "errors": ["…prerequisite 'script_scenario' ended with status 'failed'."] },
  "reporting":       { "status": "failed", "errors": ["…prerequisite 'execution' ended with status 'failed'."] },
  "postmortem":      { "status": "failed", "errors": ["…prerequisite 'reporting' ended with status 'failed'."] }
}
```

One phase succeeded. One was skipped. Five failed.

| Surface | UI string |
|---|---|
| `/runs` PROGRESS | `7/7` |
| `/runs/:id` header | `7/7 phases finished` |
| run overview card | `7 of 7 selected phases finished.` |
| phase-navigation summary | `Phase navigation · 7/7` |

### Expected vs actual

`isPipelinePhaseComplete` (`runDisplay.ts:124`) returns true for
`['succeeded','failed','aborted','skipped']`, and `phaseProgress` (`:187`) counts those as
`completed`. "Finished" in English means "completed successfully"; the counter means
"reached a terminal state". The result is a headline that reads "Orchestration failed —
7 of 7 selected phases finished", which tells an operator the opposite of what happened.

The same counter is equally misleading in the other direction: `01a14ad7-437e` reads
`0/0` (COR-13), and the 13 runs parked at a human gate read `0/7` although their first
phase has produced reviewable output.

### Recommended change

`apps/dashboard/src/features/runs/runDisplay.ts:187` — have `phaseProgress` return
`{ succeeded, terminal, total }` and reword the label to match what is counted, e.g.
"5 of 7 phases succeeded · 2 failed", or at minimum rename the string to
"x/y phases finished" → "x/y phases settled" and surface the failed count beside it.
Update `RunsListPage.tsx:164`/`:125`/`:246`, `RunDetailPage.tsx:220`/`:335`/`:343` and
`RunOverview.tsx:55`.

---

## COR-06 — Phase status is read from two sources that disagree on 18 of 45 runs

- **Severity** P2
- **Area / route** `/runs` PROGRESS and STATUS; `/runs/:id` header, phase strip and overview; `/runs/compare`
- **Mode / role** connected (admin)
- **Screenshot** `corr-01-execution-pending-vs-strip-running.jpg`

### Repro

1. `curl -H "x-api-key: …" http://127.0.0.1:2024/v1/pipelines/01a14ad7-34e0-7160-94d9-fe5cb8470596`
2. Compare `phase_strip[4]` with `values.phase_results.execution`.
3. Open `/runs/01a14ad7-34e0-7160-94d9-fe5cb8470596/phases/execution`.

### API value vs UI value

```jsonc
{ "thread_status": "error", "current_phase": "execution",
  "phase_strip": [ …, {"phase":"execution","status":"running","attempt":1}, … ],
  "values": { "phase_results": { "execution": { "status": "pending", "attempt": 1,
                                                "artifact_ids": [], "errors": [] } } } }
```

| Surface | source read | UI string for `execution` |
|---|---|---|
| `/runs` PHASE column | `current_phase` | `Execution` |
| `/runs` PROGRESS `4/7` | **`phase_strip`** (`RunsListPage.tsx:80`, `:164`) | — |
| `/runs/:id` header `4/7` | **`phase_results`** (`RunDetailPage.tsx:126`→`:131`) | — |
| `/runs/:id` phase strip | `phase_results` | **`PENDING`** (`phase-step--pending`, aria `execution — pending (attempt 1)`) |
| `/runs/compare` EXECUTION cell | `phase_results` | `PENDING` |

### Scope of the disagreement

Across the 45-run fleet the two sources disagree on **18 runs**:

| family | runs | `phase_strip` | `values.phase_results` |
|---|---|---|---|
| errored mid-execution | 6 | `execution: running` | `execution: pending` |
| parked at a human gate | 12 | `story_analysis: awaiting_prompt_review` / `awaiting_output_review` | `story_analysis: pending` |

For the 12 gated runs the run-detail page happens to mask the disagreement: `RunDetailPage.tsx:127-130`
overwrites the normalized status from `hitl.gate` after `normalizedPhases` has run. Remove the
pending gate and the detail page would show "Pending" for a phase the grid shows as awaiting review.

The `x/y` counters agree **today** only because none of the 18 disagreements involves a status
in `isPipelinePhaseComplete`'s set — `running`/`awaiting_*`/`pending` are all "not finished".
A single `succeeded`-vs-`pending` drift would make the grid and the detail print different
numbers for the same run with no indication which is right.

### Expected vs actual

Expected, per `docs/adr/0006-dashboard-architecture.md` (the cached `threads.state`
snapshot is canonical) and `docs/dashboard-usability-remediation.md` line 9 ("Detail,
inspector, comparison, and phase counters share phase normalization"): one projection,
`normalizedPhases`, feeds every phase display and every counter. Actual: the grid bypasses
it entirely and reads `run.phase_strip` directly, and compare's header reads neither — a
regression against that line.

### Recommended change

`apps/dashboard/src/features/runs/RunsListPage.tsx:80` and `:164`/`:125`/`:246` — the grid
has only the summary, so either (a) extend the summary with the authoritative status so
`phase_strip` stops being a second source of truth, or (b) make the backend guarantee
`phase_strip[i].status === values.phase_results[i].status` and add a contract test. Option
(b) is the smaller change and fixes all 18 runs at the source; `src/apex/routers/` builds
the strip.

---

## COR-07 — Analytics day buckets are labelled one day early

- **Severity** P2
- **Area / route** `/analytics` — all four time-series charts and the tick tooltip
- **Mode / role** connected (admin) and demo
- **Screenshot** `corr-05-analytics-connected-latency-p95-and-oct16-tick.jpg`

### Repro

1. `curl -H "x-api-key: …" "http://127.0.0.1:2024/v1/analytics/agents?group_by=model&bucket=day"`
2. Open `/analytics` in a browser whose timezone is behind UTC (here America/New_York, UTC-4).
3. Read the x-axis of LATENCY OVER TIME BY MODEL.

### API value vs UI value

```json
{ "window": { "from": "2026-10-10T17:52:42.001998Z",
              "to":   "2026-10-17T17:52:42.001998Z", "bucket": "day" },
  "series": [ { "bucket_start": "2026-10-17T00:00:00Z", … } ] }
```

| | |
|---|---|
| API `series[0].bucket_start` | `2026-10-17T00:00:00Z` — the UTC day bucket for **17 October** |
| UI x-axis tick | **`Oct 16`** |

Today is 17 October 2026. The only bucket of data in the window is today's, and the chart
labels it yesterday.

### Expected vs actual

`AnalyticsPage.tsx:81-89`:

```ts
function bucketTick(bucket: Bucket): (iso: string) => string {
  return (iso) => {
    const date = new Date(iso)
    …
    return bucket === 'hour'
      ? `${pad(date.getHours())}:${pad(date.getMinutes())}`
      : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  }
}
```

`new Date("2026-10-17T00:00:00Z")` in UTC-4 is 16 Oct 20:00 local, and
`toLocaleDateString` renders the **local** calendar day. The server buckets with
`date_trunc('day', …)` over UTC timestamps (`src/apex/services/usage.py:846`,
`src/apex/services/agent_analytics.py:285`), so a day bucket is a UTC calendar day and
must not be re-projected into local time. The `hour` branch has the same defect:
`getHours()` shifts a `14:00Z` bucket to `10:00`.

This also explains the demo chart, whose 7-day window ends today but whose axis runs
Oct 10 → **Oct 16**: `dev-data/store.ts:279-284` `agentBucketStart` likewise emits UTC
midnight.

Clicking a tick compounds it: `zoomDate` (`AnalyticsPage.tsx:785-790`) parses the same ISO
and adds 24h, so clicking the tick labelled "Oct 16" zooms to `2026-10-17T00:00:00Z` →
`2026-10-18T00:00:00Z` — the window contradicts the label the user clicked.

### Recommended change

`apps/dashboard/src/features/analytics/AnalyticsPage.tsx:81-89` — format bucket ticks in
UTC (`timeZone: 'UTC'` for the day branch, `getUTCHours()`/`getUTCMinutes()` for the hour
branch) and state the zone once in the chart caption, e.g. "buckets are UTC days".

---

## COR-08 — `/analytics` names no time window

- **Severity** P2
- **Area / route** `/analytics` header
- **Mode / role** connected (admin) and demo
- **Screenshot** `corr-05-analytics-connected-latency-p95-and-oct16-tick.jpg`

### Repro

1. Open `/analytics` with no query string.
2. Inspect the preset buttons: `24h`, `7d`, `30d`, `Custom`.

### API value vs UI value

| | |
|---|---|
| API `window` | `{"from":"2026-10-10T17:52:42.001998Z","to":"2026-10-17T17:52:42.001998Z","bucket":"day"}` — a concrete 7-day window |
| UI preset state | `24h aria-pressed=false`, `7d aria-pressed=false`, `30d aria-pressed=false`, `Custom aria-pressed=false` |
| UI text naming the window | *none anywhere on the page* |

So `TOTAL TOKENS —`, `AVG LATENCY 53.0s`, `AGENTS / RUNS 7 / 11` and `ERROR RATE 8.9%` are
presented with no statement of the period they cover.

### Expected vs actual

`AnalyticsPage.tsx:794-795` passes only the URL params to `WindowPresets`:

```tsx
value={{ ...(filters.from ? { from: filters.from } : {}), ...(filters.to ? { to: filters.to } : {}) }}
```

and `timeWindow.ts` `activePresetLabel` returns `null` unless **both** `from` and `to` are
present. On first load neither is, so the server default (7 days) is in force but no preset
lights up and `data.window` is never rendered.

Expected: the resolved window is named — either by lighting `7d` from `data.window`, or by
printing "Oct 10 17:52 – Oct 17 17:52 UTC · day buckets" beside the presets, which also
covers the case where the server clamps or overrides the requested window. Home's
EXECUTION HEALTH panel has the mirror-image problem: it hardcodes the caption "Last 7 days,
across accessible projects" (`HomePage.tsx:258`) without reading `data.window` either — it
is right today only because the server default happens to match.

### Recommended change

`apps/dashboard/src/features/analytics/AnalyticsPage.tsx:794` — seed `WindowPresets`'
`value` from `data?.window` when the URL carries no `from`/`to`, and render the resolved
`from`–`to`–`bucket` as text in the toolbar. Mirror it at
`apps/dashboard/src/features/home/HomePage.tsx:258` by deriving the caption from
`usage.data.window` rather than hardcoding "Last 7 days".

---

## COR-09 — The MODELS filter lists four models that exist in neither dataset

- **Severity** P2
- **Area / route** `/analytics` MODELS facet
- **Mode / role** connected (admin) and demo
- **Screenshot** `corr-04-analytics-phantom-model-facet-zero-vs-dash.jpg`

### Repro

1. Demo mode, `/analytics`. The MODEL table lists `claude-sonnet-4-20250514`,
   `claude-opus-4-20250514`, `claude-3-5-haiku-20241022`.
2. Click the first MODELS chip, `claude-3-5-sonnet-latest`.

### API value vs UI value

| | |
|---|---|
| Demo breakdown keys | `claude-sonnet-4-20250514`, `claude-opus-4-20250514`, `claude-3-5-haiku-20241022` |
| Connected breakdown keys | `unknown` (the API returns `"models": 0`) |
| **UI MODELS facet** | **`claude-3-5-sonnet-latest`, `claude-3-5-haiku-latest`, `gpt-4o`, `gpt-4o-mini`** |

Result of selecting any of them: `No agent events in this window` /
"Try a wider time window or clear a model, agent, or test filter."

### Expected vs actual

`AnalyticsPage.tsx:59-64`:

```ts
const MODEL_OPTIONS = [
  'claude-3-5-sonnet-latest',
  'claude-3-5-haiku-latest',
  'gpt-4o',
  'gpt-4o-mini',
]
```

A hardcoded list, used at `:865`. The sibling facets are derived (`AGENT_OPTIONS` at `:67`
comes from `PHASE_NAMES`, STAGES likewise), so MODELS is the only one that can be wrong —
and it is wrong in both data modes. Every one of its four options is a dead end, and the
three models that do have data cannot be selected at all. The empty-state hint sends the
user to widen the window, which will never help.

### Expected

The facet is populated from the keys the API actually returns for the current window
(`data.breakdown` when `group_by=model`, or a dedicated facet endpoint).

### Recommended change

`apps/dashboard/src/features/analytics/AnalyticsPage.tsx:59-64` — delete `MODEL_OPTIONS`
and derive the chip list from the `group_by=model` breakdown for the active window,
merging in any models already selected in the URL so a deep link stays togglable.

---

## COR-10 — "AVG LATENCY 53.0s · p95 5.6s": gate wait time is recorded as agent latency

- **Severity** P2
- **Area / route** `/analytics` AVG LATENCY tile, AVG LATENCY / P95 table columns, latency charts
- **Mode / role** connected (admin)
- **Screenshot** `corr-05-analytics-connected-latency-p95-and-oct16-tick.jpg`

### Repro

1. `curl -H "x-api-key: …" "http://127.0.0.1:2024/v1/analytics/agents?group_by=stage&bucket=day&sort=avg_latency_ms&order=desc"`
2. Open `/analytics` connected.

### API value vs UI value

```
stage              events errors avg_latency_ms p95_latency_ms  runs
story_analysis         11      0       268284.5      1475476.0    11
execution               4      1         4175.2         5622.4     4
env_triage             11      0           28.4           57.5    11
postmortem              4      1           26.0           29.6     4
reporting               4      1           26.0           26.9     4
script_scenario        11      1           24.0           30.5    11
test_planning          11      1           20.0           27.5    11

totals: events 56, errors 5, avg 53014.91 ms, p95 5560.00 ms
```

| UI element | UI string |
|---|---|
| AVG LATENCY tile | **`53.0s`** |
| its hint | **`p95 5.6s`** |
| MODEL table row `unknown` | `AVG LATENCY 53.0s` · `P95 5.6s` |
| grouped by Stage, `story_analysis` | `AVG LATENCY 268.3s` · `P95 1475.5s` |

### Expected vs actual

Both numbers are computed correctly server-side — `func.avg` and
`func.percentile_cont(0.95)` (`src/apex/services/agent_analytics.py:261-262`) — so the
arithmetic is sound. The display is still wrong as a statement about agents: a p95 one
tenth of the mean is a signal that the measure is not what its label says. The stage
breakdown shows why: `story_analysis` has a p95 of 1,475,476 ms — **24 minutes 35 seconds**
— because the runs that sat at a prompt-review gate record the human's wait as the phase's
`latency_ms`. Every other stage is 20–30 ms. The page is titled "Agent Analytics" and the
tile is titled "AVG LATENCY"; neither suggests the number is dominated by how long a person
took to click Approve.

Secondary: `analyticsFormat.ts:14` `formatLatency` only tiers ms → s, so 24m 35s renders as
**`1475.5s`**, while `runDisplay.ts` `formatDuration` on the same screens would render
`24m 35s`. Two duration formatters, different tiers.

### Recommended change

- Exclude gate-wait intervals from `AgentEvent.latency_ms`, or record them under a separate
  measure, in `src/apex/services/agent_analytics.py` / the emitter that writes
  `phase:*:succeeded` events. Until then, caption the tile "includes time spent waiting at
  human review gates" at
  `apps/dashboard/src/features/analytics/AnalyticsPage.tsx:349-353`.
- `apps/dashboard/src/features/analytics/analyticsFormat.ts:14` — add minute and hour tiers
  to `formatLatency`, or reuse `formatDuration`.

---

## COR-11 — Run detail contradicts itself, and its status badge is tinted differently from the same label on `/runs`

- **Severity** P2
- **Area / route** `/runs/:id` header badge vs run overview headline
- **Mode / role** demo (contradiction), connected (tone)
- **Screenshot** `corr-08-failed-run-7of7-neutral-badge.jpg`

### Repro A — contradictory text (demo)

Open `/runs/run-aborted`.

| Element | UI string |
|---|---|
| header badge | `ORCHESTRATION FAILED` |
| overview card headline, ~100 px below | **`Stopped by operator`** |
| `/runs` STATUS for the same run | `ORCHESTRATION FAILED` |

API: `thread_status: "error"`, `values.run_aborted: true`.
`RunOverview.tsx:47` reads `state.run_aborted` first; `RunDetailPage.tsx:217` never
consults it. Both are on screen at once.

### Repro B — tone (connected)

| run | `/runs` badge class | `/runs/:id` header badge class | label (identical) |
|---|---|---|---|
| `01a14ad6-1f91…` (idle, 5 failed phases) | `status-badge danger` | **`status-badge neutral`** | `ORCHESTRATION FAILED` |
| `019eb83c-21cf…` (idle, all succeeded) | `status-badge success` | **`status-badge neutral`** | `ORCHESTRATION COMPLETE` |

`RunsListPage.tsx:176` uses `orchestrationTone(status, strip)`, which derives the colour
from the computed label. `RunDetailPage.tsx:216` uses the local `threadStatusBadge()`
(`:376-388`), which maps raw `thread_status` and sends every `idle` run to `neutral` —
so a failed run loses its red the moment you open it.

### Expected vs actual

Expected: one outcome string and one tone per run, everywhere. Actual: a failed run reads
red on the grid, grey on its own page, and "Stopped by operator" one card lower.

### Recommended change

- `apps/dashboard/src/features/runs/RunDetailPage.tsx:216` — replace `threadStatusBadge(...)`
  with `orchestrationTone(detail.thread_status, phaseStrip)` from `runDisplay.ts`, and
  delete the now-unused local helper at `:376`.
- Move the `run_aborted` branch out of `RunOverview.tsx:47` into
  `runDisplay.ts` `orchestrationStatusLabel()` (give it the state), so "Stopped by operator"
  is produced once and appears in the badge, the grid and the overview together.

---

## COR-12 — Compare prints `0 · 0` for phases it has just marked "Not selected"

- **Severity** P2
- **Area / route** `/runs/compare` → "Artifacts · Warnings" block
- **Mode / role** connected (admin)
- **Screenshot** `corr-03-compare-connected-not-running-and-zero-counts.jpg`

### Repro

`/runs/compare?ids=01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90,019eb83c-21cf-7da2-9f07-a131d7475f79,01a14ad7-437e-7ea1-a913-d1e90b3adf72`

### API value vs UI value

`019eb83c-21cf` has `phases_plan: ["story_analysis"]` and a `phase_results` object with
exactly one key; the other six phases do not exist for this run.
`01a14ad7-437e` has an empty `phase_results` and no plan at all.

| phase | Phases block (same table) | **Artifacts · Warnings block** |
|---|---|---|
| TEST PLANNING | `NOT SELECTED` · `—` | **`0 · 0`** |
| ENV TRIAGE | `NOT SELECTED` · `—` | **`0 · 0`** |
| SCRIPT & SCENARIO | `NOT SELECTED` · `—` | **`0 · 0`** |
| EXECUTION | `NOT SELECTED` · `—` | **`0 · 0`** |
| REPORTING | `NOT SELECTED` · `—` | **`0 · 0`** |
| POSTMORTEM | `NOT SELECTED` · `—` | **`0 · 0`** |

### Expected vs actual

`ComparePage.tsx:237-238`:

```tsx
if (normalized?.status === 'none') return { status: 'none' }
return { ...snapshot.state.phase_results?.[phase], status: normalized?.status, … }
```

The `none` branch returns a truthy object `{status:'none'}`, so the counts cell at `:416-423`
takes the `entry ?` branch and renders `entry.artifact_ids?.length ?? 0` → `0 · 0`.
`countPhases` (`:246`) never filters it out either, because its predicate is
`entry !== undefined` and entries are never undefined once a snapshot loads.

Expected: `—` (the same em dash the Phases block uses), or the row omitted. Actual: the
same table says "this phase was not part of this run" and then, five rows down, that it
produced zero artifacts and zero warnings — an assertion that it ran.

`docs/dashboard-usability-remediation.md` line 42 records that "Comparison also omits old
duration/attempt metadata for excluded phases"; that holds in the Phases block but was not
applied to the counts block.

### Recommended change

`apps/dashboard/src/features/compare/ComparePage.tsx:416-425` — render
`<span className="compare-muted">—</span>` when `entry.status === 'none'`, and change
`countPhases` at `:246` to `entriesFor(phase).some((e) => e && e.status !== 'none')` so a
phase excluded from every run in the comparison drops out of the block entirely.

---

## COR-13 — "0/0 phases finished" and an empty phase strip when `phases_plan` is absent

- **Severity** P2
- **Area / route** `/runs` PROGRESS; `/runs/:id` header, overview and phase strip
- **Mode / role** connected (admin)

### Repro

1. `/runs` — row `01a14ad7-437e-7ea1-a913-d1e90b3adf72` ("Reporting-only analysis run"):
   `PROGRESS 0/0`, `PHASE: Phase not recorded`, `STATUS ORCHESTRATION FAILED`.
2. Open `/runs/01a14ad7-437e-7ea1-a913-d1e90b3adf72/phases/story_analysis`.

### API value vs UI value

```jsonc
{ "thread_status": "error", "current_phase": null, "execution_result": null,
  "phase_strip": [ {"phase":"story_analysis","status":"none","attempt":null}, …all seven `none`… ],
  "values": { "phase_results": {}, /* phases_plan is ABSENT */ } }
```

| Surface | UI string |
|---|---|
| `/runs` PROGRESS | `0/0` |
| `/runs/:id` header | `0/0 phases finished` |
| overview headline | `Orchestration failed` |
| overview body | **`0 of 0 selected phases finished.`** `Execution evidence has not been recorded.` |
| phase strip (`selectedOnly`) | **zero segments rendered** |
| disclosure below the strip | `Phases outside this run` → all seven phase names |
| primary button | `Open Story Analysis` |

### Expected vs actual

The snapshot does not say "no phases were selected" — it says **the plan was never
recorded**. `normalizedPhases` (`runDisplay.ts:170`) treats a missing `phases_plan` as "no
plan constraint", then falls through to `'none'` for every phase because `phase_results` is
empty, and the UI renders that as the positive claim "0 of 0 **selected** phases finished".
`PhaseStrip`'s `selectedOnly` filter (`PhaseStrip.tsx:66`) then produces an empty array, so
the pipeline-progress hero — the main visual on the page — is blank for an errored run.
The button offers to open Story Analysis, which the same page has just listed under
"Phases outside this run".

Four other runs are in this state (`019eb7fd-1c91`, `01a087e3-2a5e`, `01a14ad5-179e`,
`9a8b7bdd-63e9`), all showing `0/0`.

### Recommended change

`apps/dashboard/src/features/runs/runDisplay.ts:187` — have `phaseProgress` return a
`planRecorded: boolean`, and in `RunDetailPage.tsx:220` / `RunOverview.tsx:55` render
"Phase plan not recorded for this run" instead of `0/0` when it is false. In
`PhaseStrip.tsx:66`, fall back to all seven phases rather than an empty strip when the
`selectedOnly` filter yields nothing.

---

## COR-14 — Demo engine strip reads "completed · 1%"

- **Severity** P2
- **Area / route** `/runs/:id/phases/execution` → APEX Load engine strip; Pipeline Log activity feed
- **Mode / role** demo (presenter)

### Repro

1. Demo mode, open `/runs/run-active/phases/execution` and watch the strip through the ramp.

### API value vs UI value

`dev-data/store.ts:1007-1014` emits these `engine_poll` samples:

```js
{ progress_pct: 0.08, …, status: 'provisioning' },
{ progress_pct: 0.24, …, status: 'running' },
…
{ progress_pct: 0.91, …, status: 'running' },
{ progress_pct: 1,    …, status: 'completed' },
```

| sample | UI string in the strip |
|---|---|
| `progress_pct: 0.08` | `provisioning · 0%` |
| `progress_pct: 0.91` | `running · 1%` |
| `progress_pct: 1` | **`completed · 1%`** |

### Expected vs actual

The wire contract is a percentage, **not** a fraction:

- `src/apex/ports/execution_engine.py:60` — `progress_pct: FiniteFloat = Field(default=0.0, ge=0, le=100)`
- `packages/pipeline-events/tests/fixtures.test.ts:164,171` — `progress_pct: 37.5`, `progress_pct: 100.0`

So `EngineStrip.tsx:96-97` (`Math.round(latestSample.progress_pct)%`) and
`ActivityFeed.tsx:96-98` (`fmtPct`) are **correct** for the real contract; the demo fixture
is the defect. Note the same sample object gets `error_rate: 0.028` right as a 0–1 fraction
(rendered `2.80%` by `EngineStrip.tsx:51`) — the fixture treats both fields as fractions
where only one is.

This is the run shown during presentations, and a completed load test displaying "1%"
undercuts the whole execution surface.

### Recommended change

`apps/dashboard/src/dev-data/store.ts:1007-1014` and `:1171` — multiply the
`progress_pct` literals by 100 (`8, 24, 45, 62, 76, 91, 100`) to match
`src/apex/ports/execution_engine.py:60`. Leave `error_rate` as a fraction.

---

## P3 findings

### COR-15 — `/runs` DURATION is unlabelled and means opposite things per state

`/runs` column header is bare `DURATION`; Home's identical metric is captioned
*"loaded runs: creation to last update, not execution duration"*. Both compute
`(updated_at − created_at)` (`RunsListPage.tsx:83-89`, `homeLogic.ts:108-123`).

| run | API | UI DURATION | what actually happened |
|---|---|---|---|
| `01a14ad6-1f91` | created `17:08:29Z`, updated `17:40:46Z` | `32m 17s` | six phases ran in 0.02–0.08 s each; 32 min was a gate wait |
| `01a14ad7-4a0e` | created `17:09:45.614Z`, updated `17:09:46.749Z` | `1.1s` | has been waiting at a human gate for ~35 min |

So the same column overstates a stopped run's work by 32 minutes and understates a waiting
run's elapsed time by 35. **Fix:** `apps/dashboard/src/features/runs/RunsListPage.tsx` —
give the header a `title`/caption matching Home's wording, or split "elapsed" from
"execution time" once phase durations are summed.

Related: `01a14ad6-1f91`'s `story_analysis` is `status: "skipped"` with
`duration_s: 1933.010819`, which compare renders as **`SKIPPED · 32m 13s`** and highlights
with `compare-cell--slow` as the slowest phase in the row — the "slowest phase" marker is
on a phase that never ran. **Fix:** `compareModel.ts:49` `slowestIndex` should skip entries
whose status is not `succeeded`/`failed`.

### COR-16 — Analytics renders `0` for some zero-data measures and `—` for others

With `?model=claude-3-5-sonnet-latest` (zero matching events) the tiles read
`TOTAL TOKENS 0` · `0 in / 0 out`, `EST. COST $0.00`, `AVG LATENCY —` (`p95 —`),
`AGENTS / RUNS 0 / 0`, `ERROR RATE —`. One condition, two truth claims: tokens and cost
assert a measured zero while latency and error rate admit they are unknown.
`analyticsFormat.ts:1-6` returns `'0'` for `value === 0` and `:8-12` returns `'$0.00'`,
while `:14-18` and `:20-22` return `'—'`. **Fix:** in
`apps/dashboard/src/features/analytics/AnalyticsPage.tsx:337-367`, render every tile as
`—` when `data.totals.events === 0`.

### COR-17 — "AGENTS / RUNS 7 / 11" captioned "0 models" above a table listing a model

Connected `/analytics`: `totals.agents = 7`, `totals.runs = 11`, `totals.models = 0`, yet
the MODEL table below has one row keyed `unknown` (the server coalesces a null model to
`"unknown"` at `agent_analytics.py:278` but excludes it from the distinct count). The tile
says there are no models; the table lists one. The `7 / 11` composite also gives no clue
which number is which. **Fix:** `AnalyticsPage.tsx:355-360` — label the two halves
("7 agents · 11 runs") and either count `unknown` as a model or caption it "model not
recorded". Also note `runs: 11` here vs Home's `RECENT RUNS 45` — different scopes, neither
stated.

### COR-18 — Compare and run detail use different words for the same phase status

| snapshot status | `/runs/compare` (`statusLabel`, `runDisplay.ts:233`) | `/runs/:id` strip (`pipelineStatusLabel`, `:103`) |
|---|---|---|
| `succeeded` | `SUCCEEDED` | `COMPLETE` |
| `running` | `running` | `EXECUTING` |
| `awaiting_prompt_review` | `awaiting prompt review` | `PROMPT REVIEW` |
| `awaiting_output_review` | `awaiting output review` | `RESULTS READY` |
| `none` | `Not selected` | `Not selected` |

Two formatters over one enum. **Fix:** `apps/dashboard/src/features/compare/ComparePage.tsx:318`
— use `pipelineStatusLabel` and delete `statusLabel`, or keep one and route both screens
through it.

### COR-19 — Tied KPI values are tinted inconsistently

`/runs/compare?ids=run-baseline-a,run-conditional,run-aborted`, TPS AVG row: `run-conditional`
and `run-aborted` both read **82.7**; only the first carries `compare-cell--best`.
`compareModel.ts:110-129` `bestWorst` uses strict `<`/`>` so ties resolve to the lowest
index. Two identical numbers, one green, one not. **Fix:** return all tied indices, or
suppress tinting when the extremum is not unique.

### COR-20 — Demo dialogue predates the run it belongs to

`/runs/run-active` (demo): `/runs` shows `CREATED Oct 17, 12:43:40 PM`, while the Decision
panel shows `OPERATOR · OCT 17, 11:47:48 AM` and `AGENT · OCT 17, 11:48:48 AM` — a
conversation about the run, ~56 minutes before the run existed.
`dev-data/store.ts:2297` creates busy runs at `isoMinutesAgo(65)` while the dialogue at
`:521`/`:528` is anchored at `isoMinutesAgo(127)`/`(126)`. Visible in
`corr-06-test-result-failed-before-gate-decision.jpg`. **Fix:**
`apps/dashboard/src/dev-data/store.ts` — anchor dialogue offsets to the run's own
`created_at` rather than the global activation time.

### COR-21 — Timestamps carry neither a year nor a timezone

`runDisplay.ts:216-225` `formatTimestamp` renders
`toLocaleString(undefined, {month, day, hour, minute, second})` — e.g.
`Oct 17, 01:40:46 PM` on `/runs` CREATED, Home Recent Runs UPDATED, and the run detail
"Run updated". The API returns `2026-10-17T17:40:46.595352+00:00`. Nothing on screen says
whether the displayed time is UTC or local (it is local), and no year is shown, so a run
from a prior year is indistinguishable from this one. The `/runs` CREATED cell carries the
raw ISO as a `title` tooltip; the Home table and the run-detail line do not.
**Fix:** `apps/dashboard/src/features/runs/runDisplay.ts:216` — add `timeZoneName: 'short'`,
add the year when it differs from the current one (as `utils/time.ts` `formatRelative`
already does), and add `title={iso}` at the remaining call sites.

Related, and worth a decision: Home's "Needs your attention" rail uses relative times
(`36m ago`) while the Recent Runs table directly below uses absolute (`Oct 17, 01:40:46 PM`)
— two time vocabularies, 200 px apart, for the same fleet.

### COR-22 — Cost is omitted silently when the role cannot see it

Connected `/analytics` returns `cost_visible: false`, and `AnalyticsPage.tsx:346` /`:534`
/`:615` /`:628` drop the EST. COST tile, the COST TREND chart and the Est. cost column
entirely. Nothing on screen says cost exists but is withheld; demo mode (`cost_visible:
true`) shows `EST. COST $7.56`, so the same screen silently changes shape between modes.
The MEASURE segment still renders a `Cost` button (disabled). **Fix:** render the tile with
`—` and the hint "not available for your role" rather than removing it, at
`apps/dashboard/src/features/analytics/AnalyticsPage.tsx:346`.

### COR-23 — Home's "Not available" chip absorbs `inconclusive` and `not_assessed` (latent)

`HomePage.tsx:202`/`:278` computes `unknownCount = loaded - passedCount - failedCount` and
labels the chip `Not available`, while `executionResultLabel` has distinct
`Inconclusive` and `Not assessed` branches that `/runs` would show. No run in either
dataset currently carries those values, so this does not reproduce today — but the moment
one does, Home and `/runs` will describe it differently. **Fix:**
`apps/dashboard/src/features/home/homeLogic.ts` — count by
`executionResultLabel` rather than by subtraction.

---

## Verified correct

Everything below was checked against the API payload and found to agree.

| Screen | Value | API / source | UI | Verdict |
|---|---|---|---|---|
| Home (connected) | RECENT RUNS | `GET /v1/pipelines?limit=50` → 45 items | `45` | ✅ |
| Home (connected) | ACTIVE | 18 runs `busy`+`interrupted` | `18` | ✅ |
| Home (connected) | FAILURES | 8 runs `execution_result==='failed' \|\| thread_status==='error'` | `8` | ✅ (tile/panel split already confirmed; counts now 8 vs 0) |
| Home (connected) | AVG DURATION | mean of `updated_at − created_at` over 45 runs = **67.777 s** → `formatDuration(67.777)`: `floor(67.777/60)=1`, `round(67.777%60)=8` | `1m 8s` | ✅ arithmetic exact |
| Home (connected) | AVG DURATION caption | — | "loaded runs: creation to last update, not execution duration" | ✅ honest (the only duration label that is) |
| Home (demo) | AVG DURATION | 3780 + 3×9360 + 4×8100 = 64 260 s / 8 = **8032.5 s** → `2h`, `round(832.5/60)=14` | `2h 14m` | ✅ |
| Home (connected) | Test Results chips | `passed`=3, `failed`=0, rest 42 | `Passed · 3`, `Not available · 42`, `Failed · 0` | ✅ sums to 45 |
| Home (connected) | EXECUTION HEALTH | `/v1/analytics/usage` → `runs.phases_succeeded: 50`, `phases_failed: 5` | `50`, `5` | ✅ (the "0 vs —" case in the brief no longer reproduces — the counter is now non-zero) |
| Home (connected) | EXECUTION HEALTH window | API `window.from 2026-10-10T17:42:40Z` → `to 2026-10-17T17:42:40Z` | caption "Last 7 days" | ✅ matches **today**, but hardcoded — see COR-08 |
| Home | attention-rail wait ages | `formatRelative` (`utils/time.ts`) | `36m ago`, `Jun 11` | ✅ correct, incl. year suppression within the current year |
| `/runs` | PROGRESS numerator/denominator | `phase_strip` terminal-count / non-`none` count, all 45 runs | matches on all 45 | ✅ (but see COR-05 wording and COR-06 source split) |
| `/runs` | STATUS label for all 45 runs | `orchestrationStatusLabel(thread_status, phase_strip)` | matches the strip-derived outcome on all 45 | ✅ |
| `/runs` | TEST RESULT = PASSED rows | `execution_result: "passed"` on `01a14ae9-6213/4259/2277` | `PASSED`, `7/7`, `ORCHESTRATION COMPLETE` | ✅ |
| `/runs` | pagination footer | 45 items, `limit=50` | `1–45` | ✅ |
| `/runs` | DURATION arithmetic | `(updated−created)/1000` → `formatDuration` | `7.5s`, `3.9s`, `32m 17s`, `1.1s` all reproduce | ✅ arithmetic (labelling: COR-15) |
| run detail | `x/y` agreement across header, hero counter, phase-nav summary and overview card | one `phaseProgress` call | `4/7` in all four places | ✅ internally consistent |
| run detail | `none` / `excluded` distinction | `phases_plan` excludes a phase | strip omits it; "Phases outside this run" lists it; compare says `NOT SELECTED` | ✅ |
| run detail | `skipped` vs `pending` vs `failed` symbols | `–` / index number / `✕` | distinct glyphs and tones | ✅ (per remediation doc line 9) |
| run detail | error text surfacing | `phase_results[*].errors` on `01a14ad6-1f91` (5 strings) | all five rendered as `role="alert"` paragraphs | ✅ — the already-confirmed "no error text" case applies only to runs where the backend recorded none |
| run detail | missing execution evidence | `test_summary: null`, `execution_result: null` | "Test acceptance: Not available — No acceptance assessment is available in this snapshot. Completing orchestration does not certify a release." | ✅ |
| run detail | omitted execution | `phases_plan` without `execution` | "Not applicable — Execution was not selected; there is no test acceptance decision for this run." | ✅ (grid disagrees: COR-04) |
| compare | baseline selector | explicit `<select aria-label="Comparison baseline">`, defaults to `ids[0]` | baseline cell reads `Baseline`, no delta | ✅ per remediation doc line 15 |
| compare | direction labels | `COMPARE_KPIS[].better` | "HIGHER IS BETTER" / "LOWER IS BETTER" per row | ✅ |
| compare | **error-rate delta in percentage points** | 0.007 − 0.006 = 0.001 → ×100 = **0.10 pp**; relative 0.001/0.006 = **16.667%** | `+0.10 percentage points (16.7%) · Worse` | ✅ **not regressed** |
| compare | error-rate delta, large | 0.028 − 0.006 = 0.022 → **2.20 pp**; relative 0.022/0.006 = **366.667%** | `+2.20 percentage points (366.7%) · Worse` | ✅ |
| compare | error-rate value formatting | 0.006 / 0.007 / 0.028 → ×100, 2 dp | `0.60%` / `0.70%` / `2.80%` | ✅ |
| compare | p95 delta | 3650 − 2410 = 1240; 1240/2410 = **51.452%** | `+1240 ms (51.5%) · Worse` | ✅ |
| compare | p95 delta, small | 2520 − 2410 = 110; 110/2410 = **4.564%** | `+110 ms (4.6%) · Worse` | ✅ |
| compare | TPS delta | 82.7 − 81.2 = 1.5; 1.5/81.2 = **1.847%** | `+1.5 (1.8%) · Better` | ✅ |
| compare | TPS delta, float residue | 82.4 − 81.2 = 1.2000000000000028; /81.2 = **1.478%** | `+1.2 (1.5%) · Better` | ✅ float noise correctly suppressed by `toFixed` |
| compare | neutral equal case | 300 vs 300 | `No change`, no tint | ✅ per remediation doc line 15 |
| compare | neutral missing case | no `test_summary` (connected) | Engine KPIs block absent; phase cells `—` | ✅ *(but see note below)* |
| compare | best/worst tinting direction | `error_rate` lower-is-better, `tps_avg` higher-is-better | lowest error rate `--best`, highest TPS `--best` | ✅ |
| compare | excluded-phase metadata | remediation doc line 42 | `NOT SELECTED · —`, no stale duration or attempt | ✅ in the Phases block (COR-12 in the counts block) |
| compare | `?ids=` parsing | `parseCompareIds` caps at 4, dedupes | 3 ids → `3 RUNS`, three columns | ✅ |
| `/analytics` | ERROR RATE (connected) | `totals.errors 5 / totals.events 56` = **8.929%** | `8.9%` | ✅ |
| `/analytics` | ERROR RATE (demo) | 0 errors / N events | `0.0%` | ✅ |
| `/analytics` | token totals (demo) | 338.0K + 144.7K + 32.5K in = 515.2K; cost 2.26+5.24+0.06 = **7.56** | `515.2K in`, `EST. COST $7.56` | ✅ (tile vs column sums differ only by per-row rounding) |
| `/analytics` | zero-token handling (connected) | `total_tokens: 0`, `events: 56` | `TOTAL TOKENS —` · "awaiting live LLM agents"; measure auto-switches to Latency | ✅ correctly renders missing as unknown |
| `/analytics` | p95 computation | `func.percentile_cont(0.95).within_group(...)` (`agent_analytics.py:262`) | genuine percentile, not an average of per-bucket p95s | ✅ (its *meaning* is COR-10) |
| `/analytics` | top-action counts | `/v1/analytics/usage` `top_actions` is `ORDER BY count DESC LIMIT 10` (`usage.py:856-864`) | not rendered on `/analytics` — that route reads `/v1/analytics/agents`, not `/v1/analytics/usage` | ✅ no UI to disagree |
| `/analytics` | `cost_visible` honoured | `false` connected, `true` demo | cost tile, chart and column present only in demo | ✅ functionally (presentation: COR-22) |
| `/analytics` | bucket totals | single bucket `2026-10-17T00:00:00Z`, events 478 = `by_surface` v1 422 + graph 56 | chart plots one point | ✅ arithmetic (label: COR-07) |
| SSE | snapshot precedence after refetch | ADR-0006 "snapshot + tail" | navigating away and back clears all streamed engine telemetry and the activity feed; the refetched snapshot governs | ✅ invariant holds |
| SSE | `phase_status` patching | `applyStreamEvent.ts:139-190` | patches `detail.phase_strip` **and** `state.phase_results` in one write, plus the list row | ✅ the two sources cannot diverge via the stream |
| SSE | regression guard | `rejectsPhaseTransition` / `rejectsGateTransition` | a stale replayed event is rejected and the list cache is not regressed (`:187-189`) | ✅ |
| SSE | live gate transition | demo `run-active`: `running` → `gate_opened` | header `RUNNING` → `AWAITING APPROVAL`, strip `EXECUTING` → `RESULTS READY`, counter held at `4/7` | ✅ |
| SSE | engine KPI duality | live last sample `vusers 0, tps 84.7` vs snapshot `vusers_peak 300, tps_avg 82.7` | both correct for what they are, but neither is labelled "last poll" vs "recorded aggregate" | ⚠️ noted, not raised as a finding — no stale value survives a refetch |

### Note on compare's missing-result case

In connected mode no run has a `test_summary`, so `hasEngineKpis` is false and compare
drops the **whole** Engine KPIs block — including the `RESULT` row. The comparison view
therefore shows no test result at all for any connected-mode run, while `/runs` shows
`Test result: Not available` for each. This is an omission rather than a wrong value, so it
is recorded here rather than as a finding; moving the `RESULT` row above the `hasEngineKpis`
gate in `apps/dashboard/src/features/compare/ComparePage.tsx:337-369` would close it.

---

## Method notes

- All connected values were read from the production build at `:4175` through the
  same-origin proxy with the admin key; payloads were fetched with `curl` against
  `:2024` and re-pulled at report time.
- Counts of affected runs (18, 19, 10) were derived by replaying the dashboard's own
  `normalizedPhases`, `phaseProgress`, `orchestrationStatusLabel` and
  `executionResultLabel` logic over all 45 `GET /v1/pipelines/{thread_id}` payloads.
- Demo-mode "API values" are quoted from `apps/dashboard/src/dev-data/store.ts`, which
  serves demo mode offline; `/v1/*` is not reachable from the demo origin.
- No application, backend or `package.json` code was modified.
