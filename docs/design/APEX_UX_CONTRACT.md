# APEX UX interaction contract — version 2 (2026-10-17)

This compatibility baseline connects Orchestration to the current APEX Load dashboard. It retains Orchestration's shell and accessible interaction primitives while aligning the shared palette, typography, form surfaces, and sentence case with Load. The implementation is local to each repository; it is not a separately published component package.

| Primitive | Implementation and contract |
| --- | --- |
| Shell / Page header | Sidebar, Topbar and TopbarContributionProvider own the route title, matching Load's 19px / 850 page title. A route must not add a second h1 or repeat its title as a body heading; section headings describe content. Desktop starts with a 64px icon rail, honoring an explicitly stored preference. Expansion is a 240px overlay that does not reflow content; use the shared Dialog for focus containment, outside-click/Escape dismissal, and focus return to the rail opener. Empty rail space can expand it. Every destination retains its accessible name and the pending-review count remains visible and named on the rail. Preserve the mobile drawer. Routine Settings and APEX Load navigation stays outside Advanced. |
| Color / type / surfaces | `theme/tokens.css`, `themes.css`, `apex-light.css`, `typography.css`, `accessibility.css`, and `ux.css` define the shared system. Use semantic tokens, the type scale, `--label-case`, explicit form surfaces and theme color-scheme; accent text uses `--accent-text`. Semantic acceptance colors remain independent of orchestration status. |
| Buttons / fields | Use `.btn` and shared field primitives: 36px normal buttons, 32px small buttons, visible focus and muted disabled state. Use sentence case, preserving proper names and acronyms. New opens creation, Create persists a new object, Save persists edits, Apply changes the current configuration, Cancel/Close dismisses. Field labels and explanatory text remain visible. Sign out uses a secondary button. |
| Tabs | `components/ux/TabList.tsx`: automatic activation, wrapping arrows, Home/End, one tab stop and linked named panels. Wizard retains its existing accessible step tabs and prerequisite guards. |
| Dialogs | Use `Dialog` for focus containment, stacked inert handling, and focus restoration; overlays consume `--modal-backdrop`. Confirm phase skip, abort, prompt archive, and cycle lifecycle actions. Destructive resource deletion may require the resource name; abort deliberately uses the stable keyword ABORT. Cancel precedes the confirming action. Dirty forms register a navigation guard, including their own cancel controls; review drafts are isolated by authenticated session and interrupt identity. |
| Library controls | Reuse `TableToolbar`, `Pagination`, `SortableHeader`, and `OverflowMenu` for search, filters and their active state, supported sorting, comparison guidance, named row actions and Rows / Previous / Next. Sort only the loaded page unless the API supports global sorting, and say so. Compact phase strips are a single non-interactive graphic with a complete phase-state label; the row's run control opens the workspace. Keep per-phase controls in the full-size workspace. `CopyableId` keeps full identifiers in the DOM and provides copy success and failure feedback; visual truncation must not remove characters. Filtering uses the server-supported contract; application/environment/date query expansion is not implemented. |
| Personal preferences | System or explicit theme, device/reduced motion, table rows, evidence-editor wrapping and local/UTC timestamps. Preferences are immediate and browser-origin scoped. Absolute times use the shared formatter with the selected timezone; updates preserve mounted editors and drafts. No cross-origin credential or preference copying. |
| Results | Load completion, assessed acceptance, orchestration progress and evidence availability are separate facts. Missing or incomplete assessment remains unknown. Exclude uncollected execution metrics from comparison rankings and baselines; zero is a measured value, not an absence sentinel. Normalize phase state once from durable phase evidence and thread lifecycle. Approval history records the authenticated actor for approve, revise, and discuss. |

Domain exceptions: Orchestration retains phase review, discussion, selected plans, its Light (CTx) compatibility theme, and credential diagnostics. Load retains domain/project/application/release selection, workload controls, reports, and schedule authority. Specialized Orchestration tables retain their existing pagination limits; the personal row default applies to Tests. Notifications remain informational where a product does not support them.

The four run-calendar text-token differences are intentional: dark uses `--text-primary` for ad-hoc text and `--success-text` for cron text; solarized-dark uses `#f5a4ce` and `#87d8d0`, respectively. Orchestration has no routed run-calendar surface. Retain these higher-contrast values instead of copying lower-contrast Load values onto an unused surface.

## Shared fonts

All themes inherit Load's exact stacks; theme-specific font substitutions are not supported:

```css
--font-sans: 'ATT Aleck Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
--font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace;
```

The project owner approved bundling ATT Aleck Sans with Orchestration on 17 October 2026. `theme/fonts.css` self-hosts the same four normal faces as Load (400, 500, 700, 900), with `font-display: swap`. The files live under `public/fonts/att/`. No Inter, Nunito, or IBM Plex Mono assets ship. Mono uses the system stack. This records the approved distribution choice; it does not grant broader font rights.

## Names and state feedback

Use **Test** for the authored test, **Run** for an execution attempt, **Review** for a paused human workflow, and **Decision** for its recorded outcome. Pipeline/thread/gate remain API and implementation terms. Use **Tickets**, **Test presets**, **Knowledge & documents**, **Evidence**, and **Applications & environments** consistently across navigation, headings, and links. An application contains environments; source material informs a test and evidence records its execution. `/test-presets` is canonical; old `/golden-configs` bookmarks redirect with query and fragment preserved.

Use `Skeleton` for pending content and `StateCard` with an explicit kind for empty, filtered, loading, not-found, unauthorized, unconfigured, or stale states. Route failures use `ProblemCard`; form failures stay inline with `role="alert"`; stale usable results use `CachedDataWarning`. These shared components own their styles so cold routes render correctly. Do not use toasts. One run live region announces meaningful phase/review/transport changes; the review queue gives focus to its selected option. CodeMirror editing must allow Tab to leave without changing content.

Authorization is a capability, not merely a role label: scope controls launch and catalog mutations. Explain role refusal separately from missing scope, before a user authors an unlaunchable test. Global-only operations such as preset editing explain their narrower requirement.

## Handoff contract

The configured Load console URL must be an HTTP(S) execution URL with no embedded credentials or query payload. Orchestration adds `orchestrationReturn` and `orchestrationRun` as outer query parameters, retaining the exact Load run in the hash route. Load's existing route resolution determines its project/application/release from that run; local and remote project IDs are never assumed interchangeable.

Load displays an explicit return link only on the matching execution. It accepts HTTPS or loopback HTTP, the Orchestration `/runs/{thread}` or `/apex-load` routes, and only known context query keys. It rejects credentials, fragments and unrelated destinations, displays the destination host, and never automatically redirects. This is navigation context, not authorization or proof of a configured integration. Both applications enforce their own authentication and scope.

Mirrored-run analysis links reuse the stored pipeline thread identity. Refresh refetches the ledger; it does not launch a test, schedule, or second reporting pipeline. `launched` means an analysis pipeline was created, not that its analysis succeeded.

## Validation

Run the dashboard tests, shared contrast checker, typecheck/build and relevant backend adapter/reconciler contracts. When changing Load, run its required full dashboard `test:cycle`. Compare rendered preferences, libraries, approvals and run context against the saved audit, recording state/theme/density differences explicitly.
