# Cross-application contract code review — 2026-09-09

Reviewed the outcome producer/consumer, release selection and retry binding, and report/return navigation in Orchestration and the companion Load checkout. Existing unrelated changes were preserved.

## Findings fixed

- **P1 — Contradictory assessment at the execution boundary.** A provider/checkpoint summary could retain `passed: true` alongside a failed or unassessed structured outcome. `TestResultSummary` now derives the legacy boolean from the structured status during validation, including checkpoint restoration. Regression tests cover all five states.
- **P1 — Retry could drop release identity after mapping removal.** An unmapped connection could adopt a previously scoped execution and return an unscoped handle. Provisioning now rejects orphan mapping revisions before remote calls and rejects adoption of a scoped execution without its mapping.
- **P2 — Saved connection misrepresented as project default.** An unavailable connection remained in the launch payload while the picker displayed the default. The picker now shows the unavailable identity and requires an explicit replacement. Cached releases cannot be used for an ineligible connection; failed/loading capability data disables release selection.
- **P2 — Namespaced execution links rejected.** The adapter permits colon-containing execution IDs, but the browser URL validator rejected their unescaped form. Links now accept both raw and percent-encoded colons while preserving the exact return binding.
- **P2 — Canceled report navigation changed the visible section.** Load selected a report section before its unsaved-assessment guard committed navigation. The visible section now follows the committed route. A regression test verifies that an uncommitted request leaves the current section visible.

## Validation

- Orchestration: 390 affected backend tests and 101 dashboard tests passed.
- Orchestration dashboard typecheck, focused ESLint, backend Ruff, and whitespace checks passed.
- Load: the full `npm run test:cycle` passed: typecheck, lint, 4,002 unit tests, production build, 78 dev-admin browser workflows, and 9 local-auth browser workflows. The two Settings workflow failures recorded in the earlier validation did not recur.

This review does not establish a deployed cross-application handoff. The earlier broad Go-suite timeout remains a separate validation limitation; this review made no Go changes.
