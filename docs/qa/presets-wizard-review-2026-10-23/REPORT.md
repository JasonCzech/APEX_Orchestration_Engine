# Test presets × New test wizard — focused UX design review

**Date:** 2026-10-23 · **Build:** `claude/project-app-links` (presets and wizard unchanged from `main`) · **Reviewer:** Claude (review only; no code changes)

**Scope:** how test presets fit into creating a test:
- the **New test** wizard (`/runs/new`): where a preset is chosen, what applying it does, and what the wizard shows afterwards;
- the **Test presets** pages (`/test-presets`, `/test-presets/:id`);
- the reuse paths around them: **Resume draft**, **Create new test from this run** / **Edit and relaunch**, and **Re-run selected plan**;
- the **Scope** step's system rows, where the project is chosen.

The brief:
1. a preset should be an **initial option** when setting up a new test;
2. a configured test should be **savable as a preset** for future runs.

Added after the first pass:

3. presets must stay navigable when there are many, across many applications;
4. the wizard should offer only presets mapped to the **primary application under test**;
5. on Scope, the **project** should be selectable only for the primary system.

**Environment:**
- **Data:** the offline demo build (4 presets, seeded tests and drafts); the demo state was cleared afterwards.
- **Widths:** 1440×900 and 768×1024.
- **Role:** the demo presenter is an administrator scoped to all five projects, so not unscoped.

---

## Verdict

Presets work as a settings bundle, but the experience treats them as an afterthought. Neither part of the brief is met today.

1. **A preset isn't an initial option** (F-01). The wizard opens on Scope. Presets appear halfway down step 2, after the Engine choice. Yet a preset sets the systems under test (step 1), the engine, phases, review policy, workload and prompts. Choosing one late overwrites work already done, behind a confirmation dialog.
2. **A configured test can't be saved as a preset** (F-02). There is no save action in the wizard, on Review, or on a test's page. The UI can't create a preset at all: presets are LangGraph assistants, which only unscoped platform administrators may write. The only edit path is raw JSON.
3. **Presets don't explain themselves** (F-04, F-05, F-07). A preset card shows a name and description, with no summary or preview. After applying one, the wizard shows a "config inherited" chip and nothing about what it set or what you changed since. The preset pages describe an older model: "0 prompt pins" for every preset, and no workload, phase owners or analysis tools.
4. **Presets don't scale and aren't scoped** (F-15, F-16). Both the presets page and the wizard show every preset as a card, in one unfiltered grid. The wizard offers presets for unrelated applications, and nothing records which application a preset is for.
5. **Every system on Scope asks for a project** (F-17). The project belongs to the test, through its primary system. Other systems only need an application and environment.
6. **The demo's flagship preset can't launch** (F-03). Two of the four presets pin engine `apexload`, which the wizard rejects as unsupported. The backend's ID is `apex_load`.

| Area | Usability | Layout | Functionality |
|---|---|---|---|
| Starting a test | ✕ no "start from" choice; preset not offered first | ◐ Resume draft sits above the steps | ◐ preset, copy-from-run and draft are three separate paths |
| Choosing a preset | ✕ after Engine, on step 2 | ✕ an unfiltered card grid | ✕ not scoped to the primary application; late choice overwrites scope, prompts, documents |
| Scope systems | ◐ project asked for every system; raw project IDs | ● | ◐ primary chosen after the rows are filled |
| After applying | ✕ "config inherited" chip only | ◐ header shows raw project ID | ✕ no record of preset or edits on Review or the test |
| Saving as preset | — | — | ✕ not possible anywhere |
| Presets pages | ◐ jargon ("all gated", "prompt pins") | ✕ cards don't scale; no grouping, search or filters | ✕ no create; edit is admin-only raw JSON; summaries out of date |

● good · ◐ needs work · ✕ broken or missing

---

## Findings

### P1 — fix first

#### F-01 — The preset is chosen late, after the choices it overwrites ([02](screens/02-wizard-start.jpg), [03](screens/03-config-presets.jpg), [05](screens/05-replace-preset-confirm.jpg))

**What happens:**
- The wizard opens on **1. Scope**, and the only "start from" control is **Resume draft**.
- **Test preset** appears on **2. Config**, under the Engine cards.
- Applying a preset replaces:
  - the systems under test;
  - the engine;
  - phase ownership;
  - the review policy;
  - the workload;
  - prompt selections and overrides.

  If the project changes, it also drops attached documents, tickets and context summaries.
- If any of that was already set, a confirmation dialog appears.

So the natural order (pick a preset, then adjust) is reversed. The person describes systems and picks an engine, then a preset undoes it.

**Recommendation:** add a **Start** step before Scope (see "Target design"). It offers four ways to begin, and choosing one fills the wizard before anything else is entered:
- **From a preset**
- **From a recent test**
- **Resume a draft**
- **Blank test**

Keep a "Change preset" action in the wizard's context bar for later switches. Remove the preset cards from Config.

#### F-02 — A configured test can't be saved as a preset ([06](screens/06-review-step.jpg), [09](screens/09-run-detail-actions.jpg), [07](screens/07-presets-list.jpg))

**Where it's missing:**
- **Review step:** offers Save draft, Back and Launch test.
- **A test's page:** offers Create new test from this run, Edit and relaunch, and Re-run selected plan.
- **Presets page:** has no **New preset**. Its empty state says "Ask an administrator to publish a preset".

**Root cause:**
- Presets are LangGraph assistants. `on_assistants_write` (`src/apex/auth/handlers.py`) allows create, update and delete only for **unscoped platform administrators**.
- This is deliberate. Assistant config merges in after run authorization, so an assistant could carry settings that bypass per-run checks.
- So "save as preset" for operators can't create assistants.

**Recommendation:** add APEX-owned presets alongside the platform ones.
- **Storage:** a `test_presets` record holding the wizard's draft payload (the subset chosen when saving). Drafts already do this via `/v1/drafts`.
- **Ownership:** each preset has an owner, a project, and a visibility (just me / project team).
- **Applying one:** fills the wizard's visible fields. Launch then authorizes and validates them like any other test, so no new privilege is needed.
- **Platform presets:** existing assistants stay admin-curated "Organization presets". Optionally, a platform admin can **Promote** a team preset into one.
- **Entry points:**
  - **Save as preset…** on Review, as a secondary action beside Save draft;
  - **Save setup as preset…** on a test's page, next to Create new test from this run.

  Both open the same dialog (see "Target design").

#### F-03 — Demo presets pin an engine ID the wizard doesn't know ([04](screens/04-preset-applied.jpg), [07](screens/07-presets-list.jpg))

**What happens:**
- `ACC add-a-line Release Certification` and `OPUS T4 Store-Peak Certification` pin `engine: "apexload"` (`apps/dashboard/src/dev-data/store.ts`). The backend's engine ID is `apex_load`.
- Applying either shows "This preset pins engine 'apexload', which this deployment does not support". No engine card is selected.
- The list labels both "Unknown engine (apexload)".
- The same ID appears in the demo's engine handles and events.

**Recommendation:**
- Use `apex_load` throughout the demo data.
- Add a store test asserting that every seeded preset uses a supported engine.

### P2 — clarity and completeness

#### F-04 — Preset cards don't say what they set ([03](screens/03-config-presets.jpg), [10](screens/10-config-presets-768.jpg))

**What happens:** wizard preset cards show only a name and a free-text description. Before applying, you can't see:
- which systems it targets;
- the engine;
- which phases an agent runs;
- the review policy;
- the workload;
- whether it pins prompts.

**Recommendation:**
- Give each card a compact summary: systems · engine · "5 of 7 phases by agent" · "Reviews every phase" · workload (e.g. "200 VUsers · 30 min").
- Add **Preview**, which lists every field the preset sets, in the wizard's own words.

#### F-05 — After applying, the wizard forgets the preset ([04](screens/04-preset-applied.jpg), [06](screens/06-review-step.jpg))

**What happens:**
- A **config inherited** chip and a **Clear** button replace the preset's name.
- The context bar (Project / Application / Environment) doesn't mention the preset.
- No field shows that its value came from the preset.
- Review lists the settings but not their source or what you changed.
- The launched test doesn't show it started from a preset, though the launch request carries the assistant ID.

**Recommendation:**
- **Context bar:** add a preset chip, e.g. "Preset: ACC add-a-line Release Certification · v4 · 2 changes", with actions **View changes**, **Reset to preset** and **Detach**.
- **Review:** show "from preset" next to inherited values and "changed" next to edited ones.
- **Test:** record the preset ID and version on the test, and show "Started from preset … (modified)" on its page.

#### F-06 — The replace-preset dialog understates what changes ([05](screens/05-replace-preset-confirm.jpg))

**What happens:**
- The dialog lists engine, phase selection, review policy, workload, prompt selections and overrides. It doesn't say it also **replaces the systems under test**.
- It mentions documents, tickets and summaries only conditionally, in prose.
- It also fires when the only "change" is a previously applied preset.

**Recommendation:**
- With F-01 most of these replacements move to the start.
- For a later **Change preset**, show a short before → after list of what changes, including systems under test and anything removed.
- Default focus to **Cancel**.

#### F-07 — The preset pages describe an older configuration model ([07](screens/07-presets-list.jpg), [08](screens/08-preset-detail.jpg))

**What happens:** `configView.ts` reads only the older fields: engine, gates, phases, `prompt_overrides`, limits and single-system scope. As a result:
- **List cards:** every preset shows "0 prompt pins", even when it carries prompt selections. Review policy is shown as "all gated".
- **Detail page:**
  - It omits the workload and acceptance thresholds, which phases a person completes, the analysis tools, the multi-system scope (frontend, middle tier, backend), and prompt selections.
  - It uses lower-case phase keys ("story analysis") and "gated/auto". The wizard uses "Story Analysis" and "Review every phase / Run automatically / Custom".
  - **Edit preset** is raw JSON, for unscoped admins only.

**Recommendation:**
- Render a preset with the wizard's own read-only summary: the Review step's sections, labels and wording.
- **Edit** opens the wizard in a preset-editing mode, so editing uses the same fields, validation and help text. Keep JSON as an advanced view.

#### F-08 — "New test from this preset" lands on step 2 ([08](screens/08-preset-detail.jpg))

**What happens:** the link opens `/runs/new?step=config&golden=…`, on Config. The preset set the systems under test on Scope, so the person must go back to enter a title and request.

**Recommendation:** open the wizard on **Scope**, with the preset chip shown (F-05) and its fields filled. Title and request are the only typical remaining inputs.

#### F-09 — Three reuse paths with different names, in three places ([01](screens/01-home.jpg), [02](screens/02-wizard-start.jpg), [09](screens/09-run-detail-actions.jpg))

**The paths:**
- **Resume draft:** a select above the wizard steps.
- **Test preset:** cards on Config.
- **Create new test from this run** and **Edit and relaunch:** a test's page.

Home offers only **New test** and a draft list; it never mentions presets.

**Recommendation:** a single **Start** step (F-01) presents all of these. Home's **New test** opens it, and a "Start from a preset" shortcut lists the user's most-used presets.

#### F-15 — Presets are cards in one ungrouped grid ([03](screens/03-config-presets.jpg), [07](screens/07-presets-list.jpg), [10](screens/10-config-presets-768.jpg))

**What happens:**
- The presets page and the wizard both render every preset as a card, in one grid with no order, grouping, search or filter.
- Four presets fit. At 30 presets across 10 applications (the demo's catalog), a person scans a page of near-identical cards to find the one for their application.
- Cards repeat the same chips, and a card's height depends on its description, so rows don't line up.

**Recommendation:**
- **Presets page:** a table grouped by **application**, with search, a project filter, and visibility and engine filters.
  - Columns: name, systems, engine, phases by agent, reviews, owner/visibility, last used, used by.
  - A "General" group holds presets with no application.
  - Group headers collapse.
- **Wizard:** a compact list (F-16), not cards: name, a one-line summary, and **Preview**.

#### F-16 — The wizard offers presets for every application ([03](screens/03-config-presets.jpg))

**What happens:**
- The wizard lists all presets: "OPUS T4 Store-Peak Certification" (Legacy Perf) appears while ACC is the primary system.
- Applying one silently switches the systems under test to the preset's.
- The application mapping exists only inside each preset's configurable bundle (`app_id`, or the primary participant of `test_scope`).
- The wizard's list call deliberately omits configuration, so it can't filter today.

**Recommendation:**
- Map each preset explicitly to the primary application it was built for. Store this as a field the list can filter on, e.g. assistant metadata `application_id` for organization presets and a column on `test_presets`. Presets with no application are "General".
- In the wizard, list **presets for the primary application** first, then **General** presets.
- A **Show presets for other applications** link reveals the rest, each labelled with its application and a warning that applying it changes the systems under test.
- Because applications can now serve several projects, scope presets to the **application**, not the project. A preset for ACC is usable from any project that tests ACC.

#### F-17 — Every system asks for a project; only the primary system should ([11](screens/11-scope-project-per-system.jpg))

**What happens:**
- Each system row (frontend, middle tier, backend) has its own **Project → Application → Environment** cascade:
  - the project options show raw IDs ("acc-perf");
  - application options repeat the project ("ACC · acc-perf").
- The test's owning project comes from the primary system ("Owning project: Choose a primary system"). Even so, the person picks a project for every system.
- The primary role is a radio button at the bottom of each row, chosen after the row is filled.

**Recommendation:**
- **Primary system:** choose it first, with its own **Project** and **Application**. The project selects the owning project; show names, with the ID on hover.
- **Other systems:** choose **Application** and **Environment** only.
  - List every application the user can reach.
  - Derive each participant's project automatically:
    - the owning project when the application is linked to it;
    - otherwise the single linked project the user can access;
    - only when several remain, a small "via project" select.
- **Backend:** participants already carry a project each, and cross-project participants are supported since migration 0044, so no API change is needed.
- **Moving the role:** moving "Primary system" to another row moves the project field with it.

### P3 — polish

| # | Finding | Recommendation |
|---|---|---|
| F-10 | The presets empty state says "Ask an administrator to publish a preset", which offers no action. | With F-02, offer "Create a preset" (opens the wizard in preset mode) and "Save a recent test as a preset". |
| F-11 | The presets list has no search, project filter, owner, visibility, last-used or "used by N tests". | Add search, a project filter and these columns or chips. Sort by recently used. |
| F-12 | After a preset sets the scope, the context bar shows the raw project ID ("acc-perf") although the project has a name. | Show the project name, with the ID on hover, as elsewhere. |
| F-13 | The version ("v4") has no history or change note. | Show "Updated by … · what changed" and keep earlier versions viewable. Tests record the version they used (F-05). |
| F-14 | "Config inherited" is jargon, and **Clear** doesn't say what it clears (only the preset link; the fields keep their values). | Replace both with the F-05 preset chip and its actions (Detach / Reset to preset). |

---

## Target design

### 1. Start (new first step)

`/runs/new` opens **Start**, in two parts.

**a. What are you testing?** The primary system: **Project** (names), then **Application**. This sets the owning project (F-17) and scopes the presets (F-16).

**b. How do you want to start?** Four options, shown as cards, filtered to that application:

| Option | What it shows | What it does |
|---|---|---|
| **From a preset** | A compact, searchable list (F-15) of this application's presets, then General ones, grouped as *Mine*, *Team* and *Organization*. Each row shows the F-04 summary and a **Preview** link. | Fills every field it stores, then opens Scope with the preset chip. |
| **From a recent test** | The last 10 tests of this application. | The existing copy-setup behaviour. |
| **Resume a draft** | Your drafts. | Moves out of the select above the steps. |
| **Blank test** | | Today's empty wizard. |

The Start step remembers the last choice. Deep links (`?draft=`, `?preset=`) skip Start.

### 2. While editing

- **Context bar:** "Preset: <name> · v4 · N changes", with **View changes**, **Reset to preset**, **Detach** and **Change preset…**. Change preset shows a before → after list (F-06).
- **Fields:** a field the preset set shows a small "preset" marker until edited, then "changed".

### 3. Save as preset

On **Review**, a secondary **Save as preset…** action beside Save draft. On a test's page, **Save setup as preset…**. Both open one dialog:

- **Name** (required, unique within its visibility) and **Description**.
- **Visibility:**
  - **Just me**;
  - **<Project> team**: anyone who can launch tests in that project;
  - **Organization**: platform admins only. This publishes an assistant preset.
- **Include** (checkboxes, with a summary of each):
  - systems under test;
  - engine and workload;
  - phase owners and review policy;
  - analysis tools;
  - prompt selections.

  Title, request, tickets and knowledge are **not** included by default; each is a separate opt-in.
- **Update existing:** when the test started from a preset you can edit, offer **Update <preset>** as well as **Save as new**.

### 4. Presets page

- **List** (F-15): a table grouped by application, with "General" for presets without one.
  - search, plus project, application, visibility and engine filters;
  - visibility chips (Mine / Team / Organization);
  - owner, last used, and "used by N tests";
  - **New preset**, which opens the wizard in preset mode.
- **Detail:** the same read-only summary as Review (F-07). Actions:
  - **Start a test from this preset**
  - **Edit**: opens the wizard in preset mode
  - **Duplicate**
  - **Archive**
  - **Promote to organization preset**: platform admins only

### 5. Platform notes

- **Team and personal presets:** a new APEX `test_presets` resource:
  - project-scoped, with an owner and visibility;
  - payload in the wizard draft format, which `/v1/drafts` already stores and validates.

  Applying one only fills the wizard, and launch revalidates everything.
- **Organization presets:** stay LangGraph assistants, admin-only. Their bundle can include settings the wizard doesn't expose, such as connections and limits.
- **Tests:** record `preset_id`, `preset_version` and `preset_kind` (team or organization) on the test's metadata, for F-05 and "used by N tests".
- **Application mapping (F-16):** a filterable `application_id` on both kinds of preset. Organization presets keep it in assistant metadata; `test_presets` has a column. The list endpoints then filter by application without loading every configuration.

---

## What works well

- **A real bundle:** a preset carries scope, engine, phases, gates, workload and prompts, and applying one sets them in one step.
- **Safe replacement:** replacing an applied preset asks first, and project or application changes remove only the data they invalidate.
- **Copy setup:** **Create new test from this run** copies a test's frozen settings into a draft, and launch revalidates them. That's a good base for "Save setup as preset".
- **Deliberate security model:** assistant writes are limited to unscoped platform admins, for a stated reason. The recommended design keeps that and adds a safer user-level path.
- **Server-side drafts** already roam across browsers and have project-scoped access. The same pattern suits team presets.

---

## Recommended order

1. **F-03:** fix the demo engine IDs. This is a one-line data fix, and it currently breaks the flagship demo preset.
2. **F-17:** the project only on the primary system, with other systems' projects derived. It's small and independent, and the Start step builds on it.
3. **F-01 + F-16 + F-09 + F-08:** add the Start step: primary system first, then From a preset (scoped to that application), From a recent test, Resume a draft or Blank. Deep links skip it. This needs the application mapping on presets.
4. **F-05 + F-14 + F-12:** the preset chip, change tracking, and recording the preset on the test.
5. **F-02:** save as preset. This needs the `test_presets` API, the dialog, and entries on Review and the test page.
6. **F-15 + F-04 + F-07 + F-06:** the grouped presets table, preset summaries, a wizard-style detail page and editor, and the before → after dialog.
7. **P3 items.**

---

## Method and limits

- **Code read:**
  - the wizard: `NewRunWizard.tsx`, `steps/ConfigStep.tsx`, `wizardState.ts`, `useWizardLaunch.ts`;
  - the presets pages: `golden-configs/*`, `configView.ts`, `useAssistants.ts`;
  - `CopyTestSetupButton.tsx`;
  - the drafts router and model;
  - the assistant write authorization in `auth/handlers.py`.
- **In the demo build:**
  - opened the wizard from Home;
  - applied presets on Config, on a blank draft and over an applied preset;
  - checked what each changed and how Review presents it;
  - viewed the presets list and a preset's detail;
  - checked a completed test's actions.
- **Not exercised:**
  - editing a preset (needs an unscoped admin key, which the demo presenter doesn't have);
  - launching from a preset;
  - the connected dev stack, which has no presets beyond the system default.
- **Demo state:** created drafts were discarded. No credentials appear in this report or its screenshots.

### Screenshots

| File | Shows |
|---|---|
| [01-home](screens/01-home.jpg) | Home: New test and drafts, no preset entry |
| [02-wizard-start](screens/02-wizard-start.jpg) | The wizard opens on Scope; Resume draft is the only start option |
| [03-config-presets](screens/03-config-presets.jpg) | Preset cards on step 2, after Engine, with name and description only |
| [04-preset-applied](screens/04-preset-applied.jpg) | After applying: "config inherited", unsupported-engine error, raw project ID in the context bar |
| [05-replace-preset-confirm](screens/05-replace-preset-confirm.jpg) | Replace dialog: no mention that systems under test change |
| [06-review-step](screens/06-review-step.jpg) | Review: Save draft / Launch only; no preset shown or savable |
| [07-presets-list](screens/07-presets-list.jpg) | Presets list: "Unknown engine (apexload)", "0 prompt pins", no New preset |
| [08-preset-detail](screens/08-preset-detail.jpg) | Preset detail: older model, JSON-only edit, link to step 2 |
| [09-run-detail-actions](screens/09-run-detail-actions.jpg) | A test's actions: copy and re-run, no save as preset |
| [10-config-presets-768](screens/10-config-presets-768.jpg) | Preset cards at 768 px |
| [11-scope-project-per-system](screens/11-scope-project-per-system.jpg) | Scope: a project for every system, raw IDs, primary chosen after |

---

## Remediation (branch `claude/presets-wizard`)

All seventeen findings are addressed. The after screenshots, from the offline demo, are listed at the end.

| # | What changed |
|---|---|
| F-01 | A fresh **New test** opens on **Start**: "1. What are you testing?" (project, application, tier) and "2. How do you want to start?" (From a preset / From a recent test / Resume a draft / Blank test). Deep links (`?draft`, `?step`, `?preset`) skip it, and entering the wizard opens it at the top. |
| F-02 | **Save as preset…** on Review and **Save setup as preset…** on a test's page. The dialog offers Update (when the test started from a preset you may edit) or Save as new. You choose who can use it (Just me / the project's team / Organization for platform administrators), whether it is only for the primary application, which sections to include (systems, execution, phases, prompts by default; title/request and tickets/knowledge opt-in), and a "what changed" note. Team and personal presets are stored by a new `/v1/test-presets` API (migration 0048), so any operator can save within their scope. Organization presets remain assistants. |
| F-03 | Demo data uses `apex_load`, the engine ID the wizard knows. |
| F-04 | Every preset row and picker entry summarises what it sets (systems, engine and workload, phase owners, review policy), in the wizard's words. **Preview** shows the full list. |
| F-05 | A **preset bar** under the context bar names the preset and version and counts the changes since it was applied: "no changes" or "N changes", with **View changes**, **Reset to preset** and **Detach**. Review says "Started from preset X · vN · changed since: …". Launches record the preset, its version and whether it was edited. The test's page shows "Started from preset X · vN". |
| F-06 | Applying a preset over a configured test shows a before → after table of the settings that change, warns when context (documents, tickets, summaries) will be cleared, and says how many sections stay as they are. **Back** is focused. |
| F-07 | One preset page for both kinds: header facts (application, project, version, updated by), **Settings** in the wizard's words, **Usage** (tests started from it that you can see, last used), **History** for team presets (each version with its note and a before → after diff), and, for platform administrators, the raw configurable and **Edit JSON (advanced)**. |
| F-08 | **Start a test from this preset** opens the wizard on Scope, with the preset applied. **Edit in the wizard** and **Duplicate** open it on Review, where Save as preset updates or copies it. |
| F-09 | Reuse starts in one place: Start's four options. The test page's **Create new test from this run** and Start's **From a recent test** copy the same setup, and **Save setup as preset…** sits next to them. |
| F-10 | The empty presets page offers **Create a preset** and **Save a recent test as a preset** to people who can start tests. |
| F-11 | The presets list has search, application (including General), project, who-can-use-it, engine and archived filters, kept in the URL, plus Who can use it, Last used and Tests columns. |
| F-12 | The context bar and preset pages show the project name, with the ID on hover in the context bar. |
| F-13 | Team presets keep every version, with who saved it, when, and the note. Saving over a newer version is refused (409) and the dialog reloads the preset. |
| F-14 | "Config inherited" and its **Clear** are gone; the preset bar's **Detach** and **Reset to preset** replace them. |
| F-15 | The presets page is one table per application (General last), with shared column widths so columns line up down the page. At ≤900 px, Systems, Reviews and Last used drop out; on phones, only Name, Who can use it and Tests remain. The wizard's picker is a searchable list grouped the same way. |
| F-16 | The wizard offers presets **For &lt;application&gt;** and **General (any application)**. Presets for other applications sit behind "Show presets for other applications (n)", because they replace the systems under test. When the preset is for the chosen application, the project chosen on Start keeps owning the test. |
| F-17 | Only the primary system has **Project**. Other systems list every application you can reach and take the owning project when the application is linked to it, otherwise the one project you can reach it through. If there are several, a **Via project** field appears ("Reached through X" is shown otherwise). Project lists offer only projects you can own a test in. |

**Tests:**
- Backend:
  - 11 router tests cover:
    - visibility: personal presets are the creator's, and the audience must be reachable;
    - project-wide scope for sharing;
    - systems outside the caller's scope: participants, the legacy primary fields, history versions;
    - the version conflict, archive and delete;
    - usage counted over visible tests only;
    - paging over visible presets;
    - updates that leave visibility and application alone unless sent;
    - execution bundles stripped of scope and prompt keys.
  - 3 provenance tests cover:
    - once-per-test recording;
    - swallowed failures;
    - the server-side check: unknown presets, others' personal presets and other projects' presets are not recorded; the stored name and version win; credential-looking names are dropped.
  - The launch test asserts the preset is recorded.
  - The authorization matrix classifies the new operations.
- Dashboard: 12 preset model tests, 8 wizard preset tests and 13 presets page tests. The wizard, resume and config suites moved to Start.
- E2E:
  - A new `presets-wizard` spec covers Start → a preset for Mule → a change → Save as new → the grouped list → the detail page. It also checks Start and the list at 390 and 768 px.
  - The route census adds `/runs/new?step=scope` and a team preset page.

**Independent review:** a separate review found 13 issues, all fixed:
- **Paging:** the list's 200-row page was cut before personal presets were filtered out, so others' personal presets could crowd out usable ones. Filtering now happens before paging, the dashboard loads every page, and a deep link to an archived preset says so.
- **Visibility on update:** an update that left out `visibility` made a personal preset team-visible.
- **History:** version history could show systems outside the viewer's scope.
- **Prompts:** unchecking Prompts still saved prompt overrides carried in a test's execution bundle.
- **Scope check:** the legacy primary fields and the execution bundle skipped the systems scope check.
- **Reset:** Reset to preset kept tickets and documents from another project.
- **Update:** Update re-bound the preset to the current application. It now keeps its own, and warns when the test has moved project.
- **Provenance:** launch provenance trusted the client's preset name and ID. Team presets are now checked on the server, and names are screened.
- **Retries:** provenance was not recorded on an idempotent retry.
- **Derived projects:** other systems lost their derived project while the applications list was loading.
- **Project lists:** they offered projects the person couldn't launch in.
- **Start's project:** a preset chosen on Start could override the chosen project.
- **Conflicts:** a 409 conflict kept failing until the cache expired.

**Not covered:**
- The connected dev stack was not used. It needs migration 0048 before the presets API works there.
- Organization preset saves still go through LangGraph assistants and need an unscoped administrator key, as before.

### After screenshots

| File | Shows |
|---|---|
| [after-01-start-1440](screens/after-01-start-1440.jpg) | Start with ACC Perf / Mule chosen: presets for Mule, then General, others behind a toggle |
| [after-02-preset-bar](screens/after-02-preset-bar.jpg) | The wizard after Use preset: the preset bar, at the top of Scope |
| [after-03-scope-project-primary-only](screens/after-03-scope-project-primary-only.jpg) | A middle-tier OPUS system: no Project; Via project, because OPUS is in several projects |
| [after-04-apply-preset-confirm](screens/after-04-apply-preset-confirm.jpg) | Changing preset: the settings that change, before → after |
| [after-05-save-as-preset](screens/after-05-save-as-preset.jpg) | Save as preset: Update or new, application, sections, note |
| [after-06-presets-list-1440](screens/after-06-presets-list-1440.jpg) | Presets grouped by application, with usage |
| [after-07-preset-detail](screens/after-07-preset-detail.jpg) | A team preset: settings, usage, history with a diff |
| [after-08-run-detail-preset](screens/after-08-run-detail-preset.jpg) | A test's page: Started from preset, Save setup as preset |
| [after-09-home-presets](screens/after-09-home-presets.jpg) | Home: Start from a preset, by usage |
| [after-10-presets-list-1024](screens/after-10-presets-list-1024.jpg) | Presets at 1024 px |
| [after-10-presets-list-768](screens/after-10-presets-list-768.jpg) | Presets at 768 px, fewer columns |
| [after-10-presets-list-390](screens/after-10-presets-list-390.jpg) | Presets on a phone |
| [after-11-start-768](screens/after-11-start-768.jpg) | Start at 768 px |
| [after-11-start-390](screens/after-11-start-390.jpg) | Start on a phone |
