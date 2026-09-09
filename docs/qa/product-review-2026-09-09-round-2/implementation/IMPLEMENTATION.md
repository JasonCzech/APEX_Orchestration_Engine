# Round 2 implementation

Implemented the review's directly supported workflow improvements in the existing applications.

- Orchestration outcome labels distinguish completed, failed, stopped, and awaiting-review plans. An omitted execution is Not applicable; successful orchestration never implies acceptance. Overview uses the same status projection as history and Home.
- Run and draft scope is visible in setup, run detail, and approval preview. Catalog application/environment labels resolve exact IDs. The project link opens the project-scoped Load integration workspace. Project suggestions come from accessible catalog entries; manual canonical IDs remain supported because there is no authoritative project-picker API for unscoped administrators.
- Approval rows show full run identity, precise timestamps, and friendly phase names. Decision context names the next selected phase from persisted state, or explicitly says that the next phase is unknown. Arrow selection also moves focus.
- Setup marks required fields and focuses the exact field from Review issue links. Optional preset failures explain how to continue. Work item query preparation and saved-query connection recovery use plain language.
- Review policy and theme choices use native radio controls. Review policy counts planned prompt/output pauses across selected phases. Review gives scope and configuration more room. Navigation labels wrap and the active run section is distinct.
- Home explains the metric population, seven-day phase summary window, and observation time; short durations retain seconds. Drafts show project context. Preferences offer the same 10/25/50/100 History row options. Reload recovery and notification links describe their destinations.
- Load report headlines show the actual recorded termination reason, planned duration, and observed elapsed time together. The headline and outcome panel share a formatter. Unknown timing remains unknown. Incomplete comparison records now render unavailable values instead of crashing.

## Verification

Orchestration: 883 unit tests passed across 117 files. Both production browser demo workflows passed, including the full offline Jira Align → Jira Cloud journey and persisted Load integration fixture mutations. Production build passed. ESLint completed with 0 errors and 22 existing warnings. Browser verification confirmed approval identity, arrow-key selection/focus, omitted-execution outcome, selected navigation, scope links, and Review-to-Title focus. A narrow viewport check had effective CSS width 487px (the browser's existing zoom affected the requested override); document width matched viewport width. The override was reset.

Load: 13 focused outcome/report tests passed. Build passed. Browser verification of the existing web wiki run showed planned 20m0s alongside observed 277s and Unknown termination reason. Full test:cycle was run twice against the concurrently changing Load checkout; neither full run passed. The latest run had 3,943 passing and 30 failing tests across 364 files, followed by a focused report fix. This is not a clean whole-Load certification. Load workflow results: dev-admin 74 passed / 2 failed; local-auth 8 passed / 1 failed. Dev-admin failures still target the old API Guide and Save & Create Test Run labels from parallel changes. The local-auth workspace-creation workflow timed out. Report retrieval, lenses, archive recovery, lifecycle, and responsive/theme workflows passed. These broader failures remain outside the report change and must be reconciled before certifying the combined Load checkout.

## Remaining integration and validation limits

The local Orchestration environment has no enabled project-scoped Load connection, so a genuine native launch/analysis/return round trip remains unverified. Release mappings, evidence-section deep links, and a cross-app structured assessment contract need supported service contracts and a populated integration to finish. The current Orchestration summary cannot reliably distinguish missing criteria from unavailable assessment data, so it retains Not available rather than inventing Not assessed. Load displays its structured Not assessed verdict directly.

This work does not establish full accessibility conformance, all-theme contrast, or user-study outcomes. It preserves concurrent changes and does not deploy or launch real workloads.


## Subsequent cross-app implementation

The deferred release mapping, report-section deep link, and structured assessment contracts are now implemented in both checkouts. See [the version 1 contract](../../../contracts/apex-load-v1/README.md) for compatibility, immutable run identity, and rollout details. A deployed native handoff remains unverified without an enabled project-scoped connection.
