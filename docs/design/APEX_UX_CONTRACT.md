# APEX UX interaction contract — version 1

This compatibility baseline connects Orchestration to the current APEX Load dashboard. It reuses Orchestration's existing Load-derived token sheets and shell instead of introducing another visual theme. The implementation is local to each repository; it is not a separately published component package.

| Primitive | Implementation and contract |
| --- | --- |
| Shell / Page header | Existing Sidebar, Topbar and TopbarContributionProvider remain the page-heading authority. Routine Settings and APEX Load navigation stays outside Advanced. |
| Color / type / surfaces | Existing `theme/tokens.css`, `themes.css`, `apex-light.css` and `accessibility.css`; `ux.css` adds version 1 interaction sizes and aliases. Semantic acceptance colors remain independent of orchestration status. |
| Buttons / fields | Existing `.btn` and field primitives; 36px normal buttons, 32px small buttons, visible focus and muted disabled state. Field labels and explanatory text remain visible. |
| Tabs | `components/ux/TabList.tsx`: automatic activation, wrapping arrows, Home/End, one tab stop and linked named panels. Wizard retains its existing accessible step tabs and prerequisite guards. |
| Dialogs | Existing Modal, typed AbortConfirm, preflight and gate action components retain focus and confirmation behavior. Approval has one abort action. |
| Library controls | Search, status/project filters, active-filter indicator, comparison guidance, named overflow actions and Rows / Previous / Next. Filtering uses the server-supported contract; application/environment/date query expansion is not implemented. |
| Personal preferences | System or explicit theme, device/reduced motion, table rows and evidence-editor wrapping. Preferences are immediate and browser-origin scoped. No cross-origin credential or preference copying. |
| Results | Load completion, assessed acceptance, orchestration progress and evidence availability are separate facts. Missing assessment remains unknown. |

Domain exceptions: Orchestration retains phase review, discussion, selected plans, its Light (CTx) compatibility theme, and credential diagnostics. Load retains domain/project/application/release selection, workload controls, reports, and schedule authority. Specialized Orchestration tables retain their existing pagination limits; the personal row default applies to Test History. Notifications remain informational where a product does not support them.

## Handoff contract

The configured Load console URL must be an HTTP(S) execution URL with no embedded credentials or query payload. Orchestration adds `orchestrationReturn` and `orchestrationRun` as outer query parameters, retaining the exact Load run in the hash route. Load's existing route resolution determines its project/application/release from that run; local and remote project IDs are never assumed interchangeable.

Load displays an explicit return link only on the matching execution. It accepts HTTPS or loopback HTTP, the Orchestration `/runs/{thread}` or `/apex-load` routes, and only known context query keys. It rejects credentials, fragments and unrelated destinations, displays the destination host, and never automatically redirects. This is navigation context, not authorization or proof of a configured integration. Both applications enforce their own authentication and scope.

Mirrored-run analysis links reuse the stored pipeline thread identity. Refresh refetches the ledger; it does not launch a test, schedule, or second reporting pipeline. `launched` means an analysis pipeline was created, not that its analysis succeeded.

## Validation

Run the dashboard tests, shared contrast checker, typecheck/build and relevant backend adapter/reconciler contracts. When changing Load, run its required full dashboard `test:cycle`. Compare rendered preferences, libraries, approvals and run context against the saved audit, recording state/theme/density differences explicitly.
