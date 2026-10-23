# Applications & environments — focused UX design, usability and functionality review

**Date:** 2026-10-23 · **Build:** `claude/prompts-catalog-ux` (this page is unchanged from `main`) · **Reviewer:** Claude (review only; no code changes)

**Scope:**
- The **Applications & environments** page in the left sidebar (`/environments`, `EnvironmentsPage.tsx`): the list, the filters, the New application and New environment panels, row actions, and the delete dialog.
- The **environment detail page** it opens (`/environments/:id`): execution target, configuration, the edit form and inventory.

**Environment:**
- **Data:** the offline demo build (the production bundle in demo mode, served on its own port). It holds 5 projects, 28 applications and 79 environments, and its state can be reset. All create, edit, scan and delete checks ran there; the demo state was cleared afterwards. The connected dev stack (1 application, 1 environment) was used read-only.
- **Widths:** 1440×900, 1024×768 and 768×1024.
- **Role:** the demo presenter is an operator, not a global administrator.
- **Theme:** light only; the theme preference is pinned.

---

## Verdict

The page covers the basic jobs: you can see each application's environments, add one, open it, approve its target and scan it. The data underneath is sound. Target approval is version-guarded, deletes need typed confirmation, and the role rules are enforced and explained. But the page doesn't scale past a handful of applications, and it hides the one thing a tester comes here to check: **can I run an external test against this environment?** In order of impact:

1. **"Add environment" on any group below the first screen appears to do nothing** (F-01). The form opens about 4,700 px above the viewport, every Add button disappears, and focus drops to the page body.
2. **Readiness is invisible in the list** (F-02). Target approval, whether a target URL exists, and the last scan only show on each environment's own page.
3. **The list is grouped by application name, not by project** (F-03). With real data that means 28 near-identical tables over about 6,600 px, and same-named applications from different projects interleave: "ACC" ×3, "BSSe" ×4, "Mule" ×3.
4. **Management gaps** (F-04, F-05). Applications can't be renamed, described, archived or deleted from the UI, and environments can't be renamed, although the API supports all of it.
5. **The create and edit forms expose internals** (F-06): `k8s`/`vm`/`other` with no explanation, and a raw Options (JSON) box for every operator. Non-admins get no Base URL field and no word on why.

| Area | Usability | Layout | Functionality |
|---|---|---|---|
| List | ◐ hard to scan; same names interleave | ✕ 28 tables, misaligned columns, noisy headers | ✕ no readiness signal |
| Filters | ◐ search doesn't narrow environments | ◐ always expanded | ◐ not kept in the URL |
| Add environment | ✕ form opens off-screen | ◐ | ◐ no target field for operators, unexplained |
| New application | ◐ focus not moved | ● | ◐ no inline duplicate check |
| Application management | — | — | ✕ no rename, archive or delete |
| Environment detail | ◐ jargon ("Edit reference", "Version 0", "Rescan") | ◐ Rescan stretches full width | ◐ no rename; delete only from the list |
| Delete dialog | ● typed confirmation | ● | ◐ doesn't mention references |

● good · ◐ needs work · ✕ broken or missing

---

## Findings

### P1 — fix first

#### F-01 — "Add environment" below the first screen opens its form off-screen ([02](screens/02-add-environment-offscreen.jpg), [03](screens/03-new-environment-form.jpg))
- `CreateEnvironmentPanel` always renders at the top of the page, above every group (`EnvironmentsPage.tsx:501`). Opening it hides every "Add environment" button (`:550`, `:559`), and nothing scrolls or moves focus.
- Measured in the demo: clicking **Add environment** on the 21st group ("Mule") put the form 4,696 px above the viewport, with focus on `<body>`. To the user, the button just vanishes.
- The same applies to **New application**: the form opens inline and focus stays on the page body.
- **Recommendation:** open both forms as a dialog, or as an inline panel inside the group that asked. Move focus to the first field, and return it to the trigger on close. Keep the other Add buttons available, or disable them visibly with a reason.

#### F-02 — The list hides whether an environment can be used ([01](screens/01-list-1440.jpg))
- Each row shows Name, Kind, Base URL, a host count and "Updated". It doesn't show:
  - whether the execution target is **approved**;
  - whether a target URL is set at all;
  - when the inventory was last scanned.
- Those are what decide whether APEX Load or LoadRunner can run. The wizard then blocks on them ("Select a target environment…", "Review environment target"), and today the only way to check is to open each environment.
- The file header says scan info was deliberately left off the list because the payload doesn't carry it. Approval and the target URL *are* in the list payload (`target_approved`, `base_url`).
- **Recommendation:**
  - Add a **Target** status column ("Approved", "Needs approval", "No target"), using the detail page's badge.
  - Add a project-level count of environments needing approval.
  - Add a filter for "Ready for external execution".
  - Add last-scanned when the API can include it.

#### F-03 — Grouping by application name doesn't scale ([01](screens/01-list-1440.jpg), [08](screens/08-list-768.jpg))
- Groups are applications sorted by name (`groupEnvironments`), with a full table each: 28 tables, about 6,600 px, roughly seven screens at 1440×900.
- Same-named applications from different projects sit next to each other with identical titles. Three "ACC" groups appear in a row, told apart only by a small grey project caption. The same goes for BSSe ×4, Mule ×3 and OPUS ×2.
- Each table sizes its own columns, so Kind and Base URL start at different positions from group to group ([02](screens/02-add-environment-offscreen.jpg), last group).
- **Recommendation:**
  - Group by **project** first (collapsible, with application and environment counts), then by application.
  - Use one table per application with fixed column widths (or one table per project with an application column).
  - Allow collapse and expand, remembered per user.
  - Add a summary line ("5 projects · 28 applications · 79 environments").

### P2 — completeness and clarity

#### F-04 — Applications can't be managed from the UI
- The API has `updateApplication` (name and description), `archiveApplication`, `unarchiveApplication` and `deleteApplication` (`routers/catalog.py:490–550`). The dashboard has no hooks or controls for any of them.
- Once created, an application's name and description can't be changed, and a mistaken application can't be removed. The description entered at creation is never shown anywhere on the page.
- **Recommendation:**
  - Give each application header an overflow menu: Edit name and description, Archive / Unarchive, and Delete with typed confirmation and the number of environments affected.
  - Show the description under the application name.
  - Add a "Show archived" filter.

#### F-05 — Environments can't be renamed; delete is only on the list ([05](screens/05-edit-environment.jpg))
- The detail edit form ("Edit reference") has Kind, Hosts and Options only. `EnvironmentUpdate` accepts `name`, but the form never sends it.
- **Delete…** exists only in the list row's menu. The detail page, where you decide an environment is obsolete, has no delete.
- **Recommendation:** add Name to the edit form, and a Delete action on the detail page using the same typed-confirmation dialog. Rename the card "Edit environment".

#### F-06 — The create and edit forms expose internals ([03](screens/03-new-environment-form.jpg))
- **Kind:** `k8s` / `vm` / `other`, defaulting to `k8s`, with no explanation of what the choice affects.
- **Options (JSON):** a raw textarea with `{}`, shown to every operator, with only a parse error for guidance. The `_apex_` prefix is reserved, which is only explained after you type one.
- **Base URL:** shown only to global administrators (`canApproveTarget`). An operator creates an environment that external engines can't use, with no hint until the detail page says "Ask a global administrator…".
- **Duplicate names:** there's no inline check (the server returns 409 "application name already exists in project"). The demo store doesn't enforce uniqueness at all, so the demo let a second "ACC" into NPI, unlike the real backend.
- **Recommendation:**
  - Explain Kind in a sentence, and use readable labels (Kubernetes / Virtual machines / Other).
  - Move Options under a collapsed **Advanced** section with a short reference of supported keys.
  - For operators, show the Base URL as read-only with "A global administrator sets and approves the execution target", so the next step is clear.
  - Check names inline against the loaded catalog, and make the demo store enforce the same rule as the backend.

#### F-07 — The filters don't do what they suggest
- **Search matches whole groups, not environments** (`EnvironmentsPage.tsx:464`). Searching "PERFHYPER2" returned 3 groups with all 10 of their rows, PERFHYPER1 and PERFHYPER3 included.
- **Search and the project filter live in component state**, not the URL, so opening an environment and coming back resets them. The application filter, by contrast, is a URL parameter (`?application=`) with its own separate "All applications" button.
- **The Filters panel is always expanded** (`<details open>`) and takes about 110 px above the list. Its search box has no placeholder or icon.
- **Recommendation:**
  - Filter rows as well as groups, and highlight the match.
  - Keep `q`, `project` and `application` in the URL, with one Clear action.
  - Collapse the filters into a single toolbar row, alongside New application.

#### F-08 — Group headers are noisy ([01](screens/01-list-1440.jpg))
- Each application header repeats the project's name and id with **Copy** and **Edit project name** buttons, plus **Add environment**. That's 10 or more controls per group across 28 groups.
- Editing a project's display name from inside an application header is surprising, and repeating it on every application in that project multiplies the noise.
- **Recommendation:** with project grouping (F-03), show the project identity and its edit control once per project. The application header then needs only the name, a status summary, Add environment, and the F-04 menu.

#### F-09 — Detail-page wording and layout ([04](screens/04-environment-detail.jpg), [06](screens/06-detail-after-scan.jpg))
- **Headings:** the page title is a generic **Environment** (h1), and the environment's own name is a small h2 under a breadcrumb.
- **Breadcrumb:** it contains buttons ("Applications / NPI npi [Copy] [Edit project name] / Mule"). Breadcrumbs should be links only.
- **Execution target:** it says "Not approved · **Version 0**". The version number means nothing to users; it exists for the server's change check.
- **Inventory:**
  - A never-scanned environment offers **Rescan** rather than "Scan".
  - After a scan, the Rescan button stretches the full width of the card header.
  - It doesn't say which hosts were scanned, or that the result came from the demo source.
- **Missing links:** there's nothing to the environment's **Environment context** prompts (Prompts catalog) or to the tests that ran against it.
- **Recommendation:**
  - Use the environment name as the page heading, with the application and project as a link-only breadcrumb.
  - Replace "Version N" with "Changed since approval" when that applies.
  - Label the button "Scan" until the first scan, and keep it button-sized.
  - Add a "Related" card: context prompts (with an "Add environment context" link prefilled in the Prompts catalog) and recent tests.

### P3 — polish

| # | Finding | Recommendation |
|---|---|---|
| F-10 | An application with no environments shows both **Add environment** (header) and **Add first environment** (body). | Keep one. |
| F-11 | Creating an application jumps straight into the New environment form with no confirmation that the application was created. | Show "Checkout created. Add its first environment" with a Skip option. |
| F-12 | The delete dialog says it removes the environment "and its scan history", but not that tests, test presets and environment-context prompts may still reference it. | List the references (counts), or say they keep the old identifier. |
| F-13 | "Updated" shows only relative time, and hosts only a count. | Show the absolute time in the tooltip (already there) and the host names in a tooltip or an expandable row. |
| F-14 | Base URLs are long monospace strings that dominate each row, while the name is short bold uppercase. | Show the host only (`acc.perfhyper1.perf.example.test`), with the full URL in the tooltip, and give Name more weight. |

---

## What works well

- **Safety where it matters:** target approval is version-guarded, with a review dialog that refuses if the target changed. Delete needs the typed name and focuses the confirmation field.
- **Roles are enforced and explained:** viewers see read-only notes, operators without project-wide scope get a specific empty state, and only global administrators can set or approve targets.
- **Resilient states:** cached-data warnings with Retry, a problem card when nothing loads, and skeletons while loading.
- **Accessible basics:** row actions are an overflow menu with a specific label ("Environment actions: PERFHYPER1"), host rows have numbered labels, and the filter inputs have real labels.
- **Responsive:** no horizontal overflow at 1440, 1024 or 768 px.
- **Inventory:** scanning works and shows services, replicas and images.

---

## Recommended order

1. **F-01:** open the create forms where they're asked for (or in a dialog) and move focus. This is the only bug that makes a task look broken.
2. **F-02 + F-03 + F-08:** group by project, add the Target status column and readiness filter, and simplify the headers. That is one layout change.
3. **F-04 + F-05:** application management, environment rename, and delete on the detail page. Front-end only; the API already exists.
4. **F-06 + F-07:** form clarity, URL-kept filters, and search that narrows rows.
5. **F-09:** detail-page wording, plus the Related card.
6. **P3 items.**

---

## Remediation (branch `claude/apps-envs-ux`)

All fourteen findings are addressed. The demo screenshots after the change are listed at the end.

| # | What changed |
|---|---|
| F-01 | **Add environment** opens the form inside its own application, scrolls it into view and focuses Name. Cancel returns focus to the Add button. **New application** is a dialog; after it, filters that would hide the new application are cleared so its first-environment form is visible. |
| F-02 | Each row shows **Target** (Approved / Needs approval / No target), the target host and the **last scan**. The list endpoint now returns each environment's latest snapshot in one query (`latest_snapshots`). The **Target** filter and the summary's "N need target approval" link narrow to the environments that need action. |
| F-03 | Applications are grouped under their **project**. Projects collapse, and the collapsed set is remembered. Every table shares one column layout. At ≤900 px Platform and Hosts drop out; on phones only Name, Target and the row menu remain. |
| F-04 | Each application has a menu: **Edit details…** (name and description, with an inline duplicate check), **Archive… / Unarchive…** and, for global administrators, **Delete…**. **Show archived applications** brings archived ones back into the list, with their environments. Delete is offered to administrators with access to the application, as the API allows. |
| F-05 | The detail page's edit form has a **Name** field with a duplicate check, and the page has **Delete…**, which returns you to the application. |
| F-06 | **Platform** uses readable labels with help text. Options JSON moved under **Advanced options**. Operators see why there's no target field. Both forms check duplicate names inline, by exact match like the database, and archived applications count. Field names match their visible labels. |
| F-07 | Search, project, target, archived and application filters are kept in the URL. Search narrows rows to matching environments and also matches project names; the summary says how many match. **Clear filters** resets them. |
| F-08 | The project name and ID appear once, in the project header; application headers show only name, count and actions. |
| F-09 | The page title is the environment's name. The breadcrumb is links only (Applications & environments / project / application). A never-scanned environment offers **Scan**. "Edit reference" became **Edit environment**, and "Version N" is gone. A **Related** card lists environment-context prompts (with **Add environment context** for global admins) and the latest tests that used the environment. |
| F-10 | An empty application shows one action: **Add first environment**. |
| F-11 | After **New application**, the panel says "{app} was created" and offers **Skip for now**. |
| F-12 | The delete dialog counts the environment-context prompts tied to the environment (archived included), says they block the delete, and says tests and test presets keep the old identifier. Only prompts have a foreign key to environments. |
| F-13 | The host count's tooltip lists the host names; Last scan shows the absolute time on hover and refreshes after a scan. |
| F-14 | Rows show the target **host** only, with the full URL in the tooltip, and the name has more weight. |

**Tests:**
- 47 environment unit tests cover:
  - grouping, readiness, filters and URL state;
  - in-place forms and focus;
  - application edit, archive and delete;
  - collapse persistence;
  - rename and delete on the detail page;
  - the Related card;
  - role gating;
  - the archive aftermath;
  - storage that throws;
  - the `projectGroups` search and readiness logic.
- The route census expects the environment's name as the page heading.
- Backend tests assert the list returns `last_snapshot`.

**Independent review:** a separate review pass found 11 issues, all fixed:
- archiving an application left its environments under "Unknown project";
- a new application could be hidden by active filters;
- focus was lost when the inline form closed;
- duplicate checks were case-insensitive and missed archived names;
- the delete wording overstated what blocks a delete;
- Last scan went stale after a scan;
- delete was gated more strictly than the API;
- accessible names collided across same-named applications and didn't match visible labels;
- the demo store didn't cascade or enforce uniqueness;
- test gaps.

The remaining low item is that the list endpoint loads each latest snapshot's JSON to count services. That is acceptable at the 200-environment page size, and it is noted for later.

**Not changed here:** applications still belong to exactly one project (`applications.project_id`). In the demo data, an application used by several projects is therefore registered once per project, with its own copy of each environment. Making projects and applications many-to-many is a schema and access-scope change and is tracked separately.

### After screenshots

| File | Shows |
|---|---|
| [after-01-list-1440](screens/after-01-list-1440.jpg) | Project sections, Target column, target host, last scan |
| [after-02-add-environment](screens/after-02-add-environment.jpg) | Add environment opens in place under IDP, Name focused |
| [after-03-list-search-1024](screens/after-03-list-search-1024.jpg) | Search narrows rows; filter count and Clear filters |
| [after-04-environment-detail](screens/after-04-environment-detail.jpg) | Name as title, link breadcrumb, Related card |
| [after-05-edit-environment](screens/after-05-edit-environment.jpg) | Edit environment with Name, Platform and Advanced options |
| [after-06-list-768](screens/after-06-list-768.jpg) | 768 px: Platform and Hosts hidden, no overflow |
| [after-07-list-390](screens/after-07-list-390.jpg) | 390 px: stacked filters; Name, Target and menu only |

---

## Method and limits

- I read `EnvironmentsPage.tsx`, `EnvironmentDetailPage.tsx`, `EnvironmentTarget.tsx`, `environmentsForm.tsx` and `environmentsLogic.ts`, plus the catalog router's application and environment endpoints and models.
- **In the demo build I:**
  - created an application, including a duplicate name, and an environment;
  - opened the edit form, ran a scan, and walked the delete dialog up to confirmation (not confirmed);
  - used the project filter and search;
  - measured layout (form position, focus, scroll height, column widths, overflow) in the browser.
- **Not exercised:**
  - target approval as a global administrator (the demo presenter is an operator);
  - a real inventory scan against a live connection;
  - dark mode;
  - the viewer role in the browser (it's covered by the page's own role checks and tests).
- The demo state created during the review was cleared. No data on the connected dev stack was changed. No credentials appear in this report or its screenshots.

### Screenshots

| File | Shows |
|---|---|
| [01-list-1440](screens/01-list-1440.jpg) | The list at 1440×900: same-named applications, noisy headers, no readiness column |
| [02-add-environment-offscreen](screens/02-add-environment-offscreen.jpg) | After "Add environment" on a lower group: the buttons are gone, no form in view, columns misaligned |
| [03-new-environment-form](screens/03-new-environment-form.jpg) | The New environment form at the top of the page: Kind, raw Options JSON, no Base URL for operators |
| [04-environment-detail](screens/04-environment-detail.jpg) | Detail page: generic heading, breadcrumb with buttons, "Version 0", "Rescan" when never scanned |
| [05-edit-environment](screens/05-edit-environment.jpg) | The "Edit reference" form: no Name field |
| [06-detail-after-scan](screens/06-detail-after-scan.jpg) | After a scan: services table, full-width Rescan |
| [07-delete-dialog](screens/07-delete-dialog.jpg) | Typed-confirmation delete dialog |
| [08-list-768](screens/08-list-768.jpg) | 768×1024: no overflow, duplicate Add buttons on an empty application |
