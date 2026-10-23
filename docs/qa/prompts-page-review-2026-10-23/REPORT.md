# Prompts page — focused UX design and usability review

**Date:** 2026-10-23 · **Build:** `claude/prompts-step-phase-ownership` (`main` plus PR #34; the `/prompts` landing page itself is unchanged from `main`) · **Reviewer:** Claude (review only; no code changes)

**Scope:** the catalog landing page reached from **Prompts** in the left sidebar (`/prompts`, `PromptsPage.tsx`). That covers:
- the toolbar (search, Include archived, New prompt);
- the category filter;
- the catalog table;
- the preview panel;
- the New prompt dialog.

The full editor, version pages and playground are reviewed only where the landing page hands off to them.

**Environment:**
- Vite dev dashboard at `localhost:3000` against the local `langgraph dev` runtime, signed in as global admin.
- Seed catalog: 1 system prompt, 7 phase prompts, 7 legacy `/user` templates, no application or environment context.
- Widths: 1440×900, 1024×768 and 768×1024. Light theme; the theme preference is pinned to light, so dark mode was not exercised.

---

## Verdict

The page does its basic job: you can find a prompt, read its active text without leaving the list, and open the editor. But it is laid out like a database table rather than a catalog people choose prompts from, and at common laptop widths most of the table is cut away. The problems, in order of impact:

1. **The table loses its useful columns below 1440 px**, which is most laptops. Description is pinned to 74 px and always reads "Built-in …". Updated is hidden. Key and the repeated Category label are all that's left (F-01).
2. **The preview and the list disagree.** With a category selected, the previewed prompt is not the first row you see (F-02). The preview also repeats the description twice and can sit off-screen on tablets (F-03, F-09).
3. **Terminology doesn't match the rest of the product.** The page says "Namespaces", "Category", "System Prompt"/"Phase context", while phase prompts are keyed `…/system` and described as "system prompt for the … phase". The wizard and Review now say "Phase context" (F-04).
4. **Legacy `/user` templates take half of the "All" list.** They're mixed in with the selectable catalog, and the counts don't add up (F-05).
5. **The page doesn't explain where prompts are used.** Nothing shows which tests or presets use a prompt, whether a newer version is waiting, or how prompts combine at run time (F-06).
6. **The New prompt dialog** has unstyled conditional fields, a one-line content editor and no key guidance, and it defaults to the rarest category (F-07).

| Area | Usability | Layout | Completeness |
|---|---|---|---|
| Toolbar and search | ◐ search misses visible labels and prompt text | ● | ◐ no sort |
| Category filter | ◐ counts disagree; "Namespaces" jargon | ◐ strip cut off at 768 with no scroll cue | ● |
| Catalog table | ◐ only the key text selects | ✕ description and updated lost below 1440 | ◐ no usage or pending-version signal |
| Grouping | ◐ seven one-row groups for phases | ◐ heavy group headers | ◐ legacy rows mixed in |
| Preview | ✕ previews a different row than the first shown | ◐ duplicate description; off-screen at 768 | ◐ no versions or usage |
| Empty states | ◐ an empty category says "No prompts match the current filters" | ● | ✕ no category-specific create |
| New prompt dialog | ◐ wrong default category; no key rules | ✕ unstyled conditional fields; one-line editor | ◐ no template or copy-from |

● good · ◐ needs work · ✕ broken or missing

---

## Findings

### P1 — fix first

#### F-01 — Below 1440 px the table keeps only the key and a repeated category ([01](screens/01-landing-1440.jpg), [06](screens/06-landing-1024.jpg))
- `prompts.css` switches to `table-layout: fixed` at `max-width: 1440px`, pins the Description column to **74 px** and hides **Updated**. At 1440×900 the measured widths are: Key 266, Category 120, Description 74, Active 120, Updated 0.
- Every description therefore reads "Built-in …" or "Shared …". On a narrower viewport, Category ("Phase context") wraps onto two lines, and the `default` chip spills out of the Active cell (1024 px).
- The hidden Updated cells leave a blank strip at the right edge of each row, so striping and selection end before the panel does.
- **Recommendation:**
  - Make the list a two-line row instead of five columns. Line 1: a readable name (description or a humanized key) with chips (Active vN, Default). Line 2: the key in mono plus the updated time.
  - Drop the Category column whenever a category is selected, because the filter already says it.
  - Use container queries on the list panel, not the viewport width.

#### F-02 — The preview shows a different prompt from the first row ([02](screens/02-phase-filter-preview-mismatch.jpg))
- With no `?prompt=` in the URL, the page previews `rows[0]` of an alphabetically sorted array (`PromptsPage.tsx:389`), but the table renders groups in pipeline order.
- Choosing **Phase context** therefore highlights and previews `env_triage/system`, the third group down, while *Story analysis* sits at the top.
- **Recommendation:** derive the default selection from the rendered order (the first row of the first group), and scroll the selected row into view.

#### F-03 — On tablets the preview pushes the list below the fold, and changes off-screen ([07](screens/07-landing-768.jpg))
- At ≤900 px the preview is moved above the table (`order: -1`). At 768×1024 it is 545 px tall, so the list starts at y = 801.
- Choosing a row updates a panel the user can't see, with no scroll and no announcement.
- **Recommendation:** at narrow widths put the list first, and open the preview as a drawer or sheet over it (or navigate to the editor). Keep the preview beside the list only when there is room.

#### F-04 — Terminology conflicts with the wizard and with itself
- The filter is labelled **Namespaces** ("All Namespaces"), the column is **Category**, and the create field is **Category**.
- Labels are inconsistently cased: "System Prompt", "Phase context", "Application context", "Environment Context".
- Phase prompts are keyed `story_analysis/system`, and their descriptions say "Built-in **system prompt** for the story_analysis phase". Yet they appear under **Phase context**, and since PR #34 the wizard calls the shared prompt the *System prompt* and these *Phase context*. A tester reading "system prompt" here can't tell which slot is meant.
- **Recommendation:**
  - One vocabulary everywhere: **System prompt · Phase context · Application context · Environment context** (sentence case).
  - Rename "All Namespaces" to "All prompts".
  - Show a humanized title ("Story analysis — phase context") as the primary label, with the raw key secondary.
  - Reword the seeded descriptions so they stop calling phase prompts system prompts. The keys can stay.

### P2 — clarity and completeness

#### F-05 — Legacy `/user` templates are mixed into the main catalog, and counts don't add up ([01](screens/01-landing-1440.jpg))
- In **All**, seven *Compatibility — Legacy templates and unresolved entries* rows sit under the eight selectable prompts. They make up half the list, can't be selected in a test, and look identical apart from the group header.
- "All Namespaces 15" counts them, while the category counts (1 + 7 + 0 + 0 = 8) exclude them. Selecting any category hides them, and there is no filter to show only them.
- **Recommendation:** hide compatibility rows by default behind a "Show legacy templates (7)" toggle next to *Include archived*, give them a muted "Legacy · not selectable" chip, and make "All" count only what's listed.

#### F-06 — The catalog doesn't answer "where is this used?" or "what will run?"
- Each row shows only *Active vN* and *default*. There's no sign of:
  - how many versions exist;
  - whether a newer version is saved but not active;
  - which test presets or recent tests pin it;
  - which application or environment a context applies to (in the flat **All** view).
- There's also no explanation of how prompts combine. The order (system prompt → application/environment context → phase context) is now visible in the wizard but not here.
- **Recommendation:**
  - Add a one-line intro with a link: "Prompts are chosen per test in New test → Prompts; the selected versions are pinned at launch."
  - In the preview, add a small "Used by" section (presets / last N tests) and "N versions · vM latest".
  - Show the applicability ("Checkout · Staging", "Shared") on context rows in every view, not only in the grouped view.
  - The summary API (`PromptSummary`) has no version count or usage today. "Used by" needs a backend field; the version count could come from the detail call the preview already makes.

#### F-07 — The New prompt dialog breaks its own form layout ([04](screens/04-new-prompt-dialog.jpg), [05](screens/05-new-prompt-application-fields.jpg))
- The conditional fields (Phase, Applicability, Application, Environment, Make default) use a bare inline `<label>` with an un-headed select, while Category, Key and Description use the stacked `prompt-field` style. "Make default" floats beside Category.
- The **Content** editor opens one line tall, for a field that is normally 10–40 lines.
- **Key** shows only a placeholder ("checkout-analysis"). It doesn't explain the allowed format, uniqueness within a category, or that the key appears in the assembled prompt header (`=== APPLICATION CONTEXT: <key> ===`).
- The dialog always opens on **System Prompt**, even when the user is viewing *Application context* or *Environment context* (the empty state's "Create prompt" also ignores the current category).
- The primary button reads "Create and activate prompt", while the intro says "makes version 1 available for selection". Neither says whether it becomes the default.
- **Recommendation:**
  - Use one stacked field style.
  - Give the editor a minimum height of about 12 lines.
  - Prefill the category (and the application or environment) from the current filter.
  - Add key help text with inline validation.
  - Offer "Start from…" (copy an existing prompt).
  - Label the action "Create prompt (v1 active)".

#### F-08 — An empty category reads as a failed filter ([03](screens/03-empty-category.jpg))
- Choosing **Application context (0)** shows "No prompts found — No prompts match the current filters. [Clear filters]".
- Nothing is filtered: the category is simply empty, which is the normal state for a new install.
- **Recommendation:** use a category-specific empty state. Say what application context is for and when it's added to a test. Offer **New application context** (prefilled) for operators, and keep "Clear filters" for real search misses.

#### F-09 — The preview repeats itself and buries its actions
- The description is shown as the heading *and* again as a paragraph directly below it.
- *Active v1* and *Default* chips stack vertically with the relative time beneath them, in a narrow right column.
- **Make default** (global admin) and **Open full editor** sit at the very bottom, under a long code block.
- **Recommendation:**
  - Show the description once.
  - Put the chips and the updated time on one meta line.
  - Move **Open full editor** (and **New version**) into the preview header, so they're reachable without scrolling.

### P3 — polish and accessibility

| # | Finding | Recommendation |
|---|---|---|
| F-10 | Only the key text selects a row. Clicking the description, category or version cell does nothing, though rows have hover and striping. | Make the whole row the hit target, keeping one focusable button per row. |
| F-11 | Every preview click pushes a history entry (`setSearchParams` without `replace`), so Back steps through previews before leaving the page. | Use `replace: true` for selection; keep push for category and search. |
| F-12 | Search matches key, raw namespace (`phase`, `application`) and description only, not the labels people see ("Phase context", "Env triage") or prompt text. "Env Triage" finds nothing. | Normalize spaces/underscores and map labels to namespaces client-side. Consider content search on the server, and say what's searched in the placeholder. |
| F-13 | Phase view shows seven groups of one row each. Every group repeats the "Phase context" kicker and a "1 prompt" count. | When a group has one row, show the phase name in the row and drop the group header. Group only when a phase has alternatives. |
| F-14 | The category strip scrolls horizontally at ≤1440 px, with no fade or scroll cue; at 768 "Environment C…" is cut off. The stale code comment says the tree is a left column with "phase pinned first". | Wrap the chips, or add an edge fade. Update the comment. |
| F-15 | Filter buttons use `aria-current="true"`, which announces "current page" rather than a selected filter. Their name is built from two adjacent spans (label and count), with nothing between them. | Use `aria-pressed`, or a radio group, for a filter. Give the button an explicit name with a separator ("All prompts, 15"). |
| F-16 | There is no sort control, and the order is namespace then key, so alphabetical order beats pipeline order in **All**. | Default to category order (System → Application → Environment → Phase in pipeline order), with an optional "Recently updated" sort. |

---

## What works well

- **URL-backed state:** `?ns`, `?q`, `?archived` and `?prompt` make every view linkable and survive a reload.
- **Debounced, server-side search** with honest empty, loading, error and refresh-failed states (`ProblemCard`, `StateCard`, skeletons).
- **Reading without navigating:** the preview shows the full active text with line numbers, so most "what does this prompt say?" questions are answered from the list.
- **The create dialog behaves as a proper modal:** the page behind it is inert, Escape closes it, and it is protected while saving. Mutating actions are hidden by role.
- **Phase grouping is in pipeline order** once a category is selected.
- **No horizontal page overflow** at 1440, 1024 or 768 px.

---

## Recommended order

1. **F-01 + F-02 + F-03:** rework the list into two-line rows with container queries, derive the selection from the rendered order, and use a drawer preview on narrow screens. That is one layout change.
2. **F-04 + F-05:** settle the vocabulary and humanized titles, and separate legacy templates.
3. **F-07 + F-08:** tidy the create dialog and add category-aware empty states, both prefilled from the current category.
4. **F-09, F-10, F-11:** preview header actions, whole-row selection, history replace.
5. **F-06:** used-by and version info. This needs an API field for usage; the rest is front-end.
6. **P3 items.**

---

## Method and limits

- I walked the page at 1440×900, 1024×768 and 768×1024:
  - all categories, including an empty one;
  - search ("triage", labels with spaces);
  - Include archived;
  - row selection by key and by other cells;
  - the New prompt dialog for System and Application context.
- Layout facts (column widths, panel positions, overflow) were measured in the browser rather than estimated from screenshots.
- I read `PromptsPage.tsx`, `prompts.css`, `promptCategories.ts` and the backend search (`repositories/prompts.py`: key, namespace and description).
- **Not exercised:**
  - dark mode (the theme preference is pinned to light);
  - application and environment prompts with real data (the seed has none);
  - archived prompts (none exist);
  - a non-admin role;
  - the full editor, version and playground pages beyond the hand-off.
- No prompt was created, edited or archived. No credentials appear in this report or its screenshots.

### Screenshots

| File | Shows |
|---|---|
| [01-landing-1440](screens/01-landing-1440.jpg) | All prompts at 1440×900: truncated descriptions, hidden Updated, legacy rows |
| [02-phase-filter-preview-mismatch](screens/02-phase-filter-preview-mismatch.jpg) | Phase context selected; the third row is previewed (F-02) |
| [03-empty-category](screens/03-empty-category.jpg) | An empty category shown as a filter miss (F-08) |
| [04-new-prompt-dialog](screens/04-new-prompt-dialog.jpg) | New prompt: floating Make default, one-line editor (F-07) |
| [05-new-prompt-application-fields](screens/05-new-prompt-application-fields.jpg) | Unstyled Applicability and Application fields (F-07) |
| [06-landing-1024](screens/06-landing-1024.jpg) | 1024×768: wrapped category, chip overflow (F-01) |
| [07-landing-768](screens/07-landing-768.jpg) | 768×1024: preview above the list, cut-off category strip (F-03, F-14) |

---

## Remediation (2026-10-23)

Branch `claude/prompts-catalog-ux`, stacked on PR #34. All findings, P1 to P3, are addressed.

| Finding | Status | Change |
|---|---|---|
| F-01 Table loses its columns | Fixed | The list is now two-line rows. Line 1: a readable title with version, default and archived chips. Line 2: the key, its applicability, and when it was updated. There are no fixed columns, so nothing is hidden at any width. Clicking anywhere on a row selects it, which also covers F-10. |
| F-02 Preview mismatch | Fixed | The default selection is the first row actually shown. Groups and rows follow the order agents receive prompts (system → application → environment → phase in pipeline order). Choosing a row replaces the browser-history entry instead of adding one, which also covers F-11. |
| F-03 Off-screen preview | Fixed | When the page is narrower than 760 px, nothing is previewed until a row is chosen. The preview then opens as a sheet over the list. Focus moves to the sheet heading, and closing the sheet returns focus to the row. |
| F-04 Terminology | Fixed | One vocabulary everywhere, in sentence case: System prompt · Phase context · Application context · Environment context (catalog, wizard, Review, prompt-review gate). "All Namespaces" is now "All prompts". Rows lead with a readable title, and the old seeded wording ("Built-in system prompt for the … phase") shows as "<Phase> phase context". New installs seed "Default phase context for the … phase". |
| F-05 Legacy rows and counts | Fixed | Legacy templates are hidden behind "Show legacy templates (N)" and get their own group with a "can't be selected" note. A search always includes them. Category counts cover selectable prompts only, and "All prompts" is their sum (plus legacy rows when shown). |
| F-06 Where used / what runs | Fixed | Added an intro line explaining where tests choose prompts and the order agents receive them, with a link to New test → Prompts. Each category has a one-line explanation. The preview shows the version count and flags "vN saved, not active". Applicability is shown on every row. **Used by** is new: it lists the test presets that select the prompt (matched in the browser from each preset's selections) and the tests that pinned it, newest first, linked, with the version and slot. Tests come from a new `prompt_usages` index (migration 0046), which launches through the dashboard, `/v1` and Cycle dispatch write once their run exists. Recording is best-effort and never blocks a launch. `GET /v1/prompts/{id}/usage` shows only tests in the caller's scopes. Tests launched before this release aren't indexed. |
| F-07 New prompt dialog | Fixed | Stacked fields throughout. The category is prefilled from the filter (application context when no filter is set). Category help and key help are shown. Inline key checks cover duplicates within the category, spaces and length. "Start from" copies an existing prompt. The content editor is 240 px tall, the default checkbox explains what default means, and the button reads "Create prompt (v1 active)". |
| F-08 Empty category | Fixed | An empty category shows what it is for and offers "New <category>", prefilled. A search miss names the query and offers "Clear search". |
| F-09 Preview | Fixed | One header: category and applicability, title, key, then one meta line (Active vN, Default, version count, pending version, updated), with **Open full editor** and **Make default** at the top. The description is shown only when it differs from the title, and the version note sits under the content. |
| F-10 Only the key selects | Fixed | Each row is a single full-width button, named by its title and described by its key and status. |
| F-11 History per preview click | Fixed | Previewing beside the list replaces the history entry. On narrow pages the sheet adds one entry, so Back closes it. |
| F-12 Search misses labels and text | Fixed | Spaces, underscores and hyphens match one another ("Env Triage" finds `env_triage`). `%` and `\` are escaped; previously they acted as wildcards. Search also covers the active version's text, and always includes legacy templates. |
| F-13 One-row phase groups | Fixed | Phase headers appear only when some phase has alternatives, and then on every phase, so heading navigation stays accurate. |
| F-14 Scrolling filter strip | Fixed | The category chips wrap. The stale "tree" comment is gone. |
| F-15 Filter semantics | Fixed | Filters use `aria-pressed` and have an explicit name ("Phase context, 7"). Group headings read "Phase context, 2 prompts". |
| F-16 No sort | Fixed | Agent order is the default (system → application → environment → phase in pipeline order). A **Recently updated** sort (`?sort=updated`) lists everything newest first. |
