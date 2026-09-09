# Review fix and backlog follow-up

## Fixed: ambiguous execution evidence

History no longer infers Not applicable from the phase strip. That representation uses `none` for omitted execution, missing legacy data, and imported evidence outside the selected phase plan. Only a full snapshot with an explicit nonempty plan and no execution result, external evidence envelope, or engine handle can establish omission. The detail overview shares this predicate. Recorded pass/fail outcomes remain authoritative.

Regression coverage includes imported unassessed execution, retained handles, external evidence envelopes, legacy snapshots with no plan, explicitly omitted execution, and selected-but-pending execution.

## Completed backlog items

- Engine selector: one keyboard tab stop, wrapping arrow navigation, Home/End, and synchronized focus and selection. Covered by component tests and a production-browser test using offline demo data.
- History identity: full IDs in desktop rows and compact cards, plus unique Inspect accessible names even when titles match. Covered by duplicate-title regression tests and browser inspection.

## Validation

Production build passed. ESLint: no errors, 22 existing warnings. All 26 focused tests passed. Both existing production-browser workflows passed, and the new engine keyboard workflow passed separately. Final full suite: 887 tests passed across 117 files.

## Remaining backlog

The subsequent cross-app implementation adds release mappings with a scoped release picker, report-section URLs and return navigation, and versioned structured assessment states in both applications. See [the service contract and rollout notes](../../../contracts/apex-load-v1/README.md). Native integration end-to-end validation still requires an enabled project-scoped Load connection; local contract tests and isolated Load browser workflows do not certify that deployed handoff.
