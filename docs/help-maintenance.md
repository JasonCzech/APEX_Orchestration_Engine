# Dashboard documentation maintenance

The canonical customer-facing articles are `docs/help/*.md`. The typed allowlist
is `apps/dashboard/src/features/help/catalog.ts`; it supplies stable IDs, titles,
areas, audiences, search keywords, ownership and review baseline. Article IDs map
to `/help/{id}` and matching Markdown filenames. Admin topics are presented to
admin sessions; these bundled educational documents contain no deployment secrets.

The implementation follows APEX Load's five-area Help workspace while retaining
Orchestration's browser-history routing, React Markdown renderer, theme tokens,
navigation guards, and existing authentication. Articles load as separate Vite
chunks. Search loads allowed articles on demand and searches complete text and
keywords locally. No documentation API or external search service is required.

## Updating behavior

1. Verify current UI labels, permissions and runtime contracts in code. Historical
   implementation notes can be superseded by later continuations; do not promote
   an old backlog or test claim into a current product promise.
2. Edit the matching article in the same change. Include prerequisites, steps,
   expected outcomes, failure/recovery paths and deployment boundaries.
3. Keep existing article and section IDs stable. Update references intentionally
   when a heading changes; the integrity check catches broken authored anchors.
4. Add new topics to the typed catalog and map new routes/steps in
   `contextualHelp.ts`. Keep broadly useful role concepts in public operator help.
5. Review the content baseline/date in the catalog and `support.md` when conducting
   a complete review. Preserve the difference between a review date and the
   separately connected backend's runtime version.

Markdown supports ordinary headings, lists, emphasis, code, links, and local
screenshots. Use H2/H3 headings without multiline inline formatting for the outline.
Use lists instead of pipe tables, which are not part of this renderer's dialect.
Raw HTML is disabled. Article-relative links such as `phases.md#complete-human-work`
resolve to SPA URLs; app links use root paths. External links use HTTPS without
credentials. Screenshots belong under `apps/dashboard/public/help/images/` and use
absolute `/help/images/...` source paths. Do not embed connected customer data.

## API reference

`ApiReference.tsx` lazy-loads `docs/api/apex-v1.openapi.json`, the same generated
contract used for domain client types. It displays operations, requests, responses,
and component schemas and offers the raw contract download. It sends no API
requests. Authentication prose supplements authorization policy not encoded in
OpenAPI. Native LangGraph endpoints remain documented by their SDK/runtime.

Do not hand-edit the generated contract for documentation changes. Use the normal
OpenAPI export/type generation workflow when API behavior changes.

## Checks

Run `npm run check:docs` for catalog membership, article/source parity, heading
IDs, internal links, screenshots, search coverage, and contextual destination
integrity. These tests also run in the normal dashboard Vitest suite in CI.

For Help UI changes, run dashboard typecheck/lint/tests/build and the production
demo Playwright help scenario. Verify desktop/mobile, deep-link reloads,
Back/Forward, keyboard focus, zoom, printing and all five shipped themes. Check
both operator/viewer and administrator navigation in component tests.

For repeatable browser checks, build the dashboard, start
`APEX_DASHBOARD_TEST_PORT=4187 node apps/dashboard/e2e/demo-server.mjs`, open
`http://127.0.0.1:4187/help` in an isolated Playwright CLI session, and run
`playwright-cli -s=YOUR_SESSION run-code --filename scripts/qa_help_workspace.js`
from the repository root. The script uses the open tab's origin. It verifies
search/history, repeated anchor navigation, resize/focus preservation, the shell
skip target, invalid wizard bookmarks, operation/schema lookup and the
OpenAPI download, screenshot loading, five theme text/link/button contrast,
print styles and their isolation from other routes, stale API groups,
768px/390px layouts, mobile navigation, and 200% CSS zoom. It writes
screenshots to `output/playwright/`. This is a production demo check, independent
of connected-provider acceptance.

Finish the build before starting/restarting the demo server: a rebuild clears
the `dist/` files that it serves. Run the full dashboard suite without overlapping
the production build; reduce concurrency (for example, `--maxWorkers=1`) when
local resource pressure causes test timeouts.

The content baseline was assembled from current routes/features, prompt catalog,
multi-system scope, phase ownership/analysis contracts, lifecycle continuations,
and deployment runbooks. Behavior in these sources remains authoritative over
historical QA notes. Documentation validation establishes consistency and UI
behavior; it is not live external-provider acceptance evidence.
