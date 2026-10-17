# Holistic UX remediation — APEX Orchestration

Implementation belongs to APEX Orchestration, on branch `codex/holistic-ux-remediation-2026-10-17`. APEX Load was read as the design reference; its application was not changed. The working branch integrates Orchestration main `8526c2a`.

## Scope and evidence

The October 17 audit's recommendations were grouped into shared presentation, truthful state and metrics, approval provenance, gate safety, scoped authoring, Cycles, and resource workflows. Each logical change received an independent review, correction, and repeat review. Cross-cutting reviews checked state claims, authorization, asynchronous updates, keyboard focus, cold-load styling, and the interactions between shared components.

Detailed dispositions live in [governance and authoring](holistic-ux-remediation-governance-2026-10-17.md), [correctness and controls](holistic-ux-remediation-correctness-2026-10-17.md), and [accessibility and review](holistic-ux-remediation-accessibility-2026-10-17.md). These describe actual evidence and retained environment limitations; they do not certify WCAG conformance or real-provider operations.

## Shared presentation and convergence

| Findings | Implementation |
| --- | --- |
| CON-01, CON-06 | Notices own global styles, including success; dialogs use the common backdrop and z-index contract. Cold-load browser checks do not rely on visiting a run first. |
| CON-04, CON-09, CON-10 | Not-found copy names the missing route, tokens have usable definitions, and theme/motion preferences apply before configuration loads. Theme documentation matches the six shipped themes. |
| LOAD convergence, A11Y-01, A11Y-02, A11Y-14 | Shared Load theme values, separate accessible accent text, local fonts, six-step typography, sentence-case tokens, flat shell, form surfaces and color-scheme. Badges use accent contrast; editor chrome and diff styles use code-surface colors. |
| AGT-04, AGT-07 | Distinct review chip styles and phase connectors retain their actual phase state. |
| CON-02, CON-05, CON-11, CON-16, CON-17 | Shared tabs, linked panels, state cards, skeletons, table controls, menus, pagination, copyable identities and formatting replace local variants. State and query controls retain their data and authorization semantics. |

The contrast script resolves aliases and composites translucent surfaces. Production browser checks cover six themes, 390/768/1440 layouts, delayed configuration, and cold-load notices. Existing chart axis/grid token wiring and keyboard tab behavior are retained.

## Cycles and resources

| Findings | Implementation and limits |
| --- | --- |
| ARE-10, CON-07 | All five version review actions identify the exact version, action and consequence in confirmation; archive is last and destructive. Results name the completed action. Revision requests are visible and stale note/saved messages clear. |
| ARE-12 | Conflicts preserve authored content and server error detail. Reloading the latest cycle revision does not discard the draft; revising a superseded version requires an explicit rebase onto its latest successor. |
| ARE-11 | Cancel editing and navigation share the dirty guard. Switching cycle steps remains possible through that guard. |
| ARE-13 | Cycle creation uses scope-aware launch capability; access denial never claims a known 404. |
| ARE-14 | Next action follows the first incomplete deliverable's state; overview includes actual execution preparation/link counts. |
| ARE-15, ARE-16, ARE-18 | Empty connections explain their remedy. Launch owner appears only when there is a choice. Preflight checks group by outcome, resolve environment names, and show counts. Integer fields, inventory snapshot identity and unsupported arrival-rate scheduling are explicit while authoring. Existing provider/dispatch capability gates remain authoritative. |
| ARE-16 preparation lifecycle | Unused preparations can be archived after confirming their exact identity and cycle revision. Archiving blocks new validation, preserves bindings and historical validation receipts, and does not cancel already-started provider work or linked runs. Archived items are available through an explicit history toggle. Stale confirmations require reopening against current data. |
| ARE-17, ARE-21, CON-08, CON-14, CON-16 | Linked runs resolve available titles, retain full copyable IDs and label unavailable metadata honestly. Timestamps include year and the persisted Local/UTC display preference; the authenticated consumer is named when identifiable. Headings do not introduce another page h1. |
| ARE-19 | Deliverable comparison emphasizes changed fields and supports multiline comparison and unchanged-field disclosure. |
| ARE-20 | Captured evidence selects the newly created version; capture titles use actual capture time without accumulating suffixes. |
| ARE-22 | Project fields use authorized scope choices or catalog-backed suggestions. Document filenames open bounded details/preview, full identity and guidance to the wizard's Knowledge step. Delete uses the shared menu and existing explicit confirmation. |
| ARE-23 | Evidence run selection searches title, date/status and identity; duplicate titles remain distinguishable after selection. Keyboard navigation scrolls the active option into view. Missing recent-run metadata preserves the recorded run link and does not assert absent associations. |
| ARE-26 | Hand editing invalidates translation confidence. Save shows its exact query and required name, and save/edit announce completion. Provider has a visible label; result pagination retains the submitted query and connection. |

Cycles verification includes 42 focused tests and production browser workflows for viewer, unscoped operator, scoped operator and admin. Resource verification includes 96 focused tests and production browser workflows for viewer, operator and admin, with source-backed mocks of the connected API contract. Those browser tests exercise the production bundle rather than the development authentication bypass.

## Cross-cutting corrections

The final integrated review also checked transitions that individual pages cannot verify alone:

- Canonical review links retain their requested run when the separate queue cache is stale, empty or unavailable. Local revisions stay with their run while the next review is being prepared.
- Imported execution references survive unrelated snapshot schema drift; neither a reference nor a pending summary is promoted to a collected test result.
- Analytics preserves missing buckets as gaps and excludes unmeasured values from ranking. Average latency is weighted by the API's recorded timing sample count, never by all events or substituted percentiles. Legacy responses without those counts disclose that aggregation is unavailable.
- Cold wizard prompt viewers own their stylesheet and keyboard focus contract; editing retains a visible caret and Tab does not modify prompt content.

Representative browser captures: [comparison](../../output/playwright/correctness-compare.png), [analytics](../../output/playwright/correctness-analytics.png), [shared controls](../../output/playwright/shared-controls-mobile.png), [document preview](../../output/playwright/resources-document-mobile.png), and [evidence selection](../../output/playwright/resources-evidence-mobile.png). Captures record their verification checkpoint; regression tests cover subsequent changes.

## Integrated verification

| Check | Result |
| --- | --- |
| Full backend suite | 5,838 passed, 23 skipped; one upstream Starlette deprecation warning. |
| Full dashboard suite | 1,073 passed across 135 files. |
| Shared event contracts | 47 passed. |
| Production browser suite | 32 passed, including the complete offline launch/review journey, role-specific Cycles and resources, review safety, six themes, cold-route controls, responsive layouts and cross-tab timestamp preferences. |
| Final focused production rerun | A fresh bundle and all 13 affected review, accessibility and demo workflows passed after the last effect-dependency adjustment. |
| Type checking | Dashboard TypeScript and backend Pyright passed. |
| Python lint and formatting | Ruff passed; all 394 checked files formatted. |
| Dashboard lint | No errors; 23 Fast Refresh export warnings remain. The new effect-dependency warning found during final review was corrected. |
| Theme contrast | 750 declared text/surface pairs passed at 4.5:1 or above; six checker tests passed. |

The final review rounds were clear after correcting stale canonical-review navigation, retaining a reopened review until its queue catches up, restoring the visible CodeMirror cursor, and removing the unstable effect dependency. Focused tests and production workflows were repeated for the final adjustments. Review coverage includes authorization, missing-data claims, asynchronous state transitions, keyboard interaction and the shared primitives across routes.

## Rollout

Run the normal database migration step before starting this version. Migration `0039` adds nullable archive time and actor fields to execution preparations; existing bindings and validation records are retained. The new archive operation is covered by the authorization matrix and schema-readiness checks.

The work remains on the local remediation branch; no deployment or production data migration was performed.

Task-owned browser sessions and servers were closed. Cleanup removed 41 Python/Vite/TypeScript cache paths (32.73 MiB) and 32 disposable browser files, preserving dependencies, deliverable bundles and the five verification captures. No Go or Docker builds were used. The temporary audit copy was verified byte-for-byte before removal; the original audit and captures remain unchanged in `docs/qa/ux-holistic-2026-10-17/` within the sibling `APEX_Orchestration_Engine` checkout.

## Boundaries

Historical audit actors cannot be reconstructed; the new path attributes future decisions to the authenticated consumer. Historical agent latency that included review waits cannot be reconstructed either. Missing metrics and titles remain unavailable rather than being synthesized.

A configured external provider, live production stream, external ticket creation, publication, retrieval connectors and cross-product APEX Load handoff still require their actual services and credentials. The historical `/assistants/search` deployment 500 was not reproduced on the integrated main: current contract violations were corrected and covered, without claiming an unseen runtime cause was solved. These are verification limits and explicit product capability states, not simulated evidence of successful integration.
