# UX implementation code review — 2026-09-09

Reviewed the uncommitted Orchestration UX implementation and the companion Load return-link integration. All actionable findings below were fixed in this workspace. No companion Load source changes were needed during this review.

| Priority | Finding | Fix and regression coverage |
| --- | --- | --- |
| P1 | Native summary reused managed-run ownership validation with a newly generated idempotency key. Legacy archives from manually named native runs were rejected, losing the assessment. | Verify native test identity and configured project before reading evidence; keep managed ownership checks for managed runs. Added native legacy archive and cross-project rejection tests. |
| P2 | Malformed percent escapes in a Load execution URL could throw during rendering. | Decode and validate execution identifiers inside the guarded parser; tests cover malformed escapes, invalid UTF-8, and encoded path separators. |
| P2 | Overview could display Passed/Failed alongside “Acceptance has not been assessed,” or use a stale list verdict for explicitly unassessed evidence. | Use one acceptance verdict for label and fallback explanation; explicit unknown execution evidence takes precedence. Added verdict and stale-summary tests. |
| P2 | Overview discarded recorded phase coverage when state schema validation failed. | Reuse the parent’s normalized phase progress; added schema-drift coverage regression. |
| P2 | Existing run-level tab links stopped reaching phase workspaces. | Preserve legacy tab deep links and map activity to Pipeline Log. Added route regression and verified in Chrome. |
| P2 | Invalid phase recovery text had no actionable navigation. | Restore an overview link; tested and clicked through in Chrome. |
| P2 | URL-driven connection changes could briefly query a new connection using the previous scenario. | Clear dependent selections in the same update as the connection change; test records scoped schedule requests across navigation. |
| P2 | Demo mirrored-run fixtures used obsolete field names, leaving ledger identities, analysis statuses, and links blank. | Align fixture fields with the current API projection; added rendered ledger regression. |

## Validation

- Full Orchestration dashboard suite: 115 files, 877 tests passed before the final acceptance and demo-fixture follow-ups.
- Final affected suites: 88 tests passed (run detail, demo store, demo routes), including the two additional regression cases.
- Backend adapter and native service suites: 194 tests passed.
- Companion Load return-link suite: 8 tests passed.
- Final production TypeScript/Vite build passed.
- Full dashboard lint: zero errors; 22 existing Fast Refresh warnings. Final changed-file lint passed.
- Ruff and git diff whitespace checks passed.
- Chrome verification on the local built dashboard: legacy activity route selected Pipeline Log; invalid phase recovery returned to the outcome overview.

Native populated assessment paths were verified through backend test doubles; the local instance has no enabled native Load connection for a live end-to-end assessment check. Existing runtime logs and unrelated working-tree changes were preserved.
