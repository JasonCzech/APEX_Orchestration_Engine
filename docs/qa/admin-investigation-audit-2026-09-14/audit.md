# Admin and investigation UX audit

Keep administration and investigation distinct. Consolidate related context and duplicate records; split the APEX Load page by user task. The largest improvements are clearer workflow ownership, consistent registry/detail behavior, and reliable investigation handoffs.

## Recommended structure

**Testing → Load executions (APEX Load).** Move the operational entry out of Investigation. Default to one execution table with execution status, analysis status, and actions. Keep scenario authoring and schedules in the native Load application; if embedded controls are required, give them separate task views.

**Investigation → Agent Analytics / Logs.** Keep two specialist destinations. Share project, application, test/run and time context where each data source supports it; make cross-links preserve relevant filters. Do not imply unsupported filters were applied.

**Test detail → investigation views.** Reuse Logs, Agent activity/usage, Execution and Evidence views inside the selected test context. Preserve global destinations for cross-test exploration; do not duplicate their implementations.

**Admin → Connections / Access & API keys / System status.** Use a common administration shell and local navigation. Keep connection setup and access control as separate resources. The existing Admin sidebar group can remain; a single Admin entry with these subpages is optional, not necessary to deliver the fixes.

**Account → Preferences & session.** Keep personal appearance, browser API-key replacement, sign-out and current identity together. Keep system-wide status and connection administration out of personal settings.

**Resources → Applications & Environments.** Keep application/environment configuration here. Link to the connections those resources use; keep credential references and transport host mappings on connection detail.

## Prioritized findings

### F1 · High · Separate APEX Load authoring from investigation; combine execution and analysis records

**Evidence:** Steps 6–7; screenshots 12–14.

The page begins with connection readiness and three technical disclosures, followed by scenario JSON, scheduling JSON and manual approval-ID entry. Execution results are farther down. The same Load runs appear as cards and again in a separate analysis-handoff table, using different names and statuses.

**Recommendation:** Make Load executions the default operational view. Use one row per execution: name, project/application, execution status, analysis status, started/duration, and contextual actions. Open a selected execution in a detail panel with metrics and evidence. Move provisioning, credentials and mapping to Connections. Link to native Load authoring/scheduling; retain separate embedded authoring views only if these are required Orchestration tasks.

**Boundary / limit:** The native application already owns execution and schedules. Linking there preserves that ownership but entails a cross-application handoff; preserve a return link and selected execution. Treat aggregated schedule approvals as a later integration, preserving provider authority and permissions rather than pretending they are identical to pipeline gates.

### F2 · High · Carry investigation context across page boundaries

**Evidence:** Steps 5, 7–8; screenshots 15–17.

Analytics can group by test and open Run Detail. The run has a Logs link, but its destination contains only ?thread=run-engine-recovery. Logs shows a technical ID, no friendly test name or return action, and no selected time preset. The demo handoff returned an empty state.

**Recommendation:** Keep Analytics and Logs separate, with a shared context strip. From a run, initialize its actual start/end window, project/application labels, and source identifiers; show the selected test name with a return link. Offer contextual View logs and View agent activity actions. Distinguish orchestration/agent events from application workload logs.

**Boundary / limit:** The empty demo result is not proof of a production search defect. The missing run window and missing visible context are confirmed. A native Load run ID and an Orchestration thread ID are different identifiers; resolve their relationship rather than copying one into every filter.

### F3 · High · Unify registry and detail behavior across administration

**Evidence:** Steps 1–3; screenshots 01–05.

Connections has a separate card grid for each kind. With one connection in most groups, it creates a tall single-card column with large unused space. Consumers is compact, but names are plain text; opening requires the overflow menu. Consumer detail explicitly sends the user back to list actions to edit, rotate a key or delete.

**Recommendation:** Make both registries searchable tables with name links and consistent filters. Connection columns: name, kind/provider, scope, enabled state, and last test result if available. Add details/edit actions on consumer detail, with row actions as shortcuts. Keep Connections and Access as separate subpages.

**Boundary / limit:** Do not turn every action into a global toolbar button. Keep routine actions visible, put destructive actions in a secondary menu or danger section, and preserve existing permission checks, key reveal safeguards and confirmations. Enabled must remain distinct from verified connectivity.

### F4 · Medium · Refocus System and clarify the boundary with personal Settings

**Evidence:** Step 4; screenshots 06–07.

System gives equal card prominence to system metadata, your identity, a feature flag, connectivity and quick links. Settings also contains identity/about and connection/session information. “System,” “Connections” and “Connection & session” have overlapping-sounding labels.

**Recommendation:** Rename System to System status and prioritize health, last check and actionable connection problems; place capabilities and version information below. Move current identity to Account/session. Keep browser connection diagnostics there, explicitly labeled This browser. Use one source for shared version/identity values. Replace the runbook repository-path instruction with an actionable authorized link when available.

**Boundary / limit:** Do not merge access management with current-user API-key replacement. Admin scope and personal scope remain different. Read-only capabilities should look informational, not like editable feature toggles.

### F5 · Medium · Reduce analytics filter density and make data scope explicit

**Evidence:** Step 5; screenshots 08, 15 and 15a.

Two large toolbars precede results. Models, stages and agents are always expanded. Several labels are truncated in the actual button text and accessibility names. Demo model filters list older identifiers than the plotted series. No default time preset is visibly active. Sidebar says Agent Analytics; the heading says Analytics.

**Recommendation:** Use Agent Analytics consistently. Put scope/time first, KPI summary next, primary chart and breakdown after it. Keep grouping and measure next to the chart; collapse secondary filters behind Filters with a selected-count summary. Populate model/agent options from available data, support search and preserve full accessible names. Show the effective time range and explain unavailable Cost data.

**Boundary / limit:** Default analytics is a seven-day window in source, while Logs defaults to a shorter backend window. Shared controls may retain different sensible defaults, but must make them explicit. Demo mismatch proves the filter-design weakness; it does not establish which model names production uses.

### F6 · Medium · Apply the established design system to APEX Load and administration details

**Evidence:** Steps 2–7; screenshots 02–03, 07, 12–15.

Connection forms pack unrelated fields and the JSON-mode button into the same automatic grid. APEX Load mixes native-looking select/textareas, raw JSON, plain blue links, unlabeled-ID entry, status text and an unstyled handoff table with the app’s styled cards and buttons. Settings uses a more spacious layout than administration.

**Recommendation:** Use shared field, table, status, tabs and action components. Group connection forms into Basics, Scope, Provider configuration and Advanced. Keep Edit as JSON within Advanced. Show readable metrics with units, and keep raw JSON in a technical-details disclosure. Replace approval-ID and error-signature typing with selection from actual pending approvals and error groups.

**Boundary / limit:** Retain diagnostic JSON for expert troubleshooting. The goal is progressive disclosure and readable defaults. APEX Load’s .adm-table has no matching styling rule in the inspected CSS, corroborating the rendered table difference.

### F7 · Medium · Make route entry and local navigation predictable

**Evidence:** Steps 5 and 8; screenshots 15a and 16a.

Navigating from scrolled APEX Load into Analytics left the page partway down the charts; opening a test from Analytics also landed below its main heading/context. Run Detail exposes Timeline and Activity links to the same timeline route.

**Recommendation:** Reset the main scroll container for a new destination; restore prior position only for deliberate Back/Forward navigation. Preserve focus handling and ensure focused content is not covered by the sticky header. Choose Activity or Timeline once and use one consistent local navigation location.

**Boundary / limit:** The route-scroll behavior was observed in the current demo session and is corroborated by the shell focusing main without resetting its scroll position. This is separate from exact viewport sizing, which was affected by browser zoom.

### F8 · Medium · Improve labels and accessible control affordances

**Evidence:** Steps 1–7; screenshots 01–15.

Enabled pills on Connections are interactive, while similar pills elsewhere are read-only. Metadata and helper text are small. Search filters use placeholders as their visible labels. Empty consumer scopes render as a dash, which gives no explanation of access coverage.

**Recommendation:** Use a clear switch for editable enabled state and a badge for display-only status. Add persistent field labels; show full model/agent names on focus as well as hover. Explain scope semantics with explicit copy based on authorization rules. Standardize control target area, focus treatment, error placement, and readable text sizes.

**Boundary / limit:** Small text and pale borders are visual risks, not measured contrast failures. Full screen-reader, keyboard, zoom and color-theme testing is still required. Do not infer access semantics solely from an empty scopes array.

## Shared page layout

**Page heading.** One title matching navigation, optional one-sentence purpose, and one primary task action. On detail pages, put the resource name in the primary hierarchy; use a compact breadcrumb for its collection.

**Scope row.** Show project/application/test or connection, effective time range and freshness together. Make scope persistent while switching related investigation views; clearly label global versus selected-test modes.

**Local navigation.** Use the same tab/link component, spacing and selected state. Route-backed tabs are appropriate for durable detail views; keep unrelated admin resources as separate subpages.

**Content hierarchy.** Registry: filters → table → pagination. Analytics: scope → summary → main chart → breakdown; secondary charts/filters disclosed. Detail: summary → focused task sections → advanced diagnostics.

**Form grid.** Use a stable one- or two-column form, persistent labels and associated help. Put dependent fields together. Use a predictable footer for Save/Cancel and a visible unsaved state. Keep actions out of automatic field grids.

**Tables.** Use the existing shared table treatment, consistent row density, clickable names, status badges and overflow actions. Align numerical columns; show friendly names before technical IDs. At narrow widths, prioritize columns or provide a contained horizontal-scroll region.

**Typography and spacing.** Use existing theme tokens and shared primitives. Suggested targets: 24px page heading, 16–18px section heading, 14px body/controls, and 12px supporting metadata. Prefer a consistent 8/16/24px spacing rhythm and 36–40px controls; validate against current density preferences.

**Feedback.** Keep initial, loading, no-results, unconfigured, unavailable and stale-data states distinct. Preserve the user’s query on errors; show Retry, Clear filters or Manage connection only where relevant and permitted.

**Responsive behavior.** Collapse global navigation, stack form fields, wrap or disclose secondary filters, and keep important actions visible. Test reflow at representative compact widths and zoom without relying on screenshot scaling alone.

## Captured review steps

### Step 1: Connections registry — Needs improvement

Connected development data. Names and enablement are discoverable, but grouping by kind creates excessive vertical scanning. Enabled status is not connection health.

![01 connections](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/01-connections.png)

![01a connections compact](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/01a-connections-compact.png)

### Step 2: Connection detail and Host mappings — Mixed

The resource has breadcrumbs and task tabs, a useful structure to reuse. Form layout and action prominence need work. Host mappings belong with the connection, not in global investigation. No changes, probes, saves or deletions were submitted.

![02 connection detail](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/02-connection-detail.png)

![03 host mappings](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/03-host-mappings.png)

### Step 3: Consumers list and detail — Needs improvement

The table supports comparison, but names are not links and the detail view is an action dead end. Opened the overflow menu and read-only detail only; key lifecycle operations were not exercised.

![04 consumers](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/04-consumers.png)

![05 consumer detail](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/05-consumer-detail.png)

### Step 4: System and Settings boundary — Mixed

System metadata, identity and connectivity overlap with session information. Settings has a useful distinction between Personal preferences and Connection & session; personal and system-wide concerns should remain separate.

![06 system](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/06-system.png)

![07 settings session](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/07-settings-session.png)

### Step 5: Agent Analytics, empty and populated — Mixed

Empty-state recovery guidance, KPIs, charts and test drill-down are useful. Filter density, truncated names, static model choices and hidden effective range reduce clarity. Screenshot 15a records the unexpected scroll position after navigation; 15 is the same page at its top.

![08 analytics empty](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/08-analytics-empty.png)

![15a analytics navigation scroll](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/15a-analytics-navigation-scroll.png)

![15 analytics demo](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/15-analytics-demo.png)

### Step 6: Logs entry and results — Mostly clear, context needs work

The explicit Search action, result table and readable severity labels support the task. Visible labels, effective time range, connection provenance and return context need improvement. Connected results came from a stub provider and are not evidence of real operational data quality.

![09 logs start](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/09-logs-start.png)

![10 logs results](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/10-logs-results.png)

### Step 7: APEX Load, unconfigured and populated — Major restructuring recommended

The connected empty state explains the setup dependency and offers Manage connections. The isolated offline demo reveals authoring, schedules, approvals, executions, handoff and technical diagnostics on one long page. Clicked Live metrics and expanded its read-only result. No external Load link, create action or approval was executed.

![11 apex load unconfigured](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/11-apex-load-unconfigured.png)

![12 apex load demo top](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/12-apex-load-demo-top.png)

![13 apex load demo operations](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/13-apex-load-demo-operations.png)

![14 apex load demo metrics](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/14-apex-load-demo-metrics.png)

### Step 8: Analytics → Run Detail → Logs — Needs improvement

Grouping analytics by Test and opening the recovery run succeeded. The run exposes useful investigation links but duplicates Timeline/Activity and loses its friendly context on the Logs landing. Only the thread ID was carried. Screenshot 16a shows route-entry scroll; 16 is the corrected top view.

![16a run navigation scroll](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/16a-run-navigation-scroll.png)

![16 run investigation](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/16-run-investigation.png)

![17 run logs handoff](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/docs/qa/admin-investigation-audit-2026-09-14/17-run-logs-handoff.png)

## Delivery order

**First:** Fix consumer detail actions and name links; make Connections a searchable registry; unify titles, scope/time labels and route scroll behavior.
Acceptance: A user can open a resource and finish its normal management task without returning to the list; a new destination opens at its start; Back restores prior context.

**Next:** Restructure APEX Load into execution/analysis viewing plus clearly separate authoring/scheduling entry points; reuse shared tables and form controls.
Acceptance: One execution row shows both execution and analysis state; selected metrics/evidence are directly adjacent; no approval or error investigation requires manually copied IDs.

**Then:** Add shared investigation context, contextual drill-throughs and any needed approval-inbox integration; refocus System status.
Acceptance: A run opens Logs with its friendly context and relevant window; return navigation preserves prior filters; authorization remains unchanged; provider-specific approvals remain correctly identified.

## Evidence limits

Reviewed the current local working tree and captured fresh UI evidence on September 14, 2026. Existing uncommitted changes were included in the evaluated version and left untouched. The connected preview at port 3000 used Dev Admin, development data and stub integrations. Port 3016 was a temporary isolated preview of the same source using bundled offline demo data. It was used because live analytics lacked recent events and live APEX Load had no configured connection. Demo captures are visibly labeled. Screenshot capture inherited browser zoom/scaling; compact navigation was observed, but exact responsive geometry and minimum target sizes are not certified. The additional compact Logs capture was excluded from the report because its scaled framing was unsuitable for layout measurement. Other themes, screen-reader operation, complete keyboard traversal, production roles, destructive mutations, real provider failures, and real scheduling/approval execution were not tested. This is a design audit, not a full accessibility or backend correctness certification.

## Source corroboration

- [features/admin/ConnectionsPage.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/admin/ConnectionsPage.tsx): Registry grouping and connection cards.
- [features/admin/ConsumersPage.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/admin/ConsumersPage.tsx): Plain-text names and list-only edit/key actions.
- [features/admin/ConsumerDetailPage.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/admin/ConsumerDetailPage.tsx): Detail-page instruction to return to list actions.
- [features/admin/AdminSystemPage.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/admin/AdminSystemPage.tsx): System, identity, flags and connectivity cards.
- [features/apex-load/ApexLoadWorkspacePage.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/apex-load/ApexLoadWorkspacePage.tsx): Combined authoring, schedules, execution and handoff UI.
- [features/analytics/AnalyticsPage.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/analytics/AnalyticsPage.tsx): Static model options and truncated button text.
- [features/analytics/analyticsFilters.ts](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/analytics/analyticsFilters.ts): Seven-day effective analytics default.
- [features/logs/logsFilters.ts](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/logs/logsFilters.ts): Shorter backend log-window default.
- [features/runs/RunDetailPage.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/runs/RunDetailPage.tsx): Thread-only Logs URL and duplicated timeline navigation.
- [components/layout/AppShell.tsx](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/components/layout/AppShell.tsx): Focus-on-route-change without scroll reset.
- [features/admin/admin.css](/Users/jasonczech/Documents/GitHub/APEX_Orchestration_Engine/apps/dashboard/src/features/admin/admin.css): Admin form, control and table styling.
