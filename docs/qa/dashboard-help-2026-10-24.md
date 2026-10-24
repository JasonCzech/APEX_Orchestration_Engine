# Dashboard Help delivery and verification — 2026-10-24

## Delivered

- `/help` and `/help/:articleId`, accessible from desktop/mobile navigation.
- Five documentation areas with 24 canonical articles, approximately 12,700
  words, and two screenshots captured from a reproducible offline demo.
- Full-text/keyword search, role-appropriate administrative presentation,
  per-article navigation, heading outlines, section deep links, copy controls,
  printable content, chunk-load retry, and partial-search recovery.
- Contextual help for every major route and wizard step, plus readiness and
  provider-recovery links.
- Lazy bundled domain OpenAPI operation/schema browser and raw contract download.
- `npm run check:docs`, maintenance instructions, and repeatable Playwright CLI
  checks in `scripts/qa_help_workspace.js`.

Reference review used the local APEX Load/Project_Stormrunner Help workspace,
article catalog, contextual mappings, Markdown article renderer, and help guides.
Orchestration retains its own browser-history routing, role/scope model, prompt
manifest contract, seven phase owners, multi-system scope, and theme primitives.

## Automated checks

- Complete dashboard Vitest suite: **158 files, 1,402 tests passed**.
- Final Help/integrity regression run after the anchor fix: **16 tests passed**.
- Shell focus/compact-navigation and Help checks also passed together before
  the final added anchor regression (**19 tests**).
- Production build including TypeScript: passed after final UI changes.
- Dashboard lint: no errors; **25 existing Fast Refresh warnings** in unrelated
  modules. Vite retained its existing vendor-size/Zod annotation warnings.
- `git diff --check`: passed.

Integrity coverage checks exact catalog/source membership, unique heading IDs,
every authored article/app link and fragment, both screenshot assets, all major
route/step help destinations, complete search indexing and valid result anchors.
UI tests cover viewer/operator/admin presentation, search/history, missing
articles/sections, API/schema browsing, failed chunks, partial search retry, and
sticky-header inset calculation.

## Browser verification

The production bundle was served by the repository's offline demo server in an
isolated browser session. The saved CLI check script passed:

- Search → result → Back restores the query; Clear search restores the article.
- Section links move keyboard focus below the sticky header; reload preserves
  the section and focus. Browser measurement found the original height-only
  offset insufficient, so it now includes the header's scroll-container inset.
- Generated operation search, schema lookup, and successful OpenAPI download.
- Walkthrough screenshot loading.
- All five theme renderings; sampled prose, link, and copy-button contrast is
  at least 4.5:1 against the article surface. This is sampled contrast coverage,
  not a full accessibility audit or screen-reader certification.
- Desktop 1440px, 768px and mobile 390px without page/content horizontal
  overflow; mobile navigation opens and dismisses with Escape.
- Compact Help title/demo badge stays readable without clipping.
- Print media hides shell/navigation/copy controls, uses dark text on a light
  surface, and allows the document to flow beyond the viewport.
- 200% CSS zoom without main-content horizontal overflow. This checks CSS zoom;
  it is not a claim of testing every browser's native zoom implementation.
- No unhandled page errors during the final scripted checks.

An additional UI walkthrough loaded the prepared ACC draft, selected Simulated,
removed the retained incompatible Internal RCA choice, launched, reviewed prompts,
requested an environment revision, verified the revised evidence, accepted
execution/reporting evidence, and reached completed Postmortem. Its intentionally
failed synthetic threshold remains distinct from lifecycle completion. This
exercise makes no claim of live provider or model acceptance.

## Evidence

- [Desktop](../../output/playwright/help-desktop.png)
- [Mobile](../../output/playwright/help-390.png)
- [768px layout](../../output/playwright/help-768.png)
- [Dark theme](../../output/playwright/help-theme-dark.png)
- [Light theme](../../output/playwright/help-theme-light.png)
- [Solarized dark](../../output/playwright/help-theme-solarized-dark.png)
- [Solarized light](../../output/playwright/help-theme-solarized-light.png)
- [Monokai dimmed](../../output/playwright/help-theme-monokai-dimmed.png)
- [200% CSS zoom](../../output/playwright/help-zoom.png)
- The two walkthrough images are shipped under
  `apps/dashboard/public/help/images/` and referenced from the canonical guides.

## Post-implementation code review

Reviewed the new feature, shell integration, content/contract behavior, tests,
and QA tooling. Fixed the following findings:

1. **P2 — Unrecognized wizard steps could crash the shell.** Plain-object lookup
   accepted inherited properties such as `constructor`, `toString`, and
   `__proto__`, then tried to destructure them as help targets. The lookup now
   requires an own property and falls back to Scope. Trailing-slash wizard URLs
   use the same normalization as the router. Regression inputs failed before
   the fix and pass afterward.
2. **P2 — Repeated section links did not return to their target.** The scrolling
   effect only observed the fragment text, so activating an unchanged fragment
   did nothing. Explicit unmodified section-link activation now requests a new
   scroll/focus operation, including links inside article prose.
3. **P2 — Article re-renders replaced focused Markdown nodes.** Inline renderer
   functions changed component identity during route/query updates, which also
   lost heading focus during responsive reflow. Renderer identities are now
   memoized. Resized headers realign an obscured focused section while leaving
   readers focused elsewhere undisturbed. Tests check node/focus preservation
   and resize behavior.
4. **P2 — Help treated the shell skip target as a missing section.** The article
   now recognizes `#main-content` as the app-wide landmark rather than an article
   heading. Invalid article fragments retain their useful recovery notice.
5. **P2 — Help print CSS leaked into other routes.** Lazy CSS remains loaded
   after client navigation. The global root/layout/header print overrides are
   now conditional on the Help workspace being mounted. Browser verification
   navigates from Help to Tests without reloading and checks print isolation.
6. **P2 — Stale API-group bookmarks silently hid valid operations.** An unknown
   group previously produced zero results while the select displayed All groups.
   Such bookmarks now use All groups and display an explicit recovery notice.

The production browser regression script passed after these fixes, including
all prior Help checks and the new edge cases. Build/typecheck, lint (existing
warnings only), and diff whitespace checks passed. Final test-run details are
recorded below.

Targeted follow-up: **10 files, 135 tests passed**, covering Help/integrity,
shell navigation/focus, routing, launch review, and the demo/admin/prompt files
that timed out under broader concurrency. The browser script passed its full
expanded matrix. Earlier broad runs were interrupted after severe local resource
pressure caused timeouts; no test timeouts or assertions were weakened.

Final full-suite rerun with `--maxWorkers=1`: **158 files, 1,412 tests passed**
(276 seconds). The final review pass found no outstanding findings in the changed
code. Backend/API contracts were unchanged, so no backend migration or provider
acceptance claims are added by this follow-up.
