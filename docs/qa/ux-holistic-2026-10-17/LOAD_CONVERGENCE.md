# APEX Load convergence reference — gap analysis

**Date:** 2026-10-17 · **Subject:** `apps/dashboard` (APEX Orchestration) · **Reference:** `Project_Stormrunner/dashboard` (APEX Load, current `main`)
**Reference build:** APEX Load controller built from source at this review (`go build ./cmd/controller`, Go 1.26.5) and run on `127.0.0.1:4176` against a fresh SQLite database, seeded over `/api/v1` with 8 scripts and 6 tests. Screenshots below are from that running build, not from the 2026-09-09 captures.

### Reference captures

| file | screen | shows |
|---|---|---|
| `load-01-shell-dashboard.jpg` | Dashboard (Light) | shell: icon rail + flat top bar, underline tabs with count badges, cards, empty state |
| `load-02-library-scripts.jpg` | Scripts library (Light) | library: search + Source select, Filters/Create/Refresh, health chip row, table, Rows/Previous/Next |
| `load-02b-library-filters-pagination.png` | crop of the above | sentence-case field labels and table headers, pagination contract |
| `load-03-settings-section-nav.jpg` | Settings (Light) | side section list as a grouped card, settings cards, field labels + helper text |
| `load-04-personal-preferences.jpg` | Personal preferences (Light) | theme / motion / rows-per-table / word-wrap selects; the five-theme set |
| `load-05-form-new-run.jpg` | New run (Light) | form: numbered steps, labels above controls, warning callout, key/value summary, primary-then-secondary action stack |
| `load-06-tabs-table-pagination.jpg` | Script builder (Light) | card chooser with selected state, in-page underline tabs with counts, dense table with tags and right-aligned actions |
| `load-07-dark-theme-tabs-table.jpg` | Script builder (Dark) | the same screen in the default OLED theme |
| `load-08-dark-library.jpg` | Scripts library (Dark) | the same library in the default OLED theme |

---

## Verdict

Orchestration did not diverge from Load by drifting — it froze. ADR-0006's "ported verbatim" claim was true at port time and is now false in five measurable ways: Load replaced its entire Light theme with an AT&T-sampled palette (54 of 57 shared tokens in `light` now hold different values), added a `themeComponents.css` layer that flattens the ambient mesh gradient and frosted top bar Orchestration still renders, moved 309 `text-transform` and 273 `letter-spacing` declarations behind `--label-case`/`--label-tracking` (set to `none`/`normal`, making the whole product sentence case) while Orchestration still hard-codes `text-transform: uppercase` in 63 places, introduced a six-step `--font-size-*` scale plus a base-inheritance rule that Orchestration has no equivalent for (370 literal `font-size` declarations across 18 values, including seven at 9px), and built a full global `select`/`input` treatment on the `--select-*`/`--bg-input` tokens that Orchestration lacks entirely, so Orchestration's 60 `<select>` elements render with native browser chrome in all six themes. The gap is overwhelmingly a **token and global-stylesheet gap, not a component gap**: of the 23 gaps below, 11 are pure CSS with no TSX edits and would close most of the visible distance. The two places where convergence would be a regression are Orchestration's contrast-corrected `accessibility.css` (Load solves the same problem better, with a separate `--accent-text` token — adopt Load's *mechanism*, not its raw `--accent` values) and Orchestration's `TabList.tsx` keyboard contract, which Load has no shared equivalent for.

---

## Gaps ranked by user-visible impact vs implementation effort

| # | Gap | Impact | Effort | Class | Section |
|---|---|---|---|---|---|
| 1 | `light` theme is Load's retired indigo palette; 54/57 shared tokens differ | Very high | Low | (a) | [§1](#1-the-light-theme-is-a-different-product) |
| 2 | Ambient mesh gradient + frosted top bar Load deliberately removed | Very high | Low | (a) | [§2](#2-surface-treatment-flat-vs-frosted) |
| 3 | 63 hard-coded `text-transform: uppercase`; no `--label-case` | High | Low | (a) | [§3](#3-label-case-and-tracking) |
| 4 | No type scale; 370 literal `font-size` across 18 values; no base inherit | High | Medium | (a) | [§4](#4-typography) |
| 5 | Table headers 10px uppercase vs Load's 12px/700 sentence case | High | Low | (a) | [§5](#5-tables) |
| 6 | No global `select` styling; `--select-*` tokens absent | High | Low | (a) | [§6](#6-form-fields-and-selects) |
| 7 | `--modal-backdrop` declared twice, consumed zero times; 7 hard-coded scrims, 4 z-index tiers | High | Low | (a) | [§7](#7-dialogs-and-modals) |
| 8 | Eight distinct tab treatments; `.workspace-tab` restyled per page | High | Medium | (b) | [§8](#8-tabs-and-section-navigation) |
| 9 | `.btn-secondary` is Load's *superseded* filled style; weight 500 vs 700 | Medium-high | Low | (a) | [§9](#9-buttons) |
| 10 | `--subtle-fill` defined in 1 of 6 themes; zebra + chip fills vanish | Medium-high | Low | (a) | [§10](#10-cardspanels-and-the-glass-panel) |
| 11 | `--radius` 12px vs Load 16px; `--radius-sm` missing | Medium | Low | (a) | [§11](#11-radii-and-spacing-rhythm) |
| 12 | Status badges 12px pill uppercase vs Load 4px sentence case | Medium | Low | (a) | [§12](#12-badges-chips-tags-pills) |
| 13 | 6 chart series vs Load's 8; per-theme hues differ in all 5 shared themes | Medium | Low | (a) | [§13](#13-charts) |
| 14 | No global `input`/`textarea` rule; 97 inputs, 85 opt-in classes | Medium | Medium | (b) | [§6](#6-form-fields-and-selects) |
| 15 | No bundled font; `'Inter'` requested, never loaded → system fallback | Medium | Medium | (c)/(a) | [§14](#14-fonts) |
| 16 | `--accent-text` / `--danger-fill` / `--warning-fill-text` absent | Medium | Low | (a) | [§9](#9-buttons) |
| 17 | 5 tokens referenced but never declared; 2 with no fallback | Medium | Low | (a) | [§15](#15-undeclared-tokens) |
| 18 | No sentence-case machine-value treatment (`.status-badge-label`) | Low-medium | Low | (a) | [§12](#12-badges-chips-tags-pills) |
| 19 | No `--brand-highlight`; selected nav/tab indicator is raw `--accent` | Low-medium | Low | (a) | [§8](#8-tabs-and-section-navigation) |
| 20 | No shared pagination primitive; no `Rows` control contract | Low-medium | Medium | (b) | [§16](#16-library-controls-and-pagination) |
| 21 | Empty state ported correctly; no loading/error sibling | Low | Medium | (b) | [§17](#17-empty-loading-and-error-states) |
| 22 | Six themes vs Load's five (`apex-light` is a domain exception) | Low | — | (c) | [§18](#18-the-theme-set) |
| 23 | Orchestration's focus ring is `--accent`, offset 3px; Load's is `--focus-ring`, offset 2px, `!important` | Low | Low | (a) | [§19](#19-focus-and-icons) |

---

## 1. The `light` theme is a different product

**Load:** `dashboard/src/index.css:158-250`. Sampled from att.com in the October change. `--accent: #00388f` (cobalt), `--bg-primary: #f3f4f6`, `--bg-secondary: #ffffff`, `--border: #dcdfe3` (opaque), `--brand-highlight: #009fdb`, `--tab-link: #0072b2`, `--tag-bg: #baeefc`, `--app-sheen: transparent`, `--topbar-bg: #ffffff`.

**Orchestration:** `apps/dashboard/src/theme/themes.css:14-60` — still "Sleek Light (Pearl / Glass)": `--accent: #4f46e5` (indigo), `--bg-primary: #f8fafc`, `--bg-secondary: #f1f5f9`, `--border: rgba(0,0,0,0.08)` (translucent), `--app-sheen: rgba(79,70,229,0.03)`, `--topbar-bg: rgba(248,250,252,0.75)`.

**What a user sees:** the same named theme is indigo-on-pearl with translucent hairlines in Orchestration and cobalt-on-white with solid hairlines in Load. Every button, link, selected row, focus ring, chart series and status tint differs. Measured: **54 of the 57 tokens both repos declare under `[data-theme='light']` hold different values.** The three that match are `--bg-elevated`, `--accent-contrast` and `--theme-toggle-active-text`.

Per-theme divergence counts across the five shared themes (shared declarations / differing values):

| theme | shared | differ |
|---|---|---|
| `dark` (`:root`) | 68 | 17 |
| `light` | 57 | **54** |
| `solarized-dark` | 61 | 24 |
| `solarized-light` | 55 | 22 |
| `monokai-dimmed` | 55 | 13 |

**Change:** replace the whole `[data-theme='light']` block in `theme/themes.css:14-60` (and the `light` overrides in `theme/accessibility.css:11-21`) with Load's `index.css:158-250` verbatim. Load's light block is already contrast-engineered — its comments at `index.css:237` and `index.css:359` record the exact ratios it was tuned to — so the four `accessibility.css` light overrides (`--success`, `--warning`, `--danger`, `--info`, `--warning-text`) become redundant and should be deleted rather than layered on top. Re-run `scripts/check_dashboard_contrast.py` afterwards; the 43-pair check is the gate, not a reason to keep the old values.

Reference: `load-01-shell-dashboard.jpg`, `load-02-library-scripts.jpg` (Light), `load-07-dark-theme-tabs-table.jpg`, `load-08-dark-library.jpg` (Dark).

---

## 2. Surface treatment: flat vs frosted

**Load:** `dashboard/src/themeComponents.css:9-21`, with the comment "Flat surfaces: no ambient mesh gradient or frosted top bar."

```
:root[data-theme] body .main-content { background: var(--bg-primary); }
:root[data-theme] body .main-topbar {
  background: var(--bg-secondary);   /* opaque in every theme */
  border-bottom-color: var(--border);
  box-shadow: none; backdrop-filter: none; -webkit-backdrop-filter: none;
}
```

Load still *declares* `--app-sheen` and the gradient in `App.css`, but `themeComponents.css` outranks it (`:root[data-theme] body` beats a bare class) on every page. In Light, `--app-sheen: transparent` as well (`index.css:214`).

**Orchestration:** `apps/dashboard/src/components/layout/AppShell.css:16-28` still paints the two-stop radial mesh on `.main-content`, and `AppShell.css:44-55` still paints the top bar as a `linear-gradient(180deg, color-mix(--bg-elevated 72%), color-mix(--topbar-bg 92%))` with `backdrop-filter: saturate(135%) blur(28px)` and a three-part `box-shadow` including a 40px drop shadow. Nothing overrides it.

**What a user sees:** Orchestration's page background has two coloured blooms (one of them a hard-coded `rgba(59, 130, 246, 0.03)` blue at `AppShell.css:27` that is not a token and does not change per theme); the top bar is a translucent frosted slab with a shadow that scrolls content visibly through it. Load's are flat, opaque and bordered. This is the single largest "doesn't look like Load" difference and it is four declarations.

**Change:** add a `theme/themeComponents.css` imported last from `theme/index.css`, carrying Load's `themeComponents.css:9-21` verbatim against Orchestration's class names (`.main-content`, `.main-topbar` already match). Do not edit `AppShell.css` — Load keeps its own gradient source and overrides it, and matching that structure keeps future ports mechanical. Delete the hard-coded `rgba(59, 130, 246, 0.03)` stop at `AppShell.css:27` regardless; it is the only non-token colour in the shell.

---

## 3. Label case and tracking

**Load:** `index.css:58-59` declares `--label-case: none;` and `--label-tracking: normal;` once, in the base `:root`, so every theme inherits them. **309 `text-transform` declarations** and **273 `letter-spacing` declarations** across Load's 115 stylesheets route through `var(--label-case, uppercase)` / `var(--label-tracking, …)`. Only **2** hard-code `uppercase` (both on `[class='protocol-badge']`, which is an acronym, at `themeComponents.css:82-84`). The fallback in each `var()` preserves the pre-change uppercase look if the token is ever removed.

**Orchestration:** zero uses of `--label-case` or `--label-tracking`. **63 hard-coded `text-transform: uppercase`** declarations across 20 files — `features/runs/run-detail.css` has 13, `features/environments/environments.css` 5, `features/runs/live.css` 5, `features/context/context.css` 4, `features/admin/admin.css` 4, `features/compare/compare.css` 4, `features/analytics/analytics.css` 4, `theme/primitives.css` 2 (`.status-badge:263`, `.data-table th:408`). Tracking is scattered across 9 values: `0.05em` ×21, `0.06em` ×16, `0.04em` ×12, `0.03em` ×5, `0.12em` ×4, `0.08em` ×4, `0.18em` ×1, `0.09em` ×1.

**What a user sees:** Orchestration shouts. Table headers, status badges, section eyebrows, phase labels and metric captions are ALL CAPS with letter-spacing; the same elements in Load read "Authoring health", "Run releases", "Local authoring health" in sentence case (`load-02b-library-filters-pagination.png`).

**Change:** add `--label-case: none;` and `--label-tracking: normal;` to `theme/tokens.css` `:root`, then rewrite all 63 `text-transform: uppercase` declarations to `text-transform: var(--label-case, uppercase)` and the `letter-spacing` literal in each of those rules to `letter-spacing: var(--label-tracking, 0.05em)`. Orchestration declares `letter-spacing` 69 times in total; 64 are tracking values in the 0.03em–0.18em range, and the ones paired with an uppercase rule go first. This is a mechanical `sed` across `features/**/*.css`, `components/**/*.css`, `hitl/hitl.css` and `theme/primitives.css`. Leave `letter-spacing: -0.03em` on `h1,h2,h3,h4` (`tokens.css:129`) alone — Load keeps the same value at `index.css:542`.

---

## 4. Typography

**Load:** `dashboard/src/typography.css` — six tokens at `typography.css:10-17`:

```
--font-size-xs: 11px;  --font-size-sm: 12px;  --font-size-base: 13px;
--font-size-md: 14px;  --font-size-lg: 15px;  --font-size-title: 20px;
```

Three things make the scale work, and only one of them is the tokens:

1. **A base-inheritance rule** (`typography.css:19-21`): `body .main-content > :not(.main-topbar) { font-size: var(--font-size-base); }`. The file's own header comment says why: text without a size used to inherit the browser's 16px, so pages built from plain elements read noticeably larger than the rest.
2. **One shared small-label rule** (`typography.css:48-56`) applying `--text-secondary` / 12px / weight 700 / `var(--label-tracking)` / `var(--label-case)` to field labels **and** table headers in one selector — the comment at `typography.css:45-47` records that pages had been using "10-13px, regular to extra-bold, ink or muted" before.
3. **Three heading tiers**: content title 20px/700 (`typography.css:29-34`), section title 15px/700 (`typography.css:37-43`), long-form help 14px (`typography.css:24-26`).

Honest note: Load's *component* CSS is not token-driven for size — it holds 1,985 literal `font-size` px declarations against only 62 `var(--font-size-*)` uses. The scale is enforced by inheritance and the shared label rule, not by replacing every literal.

**Orchestration:** no `typography.css`, no `--font-size-*` tokens, no base-inheritance rule (nothing sets `font-size` on `body`, `#root` or `.main-content`; `tokens.css:114-125` sets `line-height` but not size). **370 literal `font-size` px declarations across 18 values**, plus 5 rem/em one-offs:

| px | count | px | count | px | count |
|---|---|---|---|---|---|
| 12 | 116 | 14 | 20 | 16 | 6 |
| 11 | 101 | 11.5 | 10 | 12.5 | 6 |
| 13 | 41 | 9 | **7** | 22 | 4 |
| 10 | 37 | 15 | 7 | 10.5 | 3 |
| 18 | 6 | 24/26/28/19/20 | 1 each | | |

**What a user sees:** two failures. First, any Orchestration text without an explicit size renders at the browser's 16px against 11–13px neighbours — the exact defect Load's `typography.css` header documents fixing. Second, the sub-11px tier has no Load equivalent: seven declarations at **9px** (`features/context/context.css:426`, `features/runs/run-detail.css:333,444,561`, `hitl/hitl.css:244`, `components/layout/Sidebar.css:165,376`) and three at **10.5px** (`features/runs/live.css:124,196,262`) are below Load's floor of 11px, and 37 at 10px against Load's 86-in-1,985 (4.3% of Load's declarations vs 10% of Orchestration's).

**Change:** add `theme/typography.css` carrying Load's six `--font-size-*` tokens verbatim, plus Load's base-inheritance rule retargeted to Orchestration's shell (`body .main-content > :not(.main-topbar) { font-size: var(--font-size-base); }` — the class names already match, see `AppShell.css:16` and `:32`), plus Load's shared label/table-header rule retargeted at `.data-table th`, `.ux-field > label` and `.ux-form-field > label`. Then:

- Replace the 101 `font-size: 11px` with `var(--font-size-xs)`, the 116 `12px` with `var(--font-size-sm)`, the 41 `13px` with `var(--font-size-base)`, the 20 `14px` with `var(--font-size-md)`, the 7 `15px` with `var(--font-size-lg)`.
- The **7 declarations at 9px and the 3 at 10.5px have no Load equivalent and should move to `--font-size-xs` (11px)**; the 37 at 10px likewise, except `theme/primitives.css:405` (`.data-table th`), which becomes `--font-size-sm` (12px) per §5.
- The 10 at 11.5px and 6 at 12.5px should round to `--font-size-sm` (12px); Load has no half-pixel sizes anywhere.
- `features/runs/run-detail.css`, `features/runs/live.css` and `hitl/hitl.css` hold 26 of the 29 half-pixel and 9px declarations between them and should be done first.

---

## 5. Tables

**Load:** two table styles, both routed through the label tokens.

- Generic `.data-table` — `components/Pages.css:2777-2799`: container `background: var(--bg-card)`, `border: 1px solid var(--border)`, `border-radius: var(--radius)` (16px), `overflow: hidden`; `table { font-size: 13px }`; `th { padding: 10px 16px; border-bottom: 2px solid var(--border); color: var(--text-secondary); font-weight: 500; font-size: 11px; text-transform: var(--label-case, uppercase); letter-spacing: var(--label-tracking, 0.05em) }`.
- Dense `.access-data-table` — `components/Access.css:817-908`: `th, td { padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 12px }`; `th { position: sticky; top: 0; background: var(--bg-card); color: var(--text-muted); font-size: 10px; font-weight: 800 }` with an opaque triple-layer background at `sectionNavigation.css:375-378` so scrolled rows do not show through translucent `--bg-card`; selected row `box-shadow: inset 3px 0 0 var(--accent)`; `.num { text-align: right; font-variant-numeric: tabular-nums }`.
- **Both are then overridden** by `typography.css:50-56`, which sets every `.data-table`/`.access-data-table`/`.object-activity-table` `th` to `--text-secondary` / **12px / weight 700** / `var(--label-tracking)` / `var(--label-case)`. That rule is what actually renders (see `load-02-library-scripts.jpg`: "Script", "Folder", "Authoring health", "Run releases" in sentence case, bold, ~12px).
- Sortable headers are full-cell buttons: `Pages.css:2811-2825` (`min-height: 24px`) and `Pages.css:2837-2853` for the scripts library (`width: 100%; min-height: 36px; padding: 8px 16px`), with `font: inherit; letter-spacing: inherit; text-transform: inherit` so the button never breaks the header's typography, and `outline-offset: -2px` on focus so the ring sits inside the cell.
- Zebra is opt-in only; the default is hover (`--bg-hover`) plus a 1px row rule.

**Orchestration:** one table style, `theme/primitives.css:382-435`. Container radius **8px**, not `var(--radius)`; `background: var(--bg-primary)`, not `--bg-card`. `th, td { padding: 8px 10px; font-size: 12px }`. `th { color: var(--text-muted); font-size: 10px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; background: var(--bg-card) }` — no sticky, no opaque layering. `.striped tbody tr:nth-child(even) { background: var(--subtle-fill, transparent) }`, which resolves to **transparent in five of six themes** (§10). 41 `data-table` references across 19 `<table>` elements, plus 8 bespoke `th` rules in feature CSS.

**What a user sees:** Orchestration's column headers are 10px ALL CAPS in the muted grey; Load's are 12px bold sentence case in the secondary ink — about 20% larger and considerably more legible. Orchestration's table corners are 8px against Load's 16px container radius, so tables read as a different family from the cards around them. Orchestration's headers scroll away; Load's stick.

**Change:**
- `theme/primitives.css:403-410` → `color: var(--text-secondary); font-size: var(--font-size-sm); font-weight: 700; letter-spacing: var(--label-tracking, 0.05em); text-transform: var(--label-case, uppercase);`
- `theme/primitives.css:385` → `border-radius: var(--radius);` and `:387` → `background: var(--bg-card);` to match `Pages.css:2778-2781`.
- Add Load's sticky-header treatment from `Access.css:837-841` plus the opaque layering from `sectionNavigation.css:375-378` (`background: linear-gradient(var(--bg-card), var(--bg-card)), linear-gradient(var(--bg-card), var(--bg-card)), var(--bg-primary)`), which Orchestration needs for the same reason Load does: `--bg-card` is `rgba(15,17,26,0.65)` in the default dark theme.
- Add Load's sortable-header button contract (`Pages.css:2837-2865`) as a `.data-table th.sortable` rule. Orchestration currently has no shared sortable-header style at all.

---

## 6. Form fields and selects

**Load:** global, unavoidable, token-driven.

- `index.css:901-909`: every `input` (excluding checkbox/radio/file/hidden/range/color/button/submit) and `textarea` gets `min-height: 36px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-input); color: var(--text-primary)`, wrapped in `:where()` so component CSS still wins. Placeholder at `index.css:910`, disabled at `:911`.
- `index.css:606-708`: a 103-line `select` treatment. `min-height: 34px`, `border-radius: 8px`, `background-color: var(--select-bg) !important`, custom CSS-gradient chevron drawn from `var(--select-arrow)` with `appearance: none` (`:617-629`), hover `var(--select-hover-bg)` (`:631-633`), disabled `color-mix(in srgb, var(--select-bg) 76%, var(--bg-primary))` (`:640-644`), `option`/`optgroup` painted with `var(--select-menu-bg)` (`:646-650`), checked option `color-mix(in srgb, var(--accent) 26%, var(--select-menu-bg))` (`:652-655`), and a full `@supports (appearance: base-select)` block (`:660-708`) styling the Chromium customizable picker with `var(--dropdown-menu-bg)` and `var(--shadow-md)`.
- Five tokens exist purely for this: `--bg-input` (= `--bg-primary`), `--select-bg` (= `--bg-secondary`), `--select-hover-bg`, `--select-menu-bg`, `--select-arrow`, plus `--dropdown-menu-bg` for app-owned menus. All six declared once at `index.css:47` and `:105-109`.

**Orchestration:** no global `input`, `textarea` or `select` rule anywhere. `theme/tokens.css:151-153` sets `font-family: inherit` and stops. The only field styling is the opt-in `.field-input` / `.field-select` pair at `theme/primitives.css:444-472` (`min-height: 34px; border-radius: 8px; background: var(--bg-secondary); font-size: 12px; padding: 7px 10px`). Usage: **97 `<input>`, 60 `<select>`, 26 `<textarea>` elements against 85 `field-input` and 35 `field-select` class references** — so roughly 12 inputs, 25 selects and all 26 textareas carry no shared styling at all. None of the six `--select-*` / `--bg-input` tokens exists.

**What a user sees:** Orchestration's selects render with the operating system's native control in every theme. In the five dark themes that means a light-grey native popup listing black-on-white options over a near-black page, and a native chevron that ignores `--text-secondary`. Load's selects are theme-coloured end to end, including the option list. Orchestration's unstyled textareas fall back to the browser's 2px inset border and `#fff` background inside dark cards.

**Change (pure CSS, no TSX):**
1. Add the six tokens from the block in §20 to `theme/tokens.css`.
2. Copy Load's `index.css:606-708` `select` block and `index.css:892-913` input/checkbox/link block into `theme/primitives.css` verbatim. Both are written with `:where()` / bare element selectors, so they sit below every existing `.field-input` rule and will not fight it.
3. Once the global rules land, the 85 `field-input` and 35 `field-select` class attributes become redundant and can be removed in a follow-up — Load carries 1,546 `<input>` and 612 `<select>` elements with no per-field class at all. That part is class (b); steps 1–2 are class (a).

---

## 7. Dialogs and modals

**Load:** one scrim token, one stacking layer.

- `--modal-backdrop` is declared per theme — `index.css:121` (`rgba(0,0,0,0.75)`), `:225` (`rgba(29,35,41,0.5)`), `:310` (`rgba(0,43,54,0.8)`), `:401` (`rgba(253,246,227,0.8)`), `:487` (`rgba(0,0,0,0.6)`) — and **consumed by 6 stylesheets**: `components/Modal.css:4`, `components/ProjectModal.css:9`, `components/Access.css:592`, `components/GenerateReportModal.css:9`, `components/SnapshotBodyPreview.css:45`, `components/ux/ux.css:7` and `:62`.
- `components/ux/ux.css:2-22` defines one portal layer: `--ux-dialog-layer: 2000`, `.ux-dialog-layer { position: fixed; inset: 0; z-index: 2000; pointer-events: none }` with children `pointer-events: auto` and **`z-index: auto`** — every dialog in the app shares one stacking context instead of competing.
- Panel: `background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; max-width: calc(100vw - 40px); max-height: calc(100dvh - 40px)` (`ux.css:9-12`), with a 760px tier for form dialogs (`ux.css:15`) and 480px for confirmations (`ux.css:35`). Base `Modal.css:18-30` is 480px / 28px padding / `var(--shadow-md)`.
- Actions: `justify-content: flex-end; gap: 8px; margin-top: 20px` (`ux.css:18-20`), buttons `min-height: 36px` (`ux.css:21`) and `white-space: nowrap; flex: 0 0 auto` (`ux.css:22`). Headers `font-size: 18px; line-height: 1.4` (`ux.css:17`), `align-items: flex-start`.
- Disabled buttons inside dialogs get a real surface instead of opacity: `ux.css:106` — `background: var(--bg-secondary); color: var(--text-muted); border: 1px solid var(--border)`.
- `backdrop-filter: blur(8px)` on the scrim; motion disabled under `prefers-reduced-motion` at `ux.css:34`.

**Orchestration:** `--modal-backdrop` is declared twice — `theme/tokens.css:62` (`rgba(0,0,0,0.75)`) and `theme/themes.css:293` (`rgba(0,0,0,0.6)`, monokai) — and **consumed zero times**. Nine overlay classes each paint their own scrim:

| file:line | class | scrim | z-index | padding |
|---|---|---|---|---|
| `features/cycles/cycles.css:130-137` | `.cycle-dialog-overlay` | `#0008` (= `rgba(0,0,0,0.533)`) | 2000 | `1rem` |
| `features/approvals/approvals.css:210-222` | `.approvals-overlay-backdrop` | `rgba(0,0,0,0.35)` | 300 | `20px 24px` |
| `features/context/context.css:636-644` | `.ctx-overlay` | `rgba(0,0,0,0.45)` | 60 | `16px` |
| `features/environments/environments.css:233-241` | `.env-overlay` | `rgba(0,0,0,0.45)` | 60 | `16px` |
| `features/admin/admin.css:485-493` | `.adm-overlay` | `rgba(0,0,0,0.45)` | 60 | `16px` |
| `features/work-items/work-items.css:193-201` | `.wi-overlay` | `rgba(0,0,0,0.45)` | 60 | `16px` |
| `features/prompts/prompts.css:311-319` | `.prompt-modal-overlay` | `rgba(0,0,0,0.45)` | 60 | `24px` |
| `features/runs/preflight.css:7-16` | `.preflight-overlay` | `color-mix(… --bg-primary 55%, transparent)` | 60 | `padding-top: 10vh` |
| `features/runs/live.css:287-296` | `.launch-modal-overlay` | `color-mix(… --bg-primary 55%, transparent)` | 60 | `padding-top: 12vh` |

Four distinct opacity values (0.35, 0.45, 0.533, and a 55% colour-mix), four z-index tiers (60, 300, 2000, and `.navigation-overlay` at `AppShell.css:98` which uses the undeclared `--bg-overlay`), and three padding conventions. Only two of the nine blur. `role="dialog"` appears 3 times across the TSX.

**What a user sees:** opening a cycle dialog dims the page to 53%, an approval dialog to 35%, an admin dialog to 45%, and a preflight dialog not at all in the light themes (a 55% mix of `#f5f7fa` with transparent is nearly invisible). A dialog opened over another dialog stacks unpredictably across the 60/300/2000 tiers. In every light theme the pure-black scrims read as a hole, where Load's light scrim is `rgba(29,35,41,0.5)` — the theme's own ink.

**Change:** this is the cheapest high-impact fix in the document.
1. Replace the scrim colour in all nine overlay rules with `var(--modal-backdrop)` (`cycles.css:134`, `approvals.css:214`, `context.css:644`, `environments.css:241`, `admin.css:493`, `work-items.css:201`, `prompts.css:314`, `preflight.css:10`, `live.css:290`). That alone is nine one-line edits and makes the UX contract's tokens-only rule true.
2. Add `backdrop-filter: blur(8px)` to each, matching `Modal.css:9`.
3. Add Load's `.ux-dialog-layer` block (`components/ux/ux.css:2-22`) to `theme/ux.css`, set `--ux-dialog-layer: 2000`, and change the nine `z-index` declarations to `auto`. This needs the dialogs to render into one portal, so it is class (b); steps 1–2 are class (a).
4. Normalise the panel: `border-radius: 12px` (Orchestration's dialogs currently inherit `var(--radius)` = 12px, which happens to match — but after §11 raises `--radius` to 16px they must be pinned to 12px explicitly, exactly as Load does at `ux.css:11`).
5. Adopt `ux.css:106`'s disabled-button surface; Orchestration's `theme/ux.css:17-21` currently drops dialog buttons to `opacity: 0.45`, which is below Load's 0.5 and renders the label at roughly 2.6:1 on the dialog surface.

---

## 8. Tabs and section navigation

**Load:** one visual treatment, three selectors, zero component.

- `topbarNavigation.css:62-111` — page-level tabs in the top bar and the two in-page tab rows: `min-height: 40px; padding: 8px 12px; border: 0; border-radius: 0; background: transparent; box-shadow: inset 0 -3px 0 transparent; color: var(--tab-link, var(--text-secondary)); font-size: 13px; font-weight: 700; line-height: 1.2; text-transform: none; white-space: nowrap`. Hover: `inset 0 -3px 0 var(--border)` + `--accent-text`. Selected: `inset 0 -3px 0 var(--brand-highlight, var(--accent))` + `--text-primary`. Focus: `outline: 2px solid var(--focus-ring); outline-offset: -2px`. Disabled: `opacity: 0.55`.
- `topbarNavigation.css:116-155` — `.ux-underline-tabs`, the opt-in class any other tab row applies to its `role="tablist"`, matched **by role** (`[role='tab']`) so a tab wrapped for a tooltip still counts. Identical treatment at `min-height: 34px; padding: 6px 12px` for in-content density.
- `topbarNavigation.css:158-170` — count badges inside tabs: `min-width: 20px; padding: 1px 7px; border-radius: 999px; background: var(--subtle-fill-strong); color: var(--text-primary); font-size: 11px; font-variant-numeric: tabular-nums`.
- `sectionNavigation.css:21-97` — the side section list (Settings, Access, Help): a card (`border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-card)`), rows at `min-height: 36px; padding: 8px 16px 8px 20px; font-size: 14px`, group headings 12px/700 with `var(--label-case)`, current row `background: var(--accent-soft); box-shadow: inset 4px 0 0 var(--brand-highlight, var(--accent)); font-weight: 700`. Below 1100px the same list becomes a horizontal scrolling row with the indicator rotated to `inset 0 -3px 0` (`sectionNavigation.css:258-308`). Rendered in `load-03-settings-section-nav.jpg` — note the group headings ("Platform", "Workspaces", "Connections", "Infrastructure") and the left-edge indicator on "Controller & runtime".
- Keyboard behaviour is not centralised — Load has no shared tab component.

**Orchestration:** one component and eight competing stylesheets.

`components/ux/TabList.tsx:20-53` renders `.workspace-tabs > .workspace-tab[role=tab]` with automatic activation, Arrow/Home/End, one tab stop and `aria-controls`. It is imported by exactly **two** pages (`features/settings/SettingsPage.tsx:28`, `features/runs/PhaseWorkspace.tsx:2`). Nine other `role="tablist"` blocks are hand-rolled:

| file:line | class | shape |
|---|---|---|
| `features/context/EvidenceTab.tsx:239` | `.ctx-detail-tabs` | underline, `border-bottom: 2px`, 10px, pad 8/9 (`context.css:516-532`) |
| `features/admin/ConnectionsPage.tsx:333` | `.adm-tabs` | pill, radius 8px, pad 5/12 (`admin.css:281-284`) |
| `features/admin/ConnectionDetailPage.tsx:703` | `.adm-tabs` | same |
| `features/apex-load/ApexLoadWorkspacePage.tsx:566` | `.adm-tabs` | same |
| `features/context/ContextPage.tsx:41` | `.ctx-tabs` | — |
| `features/new-test/NewRunWizard.tsx:501` | `.wizard-tab` | radius 8px, min-height 34px, pad 7/13, 12px (`wizard.css:57-77`) |
| `features/runs/EngineStrip.tsx:115` | `.engine-metric-tab` | radius 6px, pad 3/10, **10.5px** (`live.css:258-277`) |
| `features/prompts/PromptDetailPage.tsx:357` | `.prompt-tabs` | underline, 13px (`prompts.css:413-420`) |
| `features/runs/PromptTabsEditor.tsx:64` | `.prompt-review-tab` | radius **999px**, pad 8/14, 11px (`prompt-tabs-editor.css:10-29`) |

And the shared component is itself restyled per page: `theme/ux.css:210-217` gives `.workspace-tab` a bordered 8px chip at `font: 600 13px`, while `features/runs/run-detail.css:188-207` redefines the **same class** as a 999px pill at `font-size: 11px` on `var(--subtle-fill)`. The run-detail sheet loads later, so the one shared tab component renders as two different controls depending on the route — a direct contradiction of the UX contract's Tabs row.

**What a user sees:** eight tab shapes — 999px pills, 8px chips, 6px chips, 2px underlines — at five font sizes (10px, 10.5px, 11px, 12px, 13px) across the product. Load has one: a 13px/700 label with a 3px underline, everywhere (`load-06-tabs-table-pagination.jpg`, "Starter Templates 14 / Blank & Assisted Editors 11 / Reusable Blocks 16" with the count badges).

**Change:**
1. Add Load's `topbarNavigation.css:116-170` `.ux-underline-tabs` block to a new `theme/tabs.css`, matched by `[role='tab']` as Load does, plus the count-badge rule.
2. Add `className="ux-underline-tabs"` to the `TabList` root (`TabList.tsx:20`) and to all nine hand-rolled `role="tablist"` elements. That is one class attribute per site — nine TSX one-liners.
3. Delete the nine bespoke tab rules from `context.css`, `admin.css`, `wizard.css`, `live.css`, `prompts.css`, `prompt-tabs-editor.css` and the `.workspace-tab` overrides in `theme/ux.css:27-35,210-222` and `run-detail.css:188-207`.
4. Add `--tab-link` and `--brand-highlight` (§20) so the indicator and label colours are per-theme. Without `--brand-highlight` the selected indicator is raw `--accent`, which in Orchestration's `light` theme is indigo `#4f46e5` where Load's is AT&T cyan `#009fdb`.
5. Port `sectionNavigation.css:21-97` for `features/settings` and any future side-list page. Orchestration's settings nav is currently `.workspace-tabs` chips (`theme/ux.css:158`), not a card list.

Keep `TabList.tsx`'s keyboard logic exactly as it is — see "Where Load is weaker".

---

## 9. Buttons

**Load's effective `.btn`** is `index.css:767-775` **plus** `themeComponents.css:25-42`, and the second file wins:

| property | Load | Orchestration |
|---|---|---|
| source | `index.css:767-775` + `themeComponents.css:25-28` | `theme/primitives.css:77-85` + `theme/ux.css:10-21` |
| padding | `8px 16px` | `8px 20px` |
| radius | `999px` (`themeComponents.css:26`) | `999px` |
| font-size | `14px` | `14px` |
| **font-weight** | **`700`** (`themeComponents.css:27`) | **`500`** |
| min-height | none (36px only inside dialogs, `ux.css:21`) | `36px` (`ux.css:11`) |
| small | `.btn-sm` `4px 10px` / 12px | `.btn-sm` `4px 10px` / 12px + `min-height: 32px` |
| disabled | `opacity: 0.5` (`index.css:781-786`); real surface in dialogs/topbar (`ux.css:106`) | `opacity: 0.45` (`ux.css:17-21`) |
| `:active`/`:hover` guards | `:not(:disabled)` on every state | missing on `.btn:active`, `.btn-primary:*`, `.btn-secondary:*`, `.btn-danger:*`, `.btn-success:*` |

**`.btn-secondary` is the sharp one.** Orchestration ported `index.css:805-809` — `background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent-border)`, a *filled* button. Load superseded that with `themeComponents.css:30-42`:

```
:root[data-theme] body .btn-secondary {
  border: 1px solid var(--accent); background: transparent;
  box-shadow: inset 0 0 0 1px var(--accent);   /* 2nd px of a 2px ring, no height change */
  color: var(--accent-text);
}
```

So Load's secondary action is a transparent 2px-ringed outline button; Orchestration's is a tinted fill. Visible on every toolbar — compare `load-02-library-scripts.jpg` ("Filters", "Refresh" as outlines beside the filled "Create script") with Orchestration's tinted secondaries.

**`.btn-danger` / `.btn-success`.** Load uses `background: var(--danger-fill); color: var(--danger-fill-text)` (`index.css:852-855`) — dedicated tokens that guarantee ≥4.5:1 on the fill in every theme (`#dc2626`/`#ffffff` dark, `#c70032`/`#ffffff` light, `#c2410c`/`#ffffff` monokai). Orchestration uses `background: var(--danger); color: white` (`primitives.css:144-145`), then `theme/accessibility.css:88` overrides the text to `var(--danger-contrast)` = `#000000` in four of six themes — so the destructive button is **black text on `#f15e5e`**. `.btn-danger:hover` hard-codes `#dc2626` (`primitives.css:150`) and `.btn-success:hover` hard-codes `#16a34a` (`primitives.css:167`); neither value appears in any Orchestration or Load palette.

**Verb order.** Load puts the confirming action first and right-aligns the group (`ux.css:18-20`, `justify-content: flex-end`), and pins the primary last in builder toolbars with `order: 2` (`ux.css:151`). Orchestration right-aligns in three of nine dialogs (`prompts.css:351`, `live.css:344`, `work-items.css:227`) and has no shared rule. Load's inline create row is confirm-then-cancel ("Add" / "Cancel", `load-05-form-new-run.jpg`); Load's primary stack puts "Start now" above "Hold for manual start".

**Change:**
- `theme/primitives.css:82` → `font-weight: 700;` and `:78` → `padding: 8px 16px;`
- Replace `theme/primitives.css:108-112` with Load's `themeComponents.css:30-42` outline treatment, including the `inset 0 0 0 1px` ring and `:hover` at `themeComponents.css:37-42`.
- Add `--accent-text`, `--danger-fill`, `--danger-fill-text`, `--warning-fill-text` (§20); repoint `.btn-danger` to `var(--danger-fill)` / `var(--danger-fill-text)` and delete `theme/accessibility.css:88`.
- Delete the hard-coded `#dc2626` (`primitives.css:150`) and `#16a34a` (`primitives.css:167`) hovers; Load keeps the same fill on hover and changes only the shadow (`index.css:858-862`).
- Add `:not(:disabled)` to the five unguarded state selectors so disabled buttons stop lifting and scaling on hover.
- Adopt `ux.css:18-22` as the one dialog-action rule and delete the three per-feature copies.

---

## 10. Cards/panels and the glass-panel treatment

`.glass-panel` is the one primitive that ported cleanly: Load `index.css:758-764` and Orchestration `theme/primitives.css:65-71` are byte-identical, and `theme/ux.css:7-9` adds `border-radius: var(--radius-lg)`. Load adds the radius via `--radius` on the consuming rule instead; same result.

The gap is `--subtle-fill`. Load declares it in **all five themes** — `index.css:43` (`rgba(255,255,255,0.035)`), `:173` (`rgba(29,35,41,0.035)`), `:264`, `:353`, `:437` — and `--subtle-fill-strong` beside it. Orchestration declares `--subtle-fill` in exactly one place, `theme/apex-light.css:52` (`#f8fbfd`), and never declares `--subtle-fill-strong`. Nine Orchestration rules consume it: `theme/primitives.css:220` (`.topbar-meta-chip`), `:434` (`.data-table.striped`), `features/context/context.css:600`, `features/new-test/wizard.css:163`, `features/runs/run-detail.css:188,361`, `features/runs/prompt-tabs-editor.css:10`, `hitl/hitl.css:80`, `theme/apex-light.css:137`.

**What a user sees:** in five of six themes the zebra striping on `.data-table.striped` is absent, top-bar meta chips have no fill, and the `.workspace-tab` / `.prompt-review-tab` pills lose their resting background (falling to `var(--bg-secondary)` or `transparent` depending on the fallback each author happened to write — three different fallbacks across the nine sites).

The header comment at `theme/primitives.css:9-12` states that Load "references this custom property … but never defines it, so it computes to `transparent`". **That is no longer true** and the comment should be deleted along with the fallbacks.

**Change:** add `--subtle-fill` and `--subtle-fill-strong` per theme from the block in §20; delete the `, transparent` / `, var(--bg-card)` / `, var(--bg-secondary)` fallbacks at the nine call sites; delete the stale comment at `primitives.css:9-12`.

---

## 11. Radii and spacing rhythm

| token | Load | Orchestration |
|---|---|---|
| `--radius` | `16px` (`index.css:152`) | `12px` (`tokens.css:91`, `apex-light.css:80`) |
| `--radius-sm` | `8px` (`index.css:55`) | absent |
| `--radius-md` | absent | `8px` (`ux.css:5`) |
| `--radius-lg` | absent | `var(--radius)` (`ux.css:4`) |

Literal radii, by frequency:

- **Load:** `8px` ×404, `6px` ×213, `999px` ×184, `var(--radius)` ×123 (+22 with fallbacks), `10px` ×84, `4px` ×51, `12px` ×38. 145 references to a radius token.
- **Orchestration:** `var(--radius)` ×52, `8px` ×45, `999px` ×24, `6px` ×15, `10px` ×10, `12px` ×9, plus 13 one-offs (`9px`, `7px`, `20px`, `5px`, `4px`, `3px`, `2px`, `24px`, `14px`, `1rem`, `0.5rem`).

**What a user sees:** every card, table container and panel in Orchestration is 4px tighter at the corner than Load's. Combined with §5's 8px table container, Orchestration reads squarer throughout — which is the "Orchestration's square panels versus Load's rounded cards" note already recorded at `docs/qa/ux-audit-2026-09-09/REPORT.md:107` and **still unfixed thirteen months later**; this report quantifies it rather than re-raising it.

**Change:** `tokens.css:91` and `apex-light.css:80` → `--radius: 16px;`. Add `--radius-sm: 8px;` and keep `--radius-md`/`--radius-lg` as the local aliases they are (they are legitimate Orchestration-only tokens; see the three-way split). Pin dialog panels to a literal `12px` (§7.4) before raising `--radius`, or every dialog grows a 16px corner that Load's does not have. Normalise the 13 one-off radii to `var(--radius-sm)` (8px) or `6px`.

Spacing: Load's page padding is `padding: 10px 36px` on the top bar with `margin: -20px -36px 24px` (`App.css:40-41`); Orchestration's is `padding: 14px 28px` / `margin: -24px -28px 24px` (`AppShell.css:41-42`) against `.main-content { padding: 24px 28px 32px }` (`AppShell.css:19`). An 8px horizontal difference per side. Low impact, listed for completeness.

---

## 12. Badges, chips, tags, pills

**Load `.status-badge`:** `components/StatusBadge.css:1-10` — `padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; text-transform: var(--label-case, uppercase); letter-spacing: var(--label-tracking, 0.03em)`, **then** `themeComponents.css:46-49` overrides `border-radius: 4px` for `.status-badge` and `.script-health-token`, and `:53-55` for `.protocol-badge`. Tonal variants key off `rgba(var(--…-rgb), 0.15)` + the matching `--…-text` (`StatusBadge.css:12-49`).

Load additionally normalises **machine values**: `themeComponents.css:65-72` renders `.status-badge-label` as `text-transform: lowercase` with `::first-letter` uppercased, so `COMPLETE` from the API displays as "Complete" without changing the DOM text; `:86-92` capitalises `.script-health-token`, `.pp-pill`, `.access-status-badge`; `:82-84` keeps `[class='protocol-badge']` upper-case because protocols are acronyms (visible as the `HTTP` / `SOAP` / `JAVA` / `gRPC` tags in `load-02-library-scripts.jpg`).

**Orchestration `.status-badge`:** `theme/primitives.css:256-295` — same padding and tonal structure, but `border-radius: 12px` (never overridden to 4px), `font-weight: 600`, and `text-transform: uppercase; letter-spacing: 0.03em` hard-coded. No machine-value normalisation anywhere.

`.dash-badge`: Load `components/DashboardPage.css:77-86` uses `color: var(--accent-contrast)`; Orchestration `theme/primitives.css:190` uses `color: white`. In the default dark theme Load puts near-black `#050508` on the violet fill, Orchestration puts white — ~2.9:1 against `#a580f8`.

`.dash-context-chip` and `.topbar-meta-chip` ported correctly (`DashboardPage.css:68-75` ≡ `primitives.css:203-210`), except `.topbar-meta-chip`'s `var(--subtle-fill, transparent)` (§10).

**What a user sees:** Orchestration's status badges are 12px-radius ALL-CAPS pills; Load's are 4px-radius sentence-case tags. Orchestration shows raw API casing (`RUNNING`, `AWAITING_APPROVAL`) where Load shows "Running", "Awaiting approval".

**Change:** `primitives.css:263-264` → `text-transform: var(--label-case, uppercase); letter-spacing: var(--label-tracking, 0.03em);`. Add `border-radius: 4px` to `.status-badge` to match `themeComponents.css:46-49` (put it in the new `theme/themeComponents.css` from §2, not in `primitives.css`, so the port stays structurally parallel). Port `themeComponents.css:63-96` wholesale for machine-value casing. `primitives.css:190` → `color: var(--accent-contrast);`.

---

## 13. Charts

**Load:** 8 series tokens per theme (`--chart-1` … `--chart-8`), declared at `index.css:91-98`, `:204-211`, `:292-299`, `:383-390`, `:469-476`. 41 `.tsx` files import Recharts; 90 `var(--chart-N)` references in CSS and TSX. `--chart-7` and `--chart-8` are referenced once each — they exist as headroom for an 8-series legend rather than wrapping at 6. Axes/ticks are set per chart at `tick={{ fontSize: 11 }}` (e.g. `components/AnalysisWorkspace.tsx:246`) with series strokes as `stroke="var(--chart-1)"`; there is no shared axis/grid/tooltip wrapper.

**Orchestration:** 6 series tokens (`tokens.css:43-48`, `themes.css:48-53` etc.). 2 production Recharts files (`features/analytics/AnalyticsPage.tsx`, `features/runs/EngineStrip.tsx`) with 17 `var(--chart-N)` references. Axis and grid styling is actually **better** here and is handled consistently: `CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false}` and axis `stroke="var(--text-muted)"` at `AnalyticsPage.tsx:412-423,462-474,525,539` and `EngineStrip.tsx:133-143`. Series colours come from a `CHART_COLORS` array (`AnalyticsPage.tsx:444-445`) plus direct tokens at `:490,529-530,543`.

Per-theme hue divergence is total: **all six series colours differ in every one of the five shared themes.** Examples — dark `--chart-3`: Load `#c8800d`, Orchestration `#f59e0b`; light `--chart-5`: Load `#0d9bd4`, Orchestration `#2563eb`; solarized-dark `--chart-3`: Load `#08a198`, Orchestration `#b58900`; solarized-light `--chart-2`: Load `#2489d0`, Orchestration `#859900`. Load's October values were re-picked for series separability (its dark `--chart-1 #895af4` through `--chart-6 #ed499a` are desaturated against Orchestration's raw Tailwind `#8b5cf6`…`#ec4899`).

**What a user sees:** a six-series Orchestration chart and a six-series Load chart of the same data are different colours end to end; a seventh series in Orchestration wraps back to `--chart-1` and becomes indistinguishable from the first.

**Change:** replace all six `--chart-N` values per theme with Load's and add `--chart-7` / `--chart-8` (§20). Keep Orchestration's axis/grid/tooltip wiring exactly as it is — see "Where Load is weaker". Keep `CHART_COLORS` as the series cycle but extend it to eight entries.

---

## 14. Fonts

**Load:** four `@font-face` declarations at `index.css:1-31` for `'ATT Aleck Sans'` (Rg/Md/Bd/Blk, woff2, `font-display: swap`), served from `/fonts/att/` and committed in the built bundle at `pkg/api/static/fonts/`. `--font-sans: 'ATT Aleck Sans', -apple-system, …` (`index.css:57`).

**Orchestration:** `--font-sans: 'Inter', -apple-system, …` (`theme/tokens.css:92`). No `@font-face`, no bundled font file, no Google Fonts link. `'Inter'` is requested and never resolves unless the viewer happens to have it installed locally, so the product renders in `-apple-system` / `Segoe UI` for essentially every user.

**What a user sees:** different letterforms and different metrics — ATT Aleck Sans is noticeably wider at the same px size, so Load's 13px body reads larger than Orchestration's 13px system-font body even before §4's missing base rule.

**Change:** two options, and the choice is a licensing question this review cannot settle. (a) Bundle ATT Aleck Sans into `apps/dashboard/public/fonts/att/` and copy `index.css:1-31` + `:57` — full convergence, requires confirming the font licence covers Orchestration. (b) Bundle Inter properly (self-hosted woff2 with `@font-face`, not a bare family name) and accept the typeface difference as a documented domain exception. Option (b) is the honest minimum: the current state names a font it never loads, which is neither.

---

## 15. Undeclared tokens

Five custom properties are referenced in Orchestration and declared nowhere:

| token | site | fallback? |
|---|---|---|
| `--bg-app` | `features/new-test/wizard.css:447` | **none** — `background` computes to nothing |
| `--shadow-lg` | `features/admin/admin.css:590` | **none** — `box-shadow` is dropped entirely |
| `--bg-overlay` | `components/layout/AppShell.css:98` | `var(--bg-secondary)` |
| `--panel-border` | `features/context/context.css:148` | `rgba(255,255,255,0.12)` — a hard-coded white, wrong in all three light themes |
| `--text-tertiary` | `features/work-items/work-items.css:331` | `var(--text-secondary)` |

Load has its own undeclared references (20+, including `--accent-primary`, `--border-color`, `--error-color`), so this is not a Load-vs-Orchestration difference in kind — but Orchestration's two **fallback-free** cases are live defects: the new-test wizard panel at `wizard.css:447` paints no background, and the admin panel at `admin.css:590` has no shadow.

**Change:** `wizard.css:447` → `var(--bg-primary)`; `admin.css:590` → `var(--shadow-md)`; `AppShell.css:98` → `var(--modal-backdrop)`; `context.css:148` → `var(--border)`; `work-items.css:331` → `var(--text-muted)`. Then add a CI check that greps for `var(--…)` names absent from the theme layer — the one-liner used to find these is `comm -13 <(declared) <(referenced)`.

---

## 16. Library controls and pagination

**Load:** `components/LibraryPagination.css:1-3` — `.library-pagination { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 12px 0; color: var(--text-secondary); font-size: 12px }`, with the `Rows` select at `background: var(--bg-card); border-radius: 4px; padding: 5px`. The rendered contract is `Showing 1–8 of 8 · Rows [10 ▾] · Previous · Page 1 of 1 · Next` (`load-02b-library-filters-pagination.png`), and the `Rows` preference is a personal setting with exactly two options, `10 rows` / `25 rows` (`load-04-personal-preferences.jpg`, verified from the live DOM). The toolbar above it is `components/ux/ux.css:136-145` — `.ux-library-toolbar` / `-main` / `-actions` / `.ux-library-filter-fields`, with the search field flexed `2 1 260px` and everything else `1 1 150px`, actions pushed right with `margin-left: auto`, and a filter-count pill at `ux.css:246` (`min-width: 18px; border-radius: 999px; background: var(--accent); color: var(--accent-contrast); font-size: 11px`).

**Orchestration:** no `.library-pagination` primitive, no `.ux-library-toolbar`. Each list page builds its own. The UX contract's "Library controls" row already names the target ("Search, status/project filters, active-filter indicator, comparison guidance, named overflow actions and Rows / Previous / Next"), so this gap is a contract item that is partially met per-page rather than shared.

**Change:** port `components/ux/ux.css:136-145,244-247` and `components/LibraryPagination.css:1-3` into `theme/ux.css`, then retarget `features/runs/RunsListPage.tsx`, `features/work-items`, `features/prompts`, `features/compare` and `features/context` at the shared classes. Class (b) — it needs markup changes on five list pages. Lower priority than §1–§7: the pages work, they just each look slightly different.

---

## 17. Empty, loading and error states

`.dash-empty` ported cleanly and has not drifted: Load `components/DashboardPage.css:98-110` and Orchestration `theme/primitives.css:318-330` are identical (`var(--bg-card)`, `1px solid var(--border)`, `var(--radius)`, `24px`, centred, 14px, `--text-secondary`, `gap: 8px`), as are the `.small` / `.compact` / `-icon` / `-hint` modifiers. Live reference: `load-01-shell-dashboard.jpg` ("No completed tests yet · Start a test to build activity, performance, and release history. · [Start a new run]") and `load-05-form-new-run.jpg` ("No scenarios are available yet. Create the first reusable workload in the scenario builder.").

Load pairs it with a shared feedback block Orchestration does not have: `components/ux/ux.css:206-230` `.ux-notice` — `border-left-width: 4px`, tone from `--info-bg` / `--success-bg` / `--warning-bg` / `--danger-bg`, text always `--text-primary` so the tone sets the rail and tint only, with `.ux-notice-details summary` for a collapsible raw payload. Also `components/ux/ux.css:174-204` `.ux-undo-notice`, a fixed bottom-centre toast layered over `linear-gradient(var(--bg-elevated), var(--bg-elevated)), var(--bg-primary)` because `--bg-elevated` is translucent in the default theme.

Orchestration has `components/ProblemCard.css` (26 lines) and `features/runs/preflight.css`'s inline messages, but no shared notice/toast primitive and no loading-state primitive in either repo.

**Change:** port `ux.css:206-230` as `theme/notice.css` and retarget `ProblemCard` at it. Note that Load's `.ux-notice` uses `border-radius: var(--radius, 8px)` — after §11 raises `--radius` to 16px, that becomes a 16px notice in both products, which is correct. Class (b). Neither product has a loading primitive; that is a shared gap, not a convergence gap, and should not be filed against Orchestration.

---

## 18. The theme set

Load ships **five** themes plus "System (Auto)": `dark` (`:root`), `light`, `solarized-dark`, `solarized-light`, `monokai-dimmed`. Confirmed from the live preference select (`load-04-personal-preferences.jpg`): `System (Auto) | Dark Theme | Light Theme | Solarized Dark | Solarized Light | Monokai Dimmed`. The same screen shows Load's `Rows` preference offering only `10 rows` / `25 rows`.

Orchestration ships **six** plus `system` (`theme/useTheme.ts:19-40`): the same five plus `apex-light` ("Light (CTx)", `theme/apex-light.css`, 138 lines, cyan `--accent: #00a8e0` on `#f5f7fa`), and **defaults to `apex-light`** (`useTheme.ts:31`) where Load defaults to `dark`.

This is the clearest legitimate divergence in the document. The UX contract's Domain exceptions clause already names it: "Orchestration retains … its Light (CTx) compatibility theme". No change required beyond keeping `apex-light` outside the §1 light-theme replacement.

The *default* is worth a decision, not a change: Orchestration opens in `apex-light`, Load opens in `dark`. A reviewer moving between the two products sees a light app and a dark app. Either default is defensible; the contract does not currently say which, and it should.

---

## 19. Focus and icons

**Focus.** Load declares the ring twice: `index.css:555-558` (`:where(button, a, input, select, textarea, [role='button'], [tabindex]):focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px }`) and again at `index.css:563-565` with **`!important`** and a broader selector including `[role='tab']` and `summary`, under a comment explaining that components may restyle focus with borders or glows but keyboard focus always keeps the theme ring. Nested contexts override the offset to `-2px` (tabs, sticky table headers) so the ring stays inside the cell. There is also a `@media (forced-colors: active)` block at `index.css:581-588` marking selected tabs and `aria-current` with `Highlight` + underline, because Windows High Contrast drops the box-shadow that draws them.

**Orchestration** declares it twice too, inconsistently: `theme/primitives.css:370-373` uses `var(--focus-ring)` / offset 2px, and `theme/ux.css:36-44` uses **`var(--accent)` / offset 3px**. `ux.css` is imported last (`theme/index.css:16`) and both are single-class specificity, so the accent ring at 3px wins. In the default dark theme `--focus-ring` is `#a580f8` and `--accent` is also `#a580f8` (`accessibility.css:8-9`), so they coincide — but in `light` they are `#4f46e5` vs `#4f46e5` (coincide), in `solarized-dark` `#70b8df` vs `#70b8df` (coincide)… they coincide in all six only because `accessibility.css` sets `--focus-ring: var(--accent)`'s literal value in each block. That is fragile: changing `--accent` without changing `--focus-ring` silently desynchronises them. There is no forced-colors block.

**Change:** delete `theme/ux.css:36-44`, keep `theme/primitives.css:370-373`, and add Load's `!important` variant from `index.css:563-565` plus the forced-colors block from `index.css:581-588`. Note that after §1 replaces the light theme, Load's `--focus-ring: rgba(0, 87, 184, 0.7)` arrives and will no longer equal `--accent` — which is correct, and is exactly why the two tokens should not be kept in sync by hand.

**Icons.** Both products use inline SVG with `currentColor`; no icon-font or sprite in either. No gap found. Load's `a[target='_blank']::after` arrow with alternative text (`index.css:569-575`) and its `→` affordances on cross-page links (`sectionNavigation.css:110-112`, `:231-233`) have no Orchestration equivalent; low impact, listed for completeness.

---

## The three-way split

### (a) Pure token / CSS changes — no component edits

These eleven close most of the visible distance and touch no `.tsx` file.

1. **§1** Replace `[data-theme='light']` in `theme/themes.css:14-60` with Load's `index.css:158-250`; delete the now-redundant light overrides in `theme/accessibility.css:11-21`.
2. **§2** Add `theme/themeComponents.css` with Load's `themeComponents.css:9-21`; delete the hard-coded blue mesh stop at `AppShell.css:27`.
3. **§3** Add `--label-case` / `--label-tracking`; rewrite 63 `text-transform` and the matching `letter-spacing` declarations.
4. **§4** Add `theme/typography.css` (six tokens + base inherit + shared label rule); replace the 370 literal `font-size` declarations.
5. **§5** Retarget `.data-table th` (`primitives.css:403-410`) and the container radius/background (`:385,387`); add sticky + sortable-header rules.
6. **§6** Add the six `--select-*`/`--bg-input` tokens; copy Load's global `select` (`index.css:606-708`) and `input`/`textarea` (`index.css:892-913`) blocks.
7. **§7.1–7.2, 7.4–7.5** Repoint nine scrims at `var(--modal-backdrop)`; add `blur(8px)`; pin dialog radius to 12px; adopt Load's disabled-in-dialog surface.
8. **§9** `.btn` weight 700 / padding 8px 16px; replace `.btn-secondary` with the outline treatment; add `--accent-text`/`--danger-fill`/`--danger-fill-text`/`--warning-fill-text`; delete the two hard-coded hovers; add the five `:not(:disabled)` guards.
9. **§10–§11** Add `--subtle-fill` / `--subtle-fill-strong` per theme; `--radius: 16px`; add `--radius-sm: 8px`; normalise 13 one-off radii.
10. **§12–§13** Badge casing + 4px radius + `--accent-contrast` on `.dash-badge`; port the machine-value casing block; replace all per-theme `--chart-N` and add `--chart-7`/`--chart-8`.
11. **§15, §19** Fix the five undeclared tokens; delete `theme/ux.css:36-44`; add the `!important` ring and the forced-colors block.

### (b) Needs component edits

12. **§8** Tabs. Add `ux-underline-tabs` to `TabList.tsx:20` and to nine hand-rolled `role="tablist"` elements; delete eight bespoke tab stylesheets and the `.workspace-tab` override at `run-detail.css:188-207`. Nine one-line TSX edits, one shared stylesheet.
13. **§6.3** Remove the 85 `field-input` / 35 `field-select` class attributes once the global field rules land. Mechanical but touches ~40 files; do it after (a)§6 is verified, not with it.
14. **§7.3** Introduce a single `.ux-dialog-layer` portal and move all nine overlays into it with `z-index: auto`.
15. **§16** Shared library toolbar + pagination primitives, retargeting five list pages.
16. **§17** Shared `.ux-notice` primitive; retarget `ProblemCard`.
17. **§14** Bundle a font with real `@font-face` declarations (build config + `public/fonts/`).

### (c) Legitimate domain divergence — extend the contract instead

These should **not** converge. Each needs a line in `docs/design/APEX_UX_CONTRACT.md`'s Domain exceptions clause.

18. **`apex-light` ("Light (CTx)")** — already named in the contract. Keep it outside §1. The contract should additionally state which theme each product defaults to (§18).
19. **Phase review, discussion, gates, credential diagnostics** — `features/runs/PhaseWorkspace.tsx`, `components/runs/PhaseStrip.css` (268 lines), `hitl/hitl.css` (417 lines), `features/approvals`, `features/admin/ConnectionDetailPage.tsx`. Load has no load-test analogue for a multi-phase LLM pipeline with human-in-the-loop interrupt gates, a per-phase discussion thread, or per-connection credential diagnostics. The contract already lists "phase review, discussion, selected plans … and credential diagnostics"; it should be extended to say these surfaces may define their own layout primitives (`.phase-strip`, `.gate-module`, `.gate-summary`) provided they consume only shared tokens and the shared tab/button/field/dialog primitives. Note that `theme/ux.css:55-91` already holds gate-specific layout rules in the shared theme layer — those should move into `hitl/` so the theme layer stays domain-free.
20. **`--accent-results`** — Orchestration-only, used to keep assessed-acceptance colour independent of orchestration status, which is the contract's "Results" row ("Load completion, assessed acceptance, orchestration progress and evidence availability are separate facts"). Keep.
21. **`--sidebar-width` / `--sidebar-width-collapsed` / `--topbar-min-height`** — Orchestration-only tokens that externalise values Load hard-codes (`Sidebar.css:2`, `App.css:39`). Orchestration's version is strictly better and the comment at `theme/index.css:18-23` already explains why. Keep; do not "converge" by inlining them.
22. **`--radius-md` / `--radius-lg` / `--apex-ux-version`** — local aliases and a version stamp. Harmless. Keep, and add `--radius-sm` beside them.
23. **Six-series charts** — if Orchestration genuinely never renders more than six series, `--chart-7`/`--chart-8` are dead tokens. Adding them costs nothing and removes a wrap-around failure mode, so §13 still recommends them, but this is the one chart item that could reasonably be declined.

---

## Where Load is weaker

Four places where "converge on Load" would make Orchestration worse. Do not do these.

1. **Keyboard tabs.** `components/ux/TabList.tsx:17-48` gives Orchestration one implementation with automatic activation, roving `tabIndex`, Arrow/Home/End wrapping, `aria-controls` panel linking and a single tab stop. Load has **no shared tab component** — `topbarNavigation.css` is a stylesheet matched on `[role='tab']`, and each Load page wires its own keyboard behaviour (or does not). Orchestration should adopt Load's *visual* treatment (§8) and keep its own component; the right end state is Load adopting `TabList`, not the reverse. The UX contract's Tabs row already says this.

2. **Chart axis and grid consistency.** Orchestration sets `CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false}` and axis `stroke="var(--text-muted)"` identically at all five chart sites (`AnalyticsPage.tsx:412,462,525,539`, `EngineStrip.tsx:133`). Load's 41 Recharts files have no shared axis/grid convention — `AnalysisWorkspace.tsx:246` sets `tick={{ fontSize: 11 }}` and no grid at all, `ArchivedNativePanel.tsx:56` sets neither. Converge the *palette* (§13), not the wiring.

3. **`--accent` and the contrast layer.** Orchestration's `theme/accessibility.css` lightens `--accent` per theme (dark `#8b5cf6` → `#a580f8`, monokai `#ff6188` → `#ff97b0`) so accent-coloured *text* clears 4.5:1, and `scripts/check_dashboard_contrast.py` verifies 43 pairs in all six themes (`docs/dashboard-usability-remediation.md:23`). Load solves the same problem without desaturating the brand: it keeps `--accent: #8b5cf6` as the **fill** and adds a separate text-safe `--accent-text: #a78bfa` (`index.css:75`), used 166 times. Load's mechanism is better — brand fills stay vivid, text stays legible. **Adopt `--accent-text`, then revert `--accent` to Load's values; do not revert `--accent` without `--accent-text` first,** or 160-odd accent-coloured labels drop below 4.5:1. The same applies to `--danger`/`--danger-fill` and `--warning`/`--warning-fill-text`. Sequence matters here more than anywhere else in this document.

4. **Externalised shell dimensions.** Covered in (c)21 — Orchestration's `--sidebar-width` / `--topbar-min-height` are an improvement on Load's hard-coded `240px` / `56px`.

Two further notes that are not Load-is-weaker but are Load-is-also-imperfect, so convergence should not be framed as "Load is clean": Load carries 1,985 literal `font-size` declarations against 62 token uses (§4), and 20+ undeclared `var()` references (§15). Orchestration should adopt Load's *mechanisms* (base inheritance, the shared label rule, `--label-case`), not Load's literal-heavy component CSS.

---

## What I could not verify, and why

- **A populated chart, a run-results screen and a live-monitoring screen in Load.** The controller runs, but rendering real series needs an executed test, which needs a generator process and a reachable target. Out of the 20-minute time box. The chart findings in §13 come from source (`index.css` per-theme `--chart-N` blocks, 90 `var(--chart-N)` call sites) and are exact for palette, but I did not see two Load charts side by side. `load-01-shell-dashboard.jpg` shows the dashboard's zero-state metric tiles only.
- **A Load modal.** Three attempts (`+ New Release`, `Create script`, row `More → Delete`) all resolved to inline rows or full-page routes rather than a dialog, and I stopped rather than spend more of the box. §7's Load side is read from `components/Modal.css` (75 lines, read in full), `components/ux/ux.css:2-35,62-64` and the six `var(--modal-backdrop)` consumers — which is more precise than a screenshot would have been, but it is source, not pixels.
- **The committed `apexload.db`.** Unusable: `schema migration 24 identity mismatch: found "object-activity-and-archives"/b95038…, want "native-interval-evidence"/6c0d32…`. The committed database predates the current migration set. I ran against a fresh database in the scratchpad instead and seeded it over `/api/v1`. No repository file was modified.
- **Rendered contrast ratios.** Every colour claim is a token value read from source. I did not run `scripts/check_dashboard_contrast.py` against the proposed §20 block — §1 and "Where Load is weaker" #3 both depend on that check passing, and it must be run before any of this lands.
- **Responsive reflow, 200% zoom, reduced motion, screen readers, forced colors.** Not exercised. §19's forced-colors recommendation is read from `index.css:581-588`, not tested in Windows High Contrast.
- **`apex-light` against Load.** It has no Load counterpart by design, so there is nothing to diff. Its 138 lines were read but not compared.
- **Whether ATT Aleck Sans is licensed for Orchestration.** §14 leaves the decision open for that reason.
- **The 2026-09-09 screenshots** (`08-load-library.png`, `08b-load-current-library.png`, `08c-load-preferences.png`, `comparison-load-parity.png`) were not used. They predate Load's October CSS changes — the light theme in them is the pre-AT&T palette, which is precisely what this report says Orchestration still carries, so citing them would have been circular. Everything in this document is from current source plus the eight screenshots captured today.

**Regressions against prior QA:** one. `docs/qa/ux-audit-2026-09-09/REPORT.md:107` recorded "Orchestration's square panels versus Load's rounded cards, differing button/tab shapes and density" as an open observation and `IMPLEMENTATION.md:10` recorded U04/U05 as addressed by "shared focus/control sizes" and versioned tokens. The radius gap (§11, `--radius` 12px vs 16px), the tab-shape gap (§8, eight treatments) and the focus-ring gap (§19, two competing declarations with `ux.css` winning) are all still open, and §19's duplicate ring was *introduced* by the `ux.css` interaction layer that U04 added. Nothing else from `docs/qa/*`, `docs/dashboard-usability-remediation.md` or `design-qa.md` is re-reported here.

---

## 20. The token block to adopt

New file `apps/dashboard/src/theme/load-parity.css`, imported from `theme/index.css` **after** `tokens.css`, `themes.css` and `apex-light.css` and **before** `accessibility.css`, so the contrast layer still has the last word while the review of "Where Load is weaker" #3 is pending.

All values are Load's current `index.css` verbatim. This block adds the 34 Load-only tokens; it does **not** replace the shared-token values that differ (§1's light-theme swap and §13's chart swap are separate edits to `themes.css`).

```css
/*
 * load-parity.css — the 34 custom properties APEX Load declares and the
 * Orchestration theme layer does not.
 *
 * Source: Project_Stormrunner/dashboard/src/index.css (2026-10-13) and
 * typography.css. Values verbatim. See
 * docs/qa/ux-holistic-2026-10-17/LOAD_CONVERGENCE.md.
 *
 * Import order: tokens.css → themes.css → apex-light.css → THIS →
 * accessibility.css → primitives.css → typography.css → ux.css.
 */

:root {
  /* Type scale — Load typography.css:10-17. Six steps, no half pixels. */
  --font-size-xs: 11px;
  --font-size-sm: 12px;
  --font-size-base: 13px;
  --font-size-md: 14px;
  --font-size-lg: 15px;
  --font-size-title: 20px;

  /* Label treatment — Load index.css:58-59. Themes never change these:
     setting --label-case to `none` is what makes the product sentence case. */
  --label-case: none;
  --label-tracking: normal;

  /* Compatibility aliases — Load index.css:47-55. */
  --bg-input: var(--bg-primary);
  --bg-active: var(--accent-soft);
  --bg-tertiary: var(--bg-elevated);
  --card-bg: var(--bg-card);
  --primary: var(--accent);
  --text: var(--text-primary);
  --warning-soft: var(--warning-bg);
  --accent-transparent: color-mix(in srgb, var(--accent) 30%, transparent);
  --radius-sm: 8px;

  /* Select and menu surfaces — Load index.css:105-109. Required by the
     global select treatment (index.css:606-708). */
  --dropdown-menu-bg: var(--bg-secondary);
  --select-bg: var(--bg-secondary);
  --select-hover-bg: color-mix(in srgb, var(--accent) 8%, var(--bg-secondary));
  --select-menu-bg: var(--dropdown-menu-bg);
  --select-arrow: var(--text-secondary);

  /* Per-theme values below. Dark (:root) — Load index.css:43-44, 73-77,
     115-118, 97-98, 135. */
  --subtle-fill: rgba(255, 255, 255, 0.035);
  --subtle-fill-strong: rgba(255, 255, 255, 0.07);
  --accent-text: #a78bfa;
  --danger-fill: #dc2626;
  --danger-fill-text: #ffffff;
  --warning-fill-text: #111111;
  --logo-wave: #c4b5fd;
  --brand-highlight: var(--accent);
  --tab-link: var(--info-text);
  --tag-bg: var(--info-bg);
  --tag-text: var(--info-text);
  --chart-7: #a3e635;
  --chart-8: #f0abfc;
}

/* Light — Load index.css:161-164, 173-174, 186-190, 210-211, 240.
   AT&T blue highlight, link-blue tabs, cyan tags. */
:root[data-theme='light'] {
  --subtle-fill: rgba(29, 35, 41, 0.035);
  --subtle-fill-strong: rgba(29, 35, 41, 0.07);
  --accent-text: var(--accent);
  --danger-fill: #c70032;
  --danger-fill-text: #ffffff;
  --warning-fill-text: #ffffff;
  --logo-wave: #009fdb;
  --brand-highlight: #009fdb;
  --tab-link: #0072b2;
  --tag-bg: #baeefc;
  --tag-text: #1d2329;
  --chart-7: #5533ff;
  --chart-8: #6b3d00;
}

/* Solarized Dark — Load index.css:264-265, 276-280, 298-299, 324. */
:root[data-theme='solarized-dark'] {
  --subtle-fill: rgba(147, 161, 161, 0.08);
  --subtle-fill-strong: rgba(147, 161, 161, 0.15);
  --accent-text: var(--accent);
  --danger-fill: #c0261f;
  --danger-fill-text: #ffffff;
  --warning-fill-text: #111111;
  --logo-wave: #fdf6e3;
  --chart-7: #b58900;
  --chart-8: #f5a3d0;
}

/* Solarized Light — Load index.css:353-354, 366-371, 389-390, 415. */
:root[data-theme='solarized-light'] {
  --subtle-fill: rgba(88, 110, 117, 0.06);
  --subtle-fill-strong: rgba(88, 110, 117, 0.12);
  --accent-text: var(--accent);
  --danger-fill: #c0261f;
  --danger-fill-text: #ffffff;
  --warning-fill-text: #111111;
  --logo-wave: #a4b81c;
  --chart-7: #cb4b16;
  --chart-8: #0f4c81;
}

/* Monokai Dimmed — Load index.css:437-438, 450-455, 475-476, 501.
   The accent pink stays the fill; text takes a lighter tint that also
   reads on --bg-elevated. */
:root[data-theme='monokai-dimmed'] {
  --subtle-fill: rgba(248, 248, 242, 0.05);
  --subtle-fill-strong: rgba(248, 248, 242, 0.1);
  --accent-text: #ff86a4;
  --danger-fill: #c2410c;
  --danger-fill-text: #ffffff;
  --warning-fill-text: #111111;
  --logo-wave: #ffd866;
  --chart-7: #4fa86b;
  --chart-8: #fcfcfa;
}

/* apex-light (Light (CTx)) has no Load counterpart — it is a documented
   domain exception (APEX_UX_CONTRACT.md, Domain exceptions). Values are
   derived from its own palette, not ported. */
:root[data-theme='apex-light'] {
  --subtle-fill: #f8fbfd;                        /* already at apex-light.css:52 */
  --subtle-fill-strong: rgba(26, 26, 46, 0.07);
  --accent-text: var(--accent);
  --danger-fill: #ca313f;
  --danger-fill-text: #ffffff;
  --warning-fill-text: #ffffff;
  --logo-wave: #00a8e0;
  --brand-highlight: #00a8e0;
  --tab-link: #0075a5;
  --tag-bg: #e5f6fc;
  --tag-text: #1a1a2e;
  --chart-7: #5533ff;
  --chart-8: #6b3d00;
}
```

After adding this file, run `uv run python scripts/check_dashboard_contrast.py` and extend its pair list to cover `--accent-text` on `--bg-primary`/`--bg-card`/`--bg-elevated`, `--danger-fill-text` on `--danger-fill`, `--warning-fill-text` on `--warning`, and `--tag-text` on `--tag-bg`, in all six themes. The `apex-light` block above is the only part of this file that is not verbatim Load and the only part that has not been contrast-checked anywhere.
