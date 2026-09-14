# New Test & Pipeline usability audit
September 14, 2026 · Connected local dashboard · Product Design review

**Verdict: the experience is substantially implemented, but the workflow is fragmented.** Setup is organized around six configuration categories; execution is organized around seven phases and several diagnostic views. Users must reconstruct how the categories feed the phases, where they are, and what action is needed next.

This audit covers New Test, the Home/Pipeline Operation Dashboard, run overview, phase review, Pipeline Log, and rerun preflight. “Pipeline” is treated as both the dashboard and the execution workspace because the product uses the term in both places.

## Implementation update

The highest-impact findings from this audit were implemented after capture: configuration now precedes optional evidence collection; phase inclusion and prompt selection are separate controls; external execution cannot launch without an application and target environment; the main re-run action repeats the prior plan; and launch/re-run return to the run overview. The dashboard is now titled **Test Operations Dashboard**. The findings below preserve the state observed during the audit.

## Highest-impact findings

| Priority | Finding | Evidence | Recommended change |
|---|---|---|---|
| P1 | Phase selection also changes prompt focus. A first click on selected Test Planning left it selected and changed the Prompts screen from Story Analysis to Test Planning. | Steps 4–5; interaction reproduced; ConfigStep source. | Make phase toggles only include/exclude phases. Put a separate phase selector directly in Prompts. |
| P1 | “All required sections look ready” means form validity, not verified execution readiness. APEX Load + all seven phases + no environment reached an enabled Launch Test. The connection control offered project default without showing which connection would resolve. | Steps 4, 6; screenshot 12; stepIssues source. | Distinguish “Ready to begin planning” from “Ready to execute.” Show target, resolved engine connection, required release mapping, evidence prerequisites, and checks deferred until later phases. |
| P1 | Rerun expands scope by default. The inspected run selected only Story Analysis, but its primary Re-run action selected all seven phases. | Steps 8, 10; screenshots 08, 13; RunDetailPage source. | Default to repeat the previous plan. Make “Run all phases” an explicit alternate action with the scope change summarized. |
| P1 | Setup-to-run handoff drops the launch overview. The wizard routes successful launches to ?tab=log; run detail redirects that into a phase workspace. | Source-supported transition; workspace inspected in steps 8–9. | Land on a persistent test workspace with launch confirmation, selected plan, current phase, next review, and the main next action. Keep live logs secondary. |
| P2 | Naming and navigation require too much translation: New Test → Run Scope → pipeline; Home → Pipeline Operation Dashboard; Run Detail → Phase Detail; Timeline and Activity share a destination, while Logs and Pipeline Log are separate. | Steps 1, 7–9. | Use Test for the user’s work, Run for an execution attempt, and Phase for a step. Use one Activity destination with readable events and expandable diagnostics. |
| P2 | Readiness feedback does not identify the section needing attention until Review. Empty title/request could be skipped through every step while the same “2 items” footer remained. | Steps 1–6. | Keep free navigation, but show section status and linked, specific issues as the user goes. Mark optional sections in navigation. |
| P2 | Presets are late and can invalidate earlier work. Golden config is in step 4, after scope/evidence; applying one can change project/application and clear documents, work items, context summaries, and prompt overrides. | Step 4 placement; source inspection only for preset application, since presets failed to load. | Choose a template before evidence collection, or preview exactly what applying it will replace. |
| P2 | The default path asks for six screens and 14 planned review pauses before explaining the overall lifecycle. Prompt editing appears as a normal mandatory-looking step. | Steps 4–6. | Offer purpose-based starts: analyze requirements, prepare a test, or run a full test. Summarize reviews and expose prompt overrides as advanced controls. |
| P2 | Execution configuration does not summarize the actual workload contract: scenarios, traffic/load profile, duration, acceptance thresholds, and hard-stop criteria. These may be produced by agents later, but that deferral is not visible. | Steps 4–6; Config/Review source. | Show what is known, what the agent will propose, and what must be approved before execution. Avoid calling a planning request an execution-ready test. |
| P2 | Narrow layouts make orientation difficult. The selected Review tab can be outside the horizontally scrolled tab strip; the footer wraps heavily; the sticky page header overlaps the run heading in captured narrow views. | Screenshots 06, 08–12. | Scroll the active step into view, simplify mobile actions, and verify header/content offsets plus focus visibility. |

P1 = fix before treating the flow as dependable for routine operator use. P2 = meaningful friction or missing clarity.

## Functional completeness

| Capability | Assessment |
|---|---|
| Create and resume drafts | Present. Autosave was exercised successfully; the resume picker is visible. Existing-draft recovery and concurrent editing were not exercised. |
| Project/application/environment scope | Present, but project entry requires an exact ID, and execution readiness does not require or explain missing environment selection. |
| Attach work items and documents | Present. Optionality is explained. This session had a stale saved query and duplicate document names; external work item resolution and document parsing were not exercised. |
| Engine and release mapping | Partially surfaced. Engine selection and an APEX Load connection/release picker exist. Resolved defaults and readiness are not integrated into final validation. No live engine was invoked. |
| Phase plan and review policy | Present. Dependency checks and 14-pause summary exist. Phase control semantics and rerun defaults need correction. |
| Prompt inspection/overrides | Present for the focused phase and selected application. Cross-phase discovery is incomplete because focus is controlled from Config. |
| Review and launch | Form summary, issue links, pending-operation protection, launch-error handling, and durable idempotency are implemented. UI preflight is not an operational readiness assessment. Launch transport was inspected in code, not submitted. |
| Monitor and approve | Overview, phase progress, review controls, activity/logs, reasoning, and abort controls exist. Pending review was inspected; no approval, abort, or agent message was submitted. |
| Repeat a run | Present through rerun preflight. It operates on the existing thread and exposes phase/gate choices; it does not provide a clear “copy and edit this test” path in the inspected header. |
| Results and acceptance | Run overview distinguishes orchestration status from test acceptance—retain this. The inspected one-phase run correctly says execution acceptance is not applicable. Real completed execution evidence was not available in the inspected flow. |

## Recommended coherent flow

**Test brief → Evidence → Plan & readiness → Start → Same test workspace**

Keep the same title, application/environment, engine, and selected phase plan visible throughout. Treat optional tickets and documents as a single Evidence step. Present phase coverage, review policy, expected outputs, and deferred checks together. Put prompt overrides under Advanced, with independent navigation across all selected phases.

After start, keep the user in the same workspace and change its state: Preparing → Running → Needs review → Completed/Stopped. Lead with what happened, what happens next, and one primary action. Preserve the distinction between orchestration completion and test acceptance.

This can build on existing ScopeContext, phase strip, ReviewCard, RunOverview, and shared gate components; it does not need a new visual system.

## Captured walkthrough

### 1. Scope — needs improvement
Required title/request fields, application/environment controls, and a persistent scope bar are useful. “New Test,” “Run Scope,” and “pipeline” conflict. Project selection exposes IDs. The draft picker and multiple framing panels push application/environment below the initial desktop viewport.

![Scope](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/01-scope.png)

### 2. Work Items — usable, integration-dependent
Optionality is clear; description search, saved query, and direct key entry exist. The saved query in this environment needs a connection update and sends the user to another page. The footer’s two issues refer to Scope without saying so.

![Work Items](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/02-work-items.png)

### 3. Context — usable, selection needs more context
Upload affordance and accepted types are clear. Four existing documents have the same name, size, and type in this dataset; selecting the correct evidence is difficult without preview or distinguishing metadata. Actual upload/parse behavior was not tested.

![Context](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/03-context.png)

### 4. Config — significant interaction and readiness gaps
Engine descriptions and phase coverage are visible. Golden configs failed to load, with a retry and fallback explanation. Default review policy produces 14 planned pauses. Selecting an already included, non-focused phase changes prompt focus without excluding it.

Reproduction: fresh all-phase draft → click Test Planning once → it remains selected → open Prompts → Test Planning prompt appears. This also triggered autosave, confirming the click changed configuration state.

![Config](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/04-config.png)

### 5. Prompts — incomplete navigation
Catalog version and run override controls are useful. Only one phase prompt is shown, with no local control for switching phases. The user must understand Config’s hidden focus behavior to reach the other prompts.

![Prompts](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/05-prompts.png)

### 6. Review & readiness — useful summary, overconfident readiness
Issue links and Edit controls allow correction. However, a title/request and engine choice can yield an enabled Launch Test while operational prerequisites remain unspecified or unresolved. The screenshot below shows the initial empty-form validation; the second shows the later APEX Load audit draft marked ready.

![Review validation](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/06-review.png)

![APEX Load launch readiness](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/12-execution-readiness.png)

### 7. Pipeline dashboard — useful overview, weak connection to setup
Attention queue and recent runs provide entry points. Metrics mix recent accessible runs, a seven-day health window, and a duration explicitly described as creation-to-update rather than execution duration. Qualifications are honest, but operators must interpret several different populations. The narrow attention queue hides the run title visually in this capture.

![Pipeline dashboard](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/07-pipeline-dashboard.png)

### 8. Run overview — comparatively strong
“Review pending decision” is a clear next action. The distinction between selected-plan completion and test acceptance is correct and useful. Context is less complete than setup: the shared scope bar here omits environment, and the overview emphasizes several missing mapping fields. Existing seed records account for many “Not recorded” values; they are not proof that new launches lose data.

![Run overview](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/08-run-overview.png)

### 9. Phase review & monitoring — functional, over-fragmented
Gate copy explains the effect of approval and revision. Review actions exist in the workspace. Users still traverse several navigation layers: run sections, phase navigation, phase tabs, and prompt configuration. The paused run’s Pipeline Log has no live activity, despite being a central view; the durable result is elsewhere. Repeated explanatory text pushes evidence and actions down on narrow screens.

![Phase review orientation](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/09-phase-review.png)

![Review controls](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/10-review-actions.png)

![Pipeline Log empty state](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/11-pipeline-log.png)

### 10. Rerun — surprising default scope
The existing run has one selected phase; Re-run opens all seven selected. Dependency explanations are helpful, but “Inherit defaults / All gated / All auto” also differs from the wizard’s “Review every phase / Run automatically / Custom.” No rerun was started.

![Rerun preflight](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/new-test-pipeline-audit-2026-09-14/screenshots/13-rerun.png)

## Accessibility observations

Confirmed positives: skip link, named wizard tab panels, labelled form controls, accessible review edit labels, status announcements for draft saving, and keyboard handlers for wizard tabs/engine choice exist. Rerun opening moved focus into its modal and Cancel returned focus to its trigger.

Risks: phase controls combine selection and focus; narrow-screen tab overflow can hide the active step; compact text and muted controls need contrast measurement; sticky header/footer behavior needs keyboard-focus visibility testing. Prompt read-only editors appear as text entry areas in the accessibility tree and should be checked for clear names and reading experience.

These are observed risks and targeted implementation checks, not a WCAG conformance result. A screen-reader pass, contrast measurements, zoom testing, and full keyboard traversal remain outstanding.

## Implementation anchors

- Phase toggle/prompt focus coupling: [ConfigStep.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/new-test/steps/ConfigStep.tsx:143) and [PromptsStep.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/new-test/steps/PromptsStep.tsx:214).
- Form validation only: [wizardState.ts](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/new-test/wizardState.ts:350); optimistic readiness wording: [NewRunWizard.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/new-test/NewRunWizard.tsx:613).
- Launch-to-log handoff: [NewRunWizard.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/new-test/NewRunWizard.tsx:402) and redirect in [RunDetailPage.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/runs/RunDetailPage.tsx:103).
- Rerun defaults to all phases: [RunDetailPage.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/runs/RunDetailPage.tsx:163).
- Preset replacement side effects: [ConfigStep.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/new-test/steps/ConfigStep.tsx:70).
- Mapping status is local to the picker: [ReleaseMappingPicker.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/new-test/ReleaseMappingPicker.tsx:56).
- Review summary: [ReviewStep.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/new-test/steps/ReviewStep.tsx:89).

## Scope, limits, and changes made

Captured the connected local app at http://127.0.0.1:3000 with Dummy data off and the Dev Admin role. All screenshots are from this audit. The available browser surface changed from 1422×800 to 392×898 during inspection; wide and narrow captures are identified by their visible layout. No old screenshots were reused.

The backend contained older seeded/stub runs. Golden config loading failed, and no explicit eligible Load connection appeared for the selected project. These are environment observations, not proof of universal integration failure.

No real test, rerun, approval, agent message, abort, file upload, or external work item mutation was submitted. No application source was changed. Interaction testing created one autosaved draft, titled **UX audit — not for execution**, ID **f6a76623b9eb4185a0e02e15f51af1a3**. It is intentionally unlaunched.

Recommended next action: implement the phase-control fix, previous-plan rerun default, and explicit planning/execution readiness model, then consolidate setup and execution around one persistent test workspace.
