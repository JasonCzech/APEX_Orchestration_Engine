# New-test pipeline review — remediation record

Branch `claude/new-test-pipeline-fixes`. Each change set was reviewed by an independent agent before it was committed, and the findings that review raised were fixed in the same commit.

| Finding | Status | Change |
|---|---|---|
| F-01 Launch fails: missing prompt manifest | Fixed | The `/v1` facade prepares the manifest before `runs.create`. The SDK's in-process transport (`root_path=/noauth`) skips LangGraph auth, so the native run-create hook never runs for facade launches. Selections without a preparer are rejected up front, and so are a client-supplied `prompt_manifest` or `apex_actor_id`. |
| F-02 Failure reason hidden | Fixed | Pipeline detail carries a safe `failure` derived from the thread error. Known causes get authored text; anything else gets a generic next step plus its exception type, and raw text is never shown. The reason appears on the Overview and as a Timeline event. |
| F-03 Recovery dead-ends | Fixed | A run that failed before a plan was recorded can be copied from its submitted configurable, behind the same replay barriers as a durable copy. The Overview offers **Edit and relaunch**, and the re-run button that could not work is hidden. Refusals name the next step; outages return 502. |
| F-04 Logs show unrelated entries | Fixed | The stub log source returns nothing for a run deep link. The Logs page warns when a source ignores the requested window and has a run-specific empty state. |
| F-05 Footer takes over the screen | Fixed | The footer is one row (59 px at 1440×900, down from 123 px). Open items sit in a disclosure that opens above the footer, inside its width; Escape, choosing an item, or changing step closes it. Readiness is announced by one always-mounted live region. |
| F-06 No KPI vs threshold view | Fixed | The public execution entry exposes the thresholds the engine applied. A **Requested vs observed** table appears on the Overview and in the execution, reporting and postmortem reviews. Only the simulator gets per-row Met/Not met; for real engines the table shows the engine's own verdict and never contradicts it. |
| F-07 Config validation and units | Fixed | Ramp-up longer than duration is rejected. Ramp and duration accept seconds, minutes or hours (stored as seconds, rounded to milliseconds). Engine-specific default durations were not changed. |
| F-08 Unstyled participant row | Fixed | Each participant is a card with a field grid and role chips. Add buttons are hidden once a layer is full. |
| F-09 Misleading Scope readiness | Fixed | Readiness links focus real fields, and stale "Project is required" is gone. Retry appears only when a check has failed. Environments show their target-approval state. The intro copy is updated, and the draft-access caption only appears when it applies. |
| F-10 Raw-ID legacy clone, identical titles | Fixed | `test_scope` now survives a copy (the response model was dropping it). Legacy selections show names and a one-click **Use as backend**. Copies are titled "(copy)", then "(copy 2)", and so on. |
| F-11 CTA overlaps text | Fixed | Link-styled buttons are inline-block. |
| F-12 Navigation changes context | Fixed | One page title, "Run detail", across overview, workspace and timeline. Header actions keep a fixed position. A run awaiting a decision opens on that decision inside Run detail; `?view=overview` keeps the Overview reachable. |
| F-13 Tests list raw IDs and wrapping | Fixed | The list shows application names. The progress label is plain text, buttons no longer wrap, and the split-view rail can no longer be widened by long titles. |
| F-14 Internal jargon | Fixed | "LangGraph"/"HITL" become "Agent"/"Person". Toggles keep a stable name with `aria-pressed`. The "default (default)" label is fixed. |
| F-15 Opaque ticket query | Fixed | The provider query was already editable; the hex chip was the connection ID and now shows the connection's name. Selected tickets show their titles. |
| F-16 No document/threshold reconciliation | Fixed | Review warns when an attached document's extracted preview states a different P95 or error-rate threshold. The check is a conservative text match. |
| F-17 No per-step state in tabs | Fixed | Each step tab shows its outstanding count, or ✓ for completed required steps. |
| F-18 Narrow Readiness card | Fixed | Readiness spans the full Review grid. |
| F-19 Long page fade | Not reproduced | The entry fade is 220 ms. The 1–2 s dim seen during the review came from the browser pane being hidden, which throttles CSS animations. No change. |
| F-20 Review gate repetition / Abort wrap | Fixed | The duplicated accept/request-changes sentence is removed. Each decision's scope note sits under the buttons, so they share one row. |

## Follow-ups

| Item | Status (2026-10-23) |
|---|---|
| Model admission (`admit_models`) is also skipped for facade launches | **Fixed in PR #26.** Facade launches and reruns admit before `runs.create` ([FACADE_CONTROLS.md](FACADE_CONTROLS.md)). |
| Facade launches have no `apex_actor_id` attribution | **Fixed in PR #27**, with usage consumer names in PR #28. |
| Demo-mode journey e2e stops at Config | **Fixed in PR #26** (`8803ac2`). The offline dataset now serves an analysis connection, and the journey launches. |
| Production runtime untested | **Done** ([PRODUCTION_RUNTIME_CHECK.md](PRODUCTION_RUNTIME_CHECK.md)). The check found that every wizard launch failed on the Postgres runtime (PR #30) and that the Compose stack could not start as shipped (PR #29). Both are fixed. |
| No runtime-level integration test | **Open.** No automated test launches through `/v1` against a real LangGraph runtime. Both launch-breaking bugs (F-01 and the Postgres status-code bug) passed unit tests and were only caught by hand. A Postgres-runtime launch in CI would cover both, but it needs a license or LangSmith key in CI. |
| Compare doesn't show acceptance thresholds | **Open.** Found while exercising Compare on 2026-10-23. |
| APEX Load handoff | **Not exercised.** It needs a live APEX Load connection. |
| AKS MinIO manifest uses the unpullable `minio/minio` image | **Open.** PR #29 fixed Compose only. |

## End-to-end verification (2026-10-22, local `langgraph dev`, production bundle)

A new test was built from scratch in the wizard at 1024×768 (one backend, Simulated engine, P95 ≤ 300 ms, 5 s ramp, 1 min duration) and launched. It completed 7/7 phases, and the Overview compares P95 ≤ 300 ms with the observed 212 ms (Met). Opening a run that is awaiting review now lands on its Reporting decision inside Run detail. A run that had failed with the old manifest bug offered **Edit and relaunch**, which produced a "(copy)" draft with its topology intact.

| File | Shows |
|---|---|
| [after-01-scope-card-1024](screens/after-01-scope-card-1024.jpg) | Participant card, role chips, one-row footer |
| [after-02-review-1024](screens/after-02-review-1024.jpg) | Review: Agent labels, workload in seconds, compact footer |
| [after-03-run-complete-kpis-1024](screens/after-03-run-complete-kpis-1024.jpg) | Completed run with Requested vs observed |
| [after-04-decision-first-review](screens/after-04-decision-first-review.jpg) | A pending decision opens in Run detail, with KPIs beside it |
| [after-05-failed-run-relaunch](screens/after-05-failed-run-relaunch.jpg) | A failed run shows its reason and Edit and relaunch |

Automated checks at the end of the branch: backend unit suite 6,586 passed (15 skipped), ruff and pyright clean; dashboard 152 test files / 1,311 tests passed, `tsc -b` clean, ESLint with no new warnings; `check:contracts` passed. At that point the demo-mode journey e2e (`e2e/demo-mode.spec.ts`) still stopped at Config; it was restored later in the same PR (see Follow-ups).
