# Dashboard usability remediation

Implemented September 7, 2026. The dashboard now separates orchestration state from recorded test outcomes. Completing orchestration does not imply a passed test or release certification.

## Changes

- The pipeline summary/detail API adds optional `execution_result` (`passed`, `failed`, or `null`) from the validated execution test summary. OpenAPI and the TypeScript client are regenerated. Missing results render **Not available**, including responses from older backends.
- Home shows **Test Results** for the explicitly loaded sample. Approval work precedes secondary metrics; the unavailable Tickets metric is removed. History uses separate result/status fields and labels its project data **Project**.
- Detail, inspector, comparison, and phase counters share phase normalization. Selected but unfinished phases remain pending; excluded phases are not selected. Snapshot state takes precedence, with summary fallback identified when parsing fails. Completed, failed, aborted, and skipped phases have distinct symbols.
- Demo summaries derive from their state. Baseline postmortems agree with their counters. Newly simulated execution records its failed SLA result.
- Compact navigation exposes primary, Advanced, identity, theme, and sign-out controls. The modal supports keyboard containment, Escape dismissal, and focus return. History switches to cards; phase navigation collapses on compact screens.
- Operational results and review actions precede collapsed prompt configuration. Workflow status and live-update transport status have distinct labels.
- The wizard uses **Review Test** before Review and **Launch Test** only on Review. Review resolves catalog names, document readability/inclusion, work-item titles/keys, selected phases, and gate policy. IDs remain copyable and launch JSON is under technical details.
- Markdown artifacts render as wrapped documents with Source and Download controls. Raw HTML and unsafe link schemes are disabled; remote images render as alt text. Validated internal return destinations preserve the selected approval or phase, with a run fallback for direct links.
- Saved queries retain their identity and show **Modified query** when edited. Comparison has an explicit baseline, direction labels, absolute and relative differences, and neutral equal/missing cases. Error-rate absolute changes use percentage points.
- Gate names use operator labels. A skip link and route focus support keyboard navigation. Workflow and draft-save statuses are announced separately from streaming metrics. Shared colors were adjusted across six palettes, and reduced-motion preferences disable repeating animations and transitions.

## Validation performed

- Dashboard: **816 tests passed across 101 files**. Coverage includes missing/failed results, conflicting phase summaries, partial plans, keyboard modal behavior, safe artifact rendering/returns, wizard review gating, comparison differences, and demo lifecycle consistency. Existing recovery, authorization, stream, and concurrent-approval tests remain passing.
- Backend: **129 focused tests passed** in pipeline facade and public projection suites, including strict result extraction and partial phase plans. Backend Pyright: **0 errors, 0 warnings**.
- Production build and TypeScript compilation passed. Markdown has a separate bundle; the final build has no chunk-size warning. ESLint has no errors and 22 non-blocking warnings, primarily React fast-refresh export warnings. Python Ruff checks passed for changed Python files.
- `uv run python scripts/check_dashboard_contrast.py` checks 43 shared text/control token pairs in each shipped theme. Minimum ratios: dark 4.63:1, light 4.59:1, APEX light 4.63:1, Solarized dark 5.12:1, Solarized light 4.61:1, Monokai dimmed 4.68:1.
- Browser verification used the production build in offline simulation. Exercised draft → Review → launch → prompt approval → environment revision → execution review → reporting approval → resulting simulated PERF-248 ticket. Separately verified report return to the same selected approval, saved-query identity/edit state, baseline switching, and completed-run phase consistency.
- Checked compact layouts at 390 and 768 CSS pixels and desktop at 1440. History and launch review had no document-level horizontal overflow at 390 pixels. Verified drawer Advanced destinations, Escape focus return, and collapsed phase navigation. Evidence is in `/tmp/apex-usability-remediation`.

An unrelated connection-settings test timed out in an earlier full run; subsequent full runs passed. No application workaround was added for that timeout.

## Release sequence and remaining checks

1. Deploy the additive API field first, then the dashboard. No database migration is required. Older APIs remain safe: the dashboard displays unavailable results instead of inferring a pass.
2. Smoke-test a successful orchestration whose authoritative test result fails and a run with no recorded test result. Confirm History/Home and execution evidence agree.
3. In a seeded connected environment, verify real authentication/expiry, provider execution, approval mutations and contention, external ticket creation, and production recovery. Offline browser checks and mocked tests do not verify those integrations.
4. Complete assistive-technology testing with a screen reader, keyboard-only lifecycle, 200% browser zoom, and reduced motion. The token contrast check is reproducible evidence for shared colors, not a full WCAG conformance audit of every rendered chart, overlay, or provider-supplied artifact.

No deployment or real external ticket creation was performed.

## Follow-up code review

Reviewed the implementation and fixed these findings:

- **P1 — Stale phase status after schema drift:** a global parse-failure flag replaced independently valid phase statuses with summary data. Phase normalization now retains valid fields, consults summaries only for missing statuses during drift, rejects unrecognized phase plans, and preserves pending selected phases. Valid empty snapshots no longer silently inherit stale completions. Comparison also omits old duration/attempt metadata for excluded phases and preserves fallback attempts safely.
- **P2 — Saved-query identity drift:** unbound queries were marked modified after automatic connection resolution, and prior query labels survived a new URL/project. Bind the selected query to the resolved connection after execution and clear its identity when a new console context starts, while preserving explicit cross-project saved-query selection.
- **P2 — Modal keyboard isolation:** approval shortcuts remained active inside the navigation drawer, allowing navigation or focus changes behind the modal. Approval shortcuts now ignore events originating in dialogs.

Added regression coverage for each finding. The full dashboard suite passed **820 tests**; subsequent focused checks and the final production build/TypeScript compilation passed. ESLint reports no errors (22 existing/non-blocking fast-refresh warnings). No backend behavior or deployment changed during this review.
