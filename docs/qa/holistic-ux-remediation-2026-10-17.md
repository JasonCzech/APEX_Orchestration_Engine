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

The initial remediation and first cross-cutting follow-up were pushed to main at `822b412`. No production deployment or production data migration was performed.

Task-owned browser sessions and servers were closed. Cleanup removed 41 Python/Vite/TypeScript cache paths (32.73 MiB) and 32 disposable browser files, preserving dependencies, deliverable bundles and the five verification captures. No Go or Docker builds were used. The temporary audit copy was verified byte-for-byte before removal; the original audit and captures remain unchanged in `docs/qa/ux-holistic-2026-10-17/` within the sibling `APEX_Orchestration_Engine` checkout.

## Boundaries

Historical audit actors cannot be reconstructed. The independent re-review of `822b412` showed that the original attribution implementation did not reach gate resumes through the HTTP facade; the earlier claim that it attributed new decisions was incorrect. The correction and native-runtime verification are recorded below. Historical agent latency that included review waits cannot be reconstructed either. Missing metrics and titles remain unavailable rather than being synthesized.

A configured external provider, live production stream, external ticket creation, publication, retrieval connectors and cross-product APEX Load handoff still require their actual services and credentials. The independent re-review reproduced the `/assistants/search` 500 on `822b412`: native task groups wrapped request-guard exceptions before the middleware could map them. Earlier checks covered the corrected dashboard request shape but missed the rejected native request path. The server correction is recorded below. These are verification limits and explicit product capability states, not simulated evidence of successful integration.

## Follow-up cross-cutting review

A fresh review of the integrated Orchestration branch reproduced and corrected these additional issues:

- Execution preparations, provider imports and follow-up actions retained an idempotency key after confirmed success. Explicitly repeating the same action could replay an old result, including an archived preparation. Confirmed success now completes the key's lifetime; uncertain retries retain their original key.
- Analytics selected its bounded chart population using the table sort, so a chart labelled as the top groups could omit the actual winner. Chart queries now select by the displayed measure over the same fixed time window. Missing measurements sort after measured zero values, and the chart discloses its bounded population.
- Large Unicode prompts or combined phase output could hide an otherwise valid review gate. Output producers fit previews within the existing byte and node limits, preserve whole fields where possible, and explicitly mark shortened or omitted content. Public readers preserve valid required decisions when optional evidence cannot fit. Editable prompt bodies remain complete and receive a separate finite allowance derived from their existing character limits and worst-case JSON encoding; the rest of the gate retains its existing envelope limits. The pending-gate list shares one body allowance across its aggregate, with the existing envelope, node and count limits retained. Durable evidence is unchanged.
- Public audit history had lower record limits than durable history; large snapshots could also discard every approval. Projection now retains bounded recent history, including each phase's latest decision, and identifies incomplete previews. Long legal discussion messages no longer erase all visible dialogue.
- Schema-drift fallback accepted malformed nested engine, review and dialogue values that could crash React or CodeMirror. It now validates those values independently and preserves valid sibling fields. Overview, phase and gate surfaces describe omitted evidence as unavailable rather than asserting it was never recorded.
- Phase details could publish a test verdict before execution succeeded and did not consistently display structured assessments. It now uses the shared result-label contract and requires successful execution before showing collected KPIs.
- An application-only document filter showed the ordinary empty-library state. It now shows a filtered result with a working clear action.

Each correction received regression coverage and independent review. Further findings, including malformed native mapping keys and nested optional evidence, were reproduced, corrected and reviewed again. Required payloads validate keys before field comparisons; optional evidence receives bounded structural validation before projection. Absent evidence remains distinct from omitted evidence. The final independent cross-cutting review found no remaining actionable issues in the reviewed changes.

| Follow-up verification | Result |
| --- | --- |
| Full backend suite after final fixes | 5,895 passed, 23 skipped; one upstream Starlette deprecation warning. |
| Full dashboard suite | 1,098 passed across 135 files. |
| Shared event contracts | 48 passed. |
| Final production browser suite | 38 passed. Includes viewer/operator handling of partial evidence, complete large Unicode prompt content and Tab navigation, analytics ranking, and the existing role, theme and workflow checks. |
| Production build and type checking | Dashboard production build and TypeScript passed; full backend Pyright passed with zero errors or warnings. |
| Python lint and formatting | Ruff passed; all 395 checked files formatted. |
| Dashboard lint | No errors; 23 existing Fast Refresh export warnings. |

The branch was integrated with current `origin/main` before final builds and full backend validation. The code corrections were committed in five logical changes: `22b4003`, `517d794`, `224e297`, `1ae8053` and `7b6c80d`. Each commit followed a clear review round. No APEX Load source was changed.

Follow-up cleanup removed 40 Python/Vite/TypeScript cache paths (16.50 MiB) and one disposable browser result file. Removal was verified; dependencies, production bundles and all tracked captures were preserved. No task-owned Playwright/browser or test-server process remains. No Go or Docker builds were used. The rollout requirements and external-service verification boundaries above remain unchanged.

## Independent re-review corrections

The independent live review of `822b412` exposed two gaps in the earlier verification and four remaining presentation issues. The earlier attribution and native-search claims above have been corrected rather than treated as historical-data limitations.

| Finding | Correction and evidence |
| --- | --- |
| L-01 approval attribution | The resume route now forwards the authenticated consumer to the service. A server-owned runtime actor field survives native request validation and LangGraph's configurable-to-context copy; the auth hook accepts a supplied actor only from the trusted facade and only when it matches the authenticated consumer, then stamps both captured mappings. The runtime actor field is excluded from durable application configuration and public state; approval records retain the attributed consumer. Two separately authenticated project-scoped operators exercised approve, discuss, revise, approve and skip through the actual local native HTTP runtime. All five persisted actors matched the deciding consumer; a stale competing resume returned 409. |
| ARE-07 native request failures | The body middleware unwraps guard-only exception groups, preserving 422 for invalid input, 413 for oversized bodies and 408 for timeouts. Mixed server failures, cancellations and errors after response start still propagate. Actual `/assistants/search` requests with `{}` and a limit of 20 returned 422; the valid five-item summary request returned 200. Real AnyIO task-group regressions cover nested guards, error disclosure and response ownership. |
| S-01 demo empty activity | Empty demo feeds state that no demo activity was captured for the phase, with an actionable link to Agent reasoning and available transcripts. Populated simulated feeds retain their existing streaming behavior. The production browser test verifies the empty active-run view, keyboard focus on the destination tab and its transcript link. |
| Application identity | Run context and the scope row share the same project catalog resolver. Both display the available application name and retain its ID as the title; unavailable catalog entries retain the recorded ID. Production checks cover demo and connected viewer/operator sessions. |
| Chart contrast | The two failing apex-light series were darkened. The checker now enforces 3:1 for all eight series against the composited chart panel in every theme, in addition to the existing 750 text/surface checks. Production browser tests measure the rendered panel for all six themes and confirm that patterns and dash variations remain present. |
| Queue announcement length | Selecting a run announces its title, gate/current phase and queue position. Later updates announce changed facts; full phase details remain accessible in the workspace. The live region retains its identity while real updates replace its text child, including consecutive updates with identical spoken words. |

[Native verification receipts](holistic-ux-native-rereview-2026-10-17.json) record the response statuses, persisted actors and consumer revocations without credentials. Verification used an isolated local LangGraph runtime, the stub phase engine and two temporary persisted operator consumers with project scopes. Both consumers were revoked after the checks. It did not use the dashboard's development authentication bypass. The real-runtime probe caught and prevented an initially proposed fix that sent a reserved auth key through the credential guard; the final transport preserves that guard. Existing reviewer servers and their data were left running.

| Re-review verification | Result |
| --- | --- |
| Full backend suite | 5,941 passed, 23 skipped; one upstream Starlette deprecation warning. |
| Full dashboard suite | 1,107 passed across 135 files. |
| Shared event contracts | 48 passed. |
| Final production browser suite | 47 passed, including all six chart palettes, demo evidence navigation, connected viewer/operator context and queue announcements, and the existing role and workflow coverage. |
| Build, types and lint | Production build, TypeScript, Pyright and Ruff passed. Dashboard lint has no errors and 23 existing Fast Refresh export warnings. |
| Contrast | 750 text/surface pairs passed at 4.5:1 or above; 48 series/panel pairs passed at 3:1 or above. Eight checker regressions passed in the backend suite. |

Reviews found and corrected nullable gate-summary typing, identical live-region messages that produced no DOM mutation, and same-ID interrupt generations that needed canonical timestamp/payload tracking. Repeated unchanged snapshots remain silent. Browser verification also replaced a stale-current-text assertion with an observer of actual gate announcement mutations, allowing legitimate later transport updates. The final independent cross-cutting review and subsequent test review found no remaining issues in these changes.

The four code changes were committed after clear review rounds as `53b9d98`, `4e2c8d5`, `065bc30` and `129a292`. Main was fetched and integrated before the final production builds and backend suite. No new migration is required beyond the previously documented `0039` requirement. No APEX Load source was changed.

Cleanup removed and verified 39 Python/Vite/TypeScript cache paths (16.59 MiB), one disposable browser result file and the task's isolated native-runtime directory. Task-owned test servers and browser processes are closed; pre-existing reviewer servers remain running. Temporary operator consumers were revoked, while their administrative audit records remain in the local development database. Dependencies, production bundles and tracked captures were preserved. No Go or Docker builds were used.
