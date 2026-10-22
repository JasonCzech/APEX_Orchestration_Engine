# Accessibility review — APEX Orchestration dashboard

Holistic UX review, 2026-10-17 · accessibility workstream
Environment: **connected** (`http://127.0.0.1:4175/`, production build, same-origin `/v1` proxy,
admin key) unless stated. Demo (`:4173`) used only where connected mode has no data (charts).
Tooling: axe-core 4.10.2 served from the app bundle (`/axe.min.js`), injected same-origin;
`scripts/check_dashboard_contrast.py`; `getBoundingClientRect()` for every size claim;
a DOM focus/keystroke recorder for the keyboard walkthroughs.

Severity: **P0** blocks a core task for an affected user · **P1** breaks a core task or fails a
Level A/AA criterion on a primary surface · **P2** fails a criterion on a secondary surface or
degrades a core task · **P3** advisory, measured, worth scheduling.

---

## 1. Summary

| | |
|---|---|
| Routes run through axe | **33 of 36** (3 unreachable — see §9) |
| Themes per route | 6 + `system` preference (both resolutions) |
| Distinct axe rules violated | **5** |
| Total axe violation nodes | 140 — `color-contrast` 129, `landmark-complementary-is-top-level` 4, `heading-order` 3, `aria-input-field-name` 2, `scrollable-region-focusable` 2 |
| …of which one root cause | **99** (`.dash-badge`: 33 routes × 3 dark themes); the other 30 contrast nodes are CodeMirror chrome |
| New findings | 17 (A11Y-01 … A11Y-17) |
| Corrections to earlier claims | 3 (§8) |
| Verified passes worth recording | 7 (§7) |

The single highest-impact defect is not an axe finding: **the prompt gate traps Tab inside the
CodeMirror editor and the trapped keystrokes silently edit the prompt the approver is about to
execute** (A11Y-04). The second is that **none of the three events this product exists to signal
— a gate opening, a phase changing state, a live stream dropping — is announced** (A11Y-06), and
the component that was supposed to carry the gate announcement (`RunRail`) is dead code.

---

## 2. Method and its limits

**axe.** For each route: navigate via `history.pushState` + `popstate` (React Router picks this
up), wait for `[aria-busy]` to clear, then run axe once per theme. Full ruleset on `apex-light`
(`color-contrast-enhanced` disabled); `color-contrast` + `link-in-text-block` on the other five.
Themes only redefine CSS custom properties — no DOM differs between them — so structural rules
cannot vary by theme, and running them six times would only repeat the same nodes. Viewport
1440×900 for the sweep.

**A correction to my own first pass.** My first sweep produced ~16 bogus `color-contrast` nodes
per route (e.g. apex-light's `#5a6578` reported against dark's `#0b0c10`). That was a measurement
artifact: the Browser pane was backgrounded, Chromium throttles style recalc for non-painting
pages, and `transition: background .12s, color .12s` on `.nav-item` left stale computed colours
for up to 3 s after `data-theme` changed. I re-ran the whole sweep after injecting
`*{transition:none!important;animation:none!important}` and gating each axe run on the computed
`.nav-item` colour actually matching the theme's `--text-secondary`. **Only the second sweep is
reported here.** Anyone reproducing this must disable transitions first or they will get the same
false positives.

**What axe cannot see**, and was therefore measured separately: chart series colours, badge and
chip variants, disabled states, focus-ring contrast (§4), keyboard traps and focus order (§5),
target sizes (§6), and live-region behaviour (§3).

---

## 3. Screen-reader semantics for the three events that matter

Verified by inspecting the live DOM (`[aria-live]`, `role=status|alert|log`), by a
`MutationObserver` over every live region during real interaction, and by reading the source.
There are exactly **three** `aria-live` attributes in the whole dashboard
(`ApexLoadWorkspacePage.tsx:877`, `RunDetailPage.tsx:216`, `NewRunWizard.tsx:537`) plus a
scattering of `role="status"`/`role="alert"` on loading and error paragraphs.

| Event | Announced? | Evidence |
|---|---|---|
| **A gate opens** | **Partially — and only by accident.** The only live region on a run page is the thread-status badge (`RunDetailPage.tsx:216`, `role="status" aria-live="polite"`), whose text flips `Running` → `Awaiting approval`. It names neither the phase nor the gate kind. On `/approvals` it never changes (every row is already "Awaiting approval"), so there it is silent. | §3.1 |
| **A phase changes state** | **Silent.** `PhaseStrip` is `role="group" aria-label="Phase progress"` with no live region; status lives in each button's `aria-label`, and changing an `aria-label` on an unfocused button is not announced. `orchestrationStatusLabel()` maps `busy` → `"Running"` for every phase, so the one live region's text does not change as the pipeline advances. | §3.2 |
| **Live-stream status changes** | **Silent, deliberately.** `LiveStatusChip` carries no `role` and no `aria-live` (measured: both `null`); only `data-status` and the visible text change. The source comment says this is "Deliberately NOT `role="status"`: the run rail's gate banner owns that landmark on this page". | §3.3 |

### 3.1 The gate banner that owns the announcement is never rendered — A11Y-06

`RunRail.tsx:109` and `:130` render `<div className="gate-banner" role="status">` ("Gate open: …
on …" and "Gate opening: …"). A repository-wide search finds `RunRail` referenced **only** by its
own file, its unit test (`__tests__/RunRail.test.tsx`), its CSS and `ROUTES.md`. No route, page or
layout imports it. I confirmed at runtime: `document.querySelector('.gate-banner')` is `false` on
a run that has an open gate (`/runs/01a14ad7-4a0e-…`, `pending_gate.kind = phase_review`), on its
phase route, and on `/approvals`.

So the justification written into `LiveStatusChip.tsx` for not being a live region rests on a
component that does not ship, and **no live region anywhere announces that a gate has opened with
its phase or kind.**

### 3.2 and 3.3 measured values

On `/runs/01a14ad7-4a0e-7f03-bd22-70df491f2e40/phases/story_analysis`:

- live regions present: exactly one — `span.status-badge.warning[role=status][aria-live=polite]`, text `"Awaiting approval"`.
- `.phase-strip`: `role="group"`, `aria-label="Phase progress"`, `aria-live` **null**, 7 `<button>` segments.
- `.live-status-chip`: `role` **null**, `aria-live` **null**, `data-status="idle"`, text `"Snapshot refresh active"`.

Driving five gate changes with `j` on `/approvals` produced **zero** live-region mutations
(`MutationObserver` count 0) across five full workspace swaps and five URL changes.

---

## 4. Contrast

### 4.1 `scripts/check_dashboard_contrast.py` — run from the repo root

```
$ uv run python scripts/check_dashboard_contrast.py
dark: minimum 4.63:1 across 43 shared token pairs
light: minimum 4.59:1 across 43 shared token pairs
apex-light: minimum 4.63:1 across 43 shared token pairs
solarized-dark: minimum 5.12:1 across 43 shared token pairs
solarized-light: minimum 4.61:1 across 43 shared token pairs
monokai-dimmed: minimum 4.68:1 across 43 shared token pairs
```

Exit 0. All six themes pass.

**What those 43 pairs are** (read from the script): `--text-primary`, `--text-secondary`,
`--text-muted` and `--accent` against five backgrounds (20); the four tonal pairs
`--{success,warning,danger,info}-text` on `--{…}-bg` against the same five backgrounds (20);
`--accent-contrast` on `--accent` and on `--accent-hover` (2); `--danger-contrast` on `--danger`
(1). It is reproducible evidence **for shared colour tokens only** — the script's own docstring
says so: "not a substitute for testing rendered focus, overlays, charts, and typography."

**It does not cover** chart series colours, any badge or chip that does not use the
`--{tone}-text` / `--{tone}-bg` pair, the disabled state, placeholder text, or focus rings. Those
are below, computed with the same WCAG formulas over the same token files
(script kept at `scratchpad/extra_contrast.py`; 340 additional pairs).

### 4.2 Chart series colours — **not covered, and failing** (A11Y-03)

Series vs. the surface they are drawn on (needs 3:1, SC 1.4.11):

| Theme | min ratio | worst pairs |
|---|---|---|
| apex-light | **2.32** | `--chart-3` 2.32 / `--chart-1` 2.55 / `--chart-2` 2.80 on `--bg-primary` |
| solarized-light | **2.67** | `--chart-2` 2.67, `--chart-1` 2.67 on `--bg-card` |
| solarized-dark | **2.87** | `--chart-4` 2.87, `--chart-5` 2.92, `--chart-6` 2.97 on `--bg-card` |
| light | 3.04 | `--chart-3` on `--bg-primary` (marginal) |
| dark | 3.89 | pass |
| monokai-dimmed | 3.81 | pass |

Series vs. **each other** — the thing that decides whether two lines can be told apart:

| Theme | worst pair | ratio |
|---|---|---|
| solarized-light | `--chart-1` vs `--chart-2` (`#b58900` vs `#859900`) | **1.00** |
| solarized-dark | `--chart-2` vs `--chart-3` | **1.00** |
| monokai-dimmed | `--chart-2` vs `--chart-5` | **1.00** |
| dark | `--chart-4` vs `--chart-5` | 1.02 |
| light | `--chart-4` vs `--chart-6` | 1.05 |
| apex-light | `--chart-1` vs `--chart-2` | 1.10 |

Across all six themes **no pair of series reaches 3:1**; the best anywhere is 2.62:1. Area fills
are worse again: `AnalyticsPage.tsx` draws areas at `fillOpacity` 0.12–0.16, giving 1.05–1.44:1
against the card. `AnalyticsPage.tsx:529-543` sets `stroke` and `fill` only — no
`strokeDasharray`, no marker shapes — so colour is the sole encoding (SC 1.4.1).

Screenshot `a11y-04-chart-series-solarized-light.jpg` (demo mode, "Tokens over time by model"):
three series at `#b58900`, `#859900`, `#cb4b16`; the first two are a single visual band.

### 4.3 Badges and chips — **not covered, and failing** (A11Y-01)

`primitives.css:190` — `.dash-badge { background: var(--accent); color: white; }`. Hardcoded
`white`, not `var(--accent-contrast)`:

| | apex-light | dark | light | solarized-dark | solarized-light | monokai-dimmed |
|---|---|---|---|---|---|---|
| `white` on `--accent` (shipped) | 5.15 | **2.98** | 6.29 | **2.19** | 6.24 | **2.04** |
| `--accent-contrast` on `--accent` (the fix) | 5.15 | 7.04 | 6.29 | 9.61 | 6.24 | 10.31 |

This is the sidebar "tests need review" badge, present on **every route**, and it is the single
axe `color-contrast` node that appears on all 33 routes in the three dark themes. Switching the
declaration to `var(--accent-contrast)` clears it in every theme without touching any token.

`primitives.css` has four `color: white` declarations. Two are rescued because
`theme/accessibility.css` is imported after `primitives.css` and re-declares them at equal
specificity — `.btn-primary` (line 93 → `--accent-contrast`) and `.btn-danger` (line 145 →
`--danger-contrast`). Two are not: `.dash-badge` (line 190, shipped, failing above) and
`.btn-success` (line 162), where white on `--success` is **2.54** (dark), **1.85**
(solarized-dark) and **1.59** (monokai-dimmed), plus a hardcoded `background: #16a34a` on hover
that is 3.30:1 with white in every theme. `.btn-success` has no call site in `.tsx` today, so it
is latent — but it is the same root cause and will fail the moment someone uses it.

Other uncovered badge pairs, all passing (minimum across the six themes, worst theme in
brackets): `.status-badge.accent` — `--accent` on composited `--accent-soft`, **4.64**
(apex-light); `.topbar-meta-chip.info`/`.accent` — `--accent-hover` on `--accent-soft`, **4.63**
(apex-light); `.status-badge.neutral` — `--text-secondary` on `rgba(107,112,128,.15)`, **4.87**
(apex-light); `.live-status-chip.live` — `--success` (not `--success-text`) on `--success-bg`,
**4.80** (solarized-light). Two run-calendar chips fail but are not reachable in this build:
`--run-calendar-cron-text` on its fill is 2.82:1 in apex-light and 3.17:1 in solarized-dark, and
`--run-calendar-ad-hoc-text` is 2.65:1 in solarized-dark (A11Y-17, P3).

### 4.4 Disabled state — **not covered** (A11Y-16)

`ux.css:17` — `.btn:disabled { opacity: 0.45 }`; `primitives.css:468` —
`.field-select:disabled, .field-input:disabled { opacity: 0.65 }`. Opacity composites the whole
element, so the label-against-its-own-fill ratio becomes:

| Control | apex-light | dark | light | solarized-dark | solarized-light | monokai-dimmed |
|---|---|---|---|---|---|---|
| `.btn-primary:disabled` | 1.96 | 2.28 | 2.10 | 3.17 | 2.07 | 3.17 |
| `.btn-danger:disabled` | 2.07 | 2.13 | 2.20 | 2.82 | 2.30 | 3.31 |
| `.btn-secondary:disabled` | 2.00 | 2.40 | 2.15 | 2.44 | 1.95 | 2.73 |
| `.btn-ghost:disabled` | 1.96 | 2.41 | 2.12 | 2.39 | 2.49 | 3.08 |
| `.field-input:disabled` (value) | 4.96 | 8.10 | 5.26 | 5.99 | 4.51 | 6.76 |

SC 1.4.3 exempts "inactive user interface components", so **this is not a WCAG failure**. It is
still a legibility problem: the wizard's `Next` and the gate's `Execute Phase` spend real time
disabled, and at ~2:1 a low-vision user cannot read what the blocked action is.

### 4.5 Placeholder text — **covered after all**

`primitives.css:457` — `.field-input::placeholder { color: var(--text-muted) }` on
`background: var(--bg-secondary)` (`--bg-elevated` under apex-light). Both are in the script's
43 pairs. Computed independently: 4.63 / 5.49 / 4.65 / 5.38 / 4.67 / 6.36 — **all pass**. No
other `::placeholder` rule exists in the codebase, so any input that is not `.field-input` falls
back to the UA placeholder colour; I found no such input in the routes I reached.

### 4.6 Focus rings — **not covered, and passing**

Two rules apply. `ux.css:36-43` — `button|a|select|input|textarea|summary:focus-visible { outline:
2px solid var(--accent); outline-offset: 3px }` (specificity 0,1,1) wins over
`primitives.css:370` — `:where(button, a, [role='button'], …):focus-visible { outline: 2px solid
var(--focus-ring); outline-offset: 2px }` (specificity 0,1,0). `accessibility.css` sets
`--focus-ring` equal to `--accent` in every theme, so both paths draw the same colour.

Ring vs. surface (needs 3:1, SC 1.4.11):

| Theme | bg-primary | bg-secondary | bg-card | bg-elevated | accent-soft panel |
|---|---|---|---|---|---|
| apex-light | 4.80 | 4.63 | 5.15 | 5.15 | 4.64 |
| dark | 6.83 | 6.56 | 6.51 | 5.53 | 5.65 |
| light | 6.01 | 5.74 | 6.25 | 6.29 | 5.55 |
| solarized-dark | 6.87 | 5.95 | 6.08 | 5.95 | 5.36 |
| solarized-light | 5.79 | 5.09 | 5.19 | 6.24 | 4.71 |
| monokai-dimmed | 8.13 | 7.29 | 7.42 | 5.37 | 6.28 |

Minimum 4.63:1. The 3 px offset keeps the ring on the container background rather than on the
control, so the `--accent`-on-`--accent` adjacency a primary button would otherwise create does
not occur. **Pass, with margin.**

### 4.7 CodeMirror surfaces — **not covered, and failing** (A11Y-02)

`components/viewers/viewers.css:28` — `.code-viewer .cm-gutters { color: var(--text-muted) }` over
`background: var(--code-bg)`. `--text-muted` is tuned against page backgrounds, but `--code-bg` is
a near-black `#1a1a2e` even in the light APEX theme:

| pair | apex-light | dark | light | solarized-dark | solarized-light | monokai-dimmed |
|---|---|---|---|---|---|---|
| `--text-muted` on `--code-bg` (gutter line numbers) | **3.31** | 5.56 | 4.65 | 5.38 | 4.67 | 6.36 |
| `--code-text` on `--code-bg` (editor body) | 13.84 | 16.04 | 13.35 | 4.86 | **4.39** | 13.94 |

Both reproduce in axe: 3.30:1 for `.cm-gutterElement` in apex-light, 4.39:1 for `.cm-line` ×9 in
solarized-light. Screenshot `a11y-05-codemirror-gutter-apex-light.jpg`.

---

## 5. Keyboard-only walkthroughs

No pointer was used in any of these. Focus was reset to `document.body` (the post-load state), keys
were delivered as real key events, and every `keydown` and `focusin` was recorded, so the counts
below are measured, not estimated.

### 5.1 (a) `/runs/new` → Review step, without launching — **completed, 67 keystrokes**

| Step | Tab stops from the step tab to the step's primary button | keystrokes |
|---|---|---|
| Scope (from a cold load, focus on `<body>`) | skip link → 16 nav items → account → collapse sidebar → topbar link → Scope tab → 5 fields → Save Draft → **Next** | 28 Tab + 1 Enter |
| Config | engine radio → Retry test presets → 7 phase toggles → "Review every phase" → Save Draft → Back → **Next** | 13 Tab + 1 Enter |
| Work Items | NL query → saved-queries link → manual key field → Save Draft → Back → **Next** | 6 Tab + 1 Enter |
| Context | Choose files → Save Draft → Back → **Next** | 4 Tab + 1 Enter |
| Prompts | 7 phase toggles → Override for this run → Save Draft → Back → **Review Test** | 11 Tab + 1 Enter |
| **Total to land on `?step=review`** | | **67** |

The Review step then shows the validation issues ("Scope: Title is required", "Scope: Request is
required") as focusable buttons; nothing was launched.

Using the skip link (Tab, Enter) instead of tabbing the shell brings the first step down from 28
to 11, giving **50 keystrokes** overall. The skip link is the first tab stop on every route,
`AppShell.css:95-96` reveals it on focus (`transform: translateY(-200%)` → `none`), and
`#main-content` has `tabIndex={-1}` so focus lands correctly. It works.

The wizard's `role="tablist"` (`NewRunWizard.tsx:500-521`) is one of the correct ones: roving
`tabIndex`, `aria-controls`, and `moveTab()` handling Left/Right/Home/End.

**Defect found — A11Y-08, asynchronous focus steal.** Pressing `Next` leaves focus on the `Next`
button and *then*, some hundreds of milliseconds later, moves it to the new step's tab button.
Recorded trail for `?step=context` → `?step=prompts`:

```
K1:Enter from button.btn.btn-primary «Next»
K2:Tab   from button.btn.btn-primary «Next»      → a.skip-link «Skip to content»
K3:Tab   from a.skip-link                        → a.nav-item.active «New Test»
         → focus button.wizard-tab[role=tab] «Prompts»     ← no keystroke caused this
```

The user had already tabbed two stops into the sidebar when the wizard yanked focus back into the
panel. Source: `NewRunWizard.tsx:219-225` (`pendingFocusRef` effect) and `:467`
(`afterNextPaint(... .focus())`).

### 5.2 (b) Prompt gate from the queue → decision control — **a trap, A11Y-04 (P0)**

From `/approvals` with the queue selection on a `prompt_review` gate:

```
K1 Enter  → div.gate-module-root «Review prompts — Story Analysis»
K2 Tab    → summary «Technical details»
K3 Tab    → summary «Shared platform instructions (read-only)»
K4 Tab    → button.prompt-review-tab «System Prompt»
K5 Tab    → button.prompt-review-tab «Phase Prompt»
K6 Tab    → button.prompt-review-tab «Additional Context»
K7 Tab    → div.cm-content[role=textbox]
K8..K14 Tab  — no focus change. Seven keystrokes consumed by the editor.
```

The decision controls (`▶ Execute Phase`, `Skip Phase`, `Abort`) sit **after** the editor in the
tab order and cannot be reached by Tab.

**Worse than a trap: the trapped keystrokes edit the prompt.** After those eight Tab presses the
editor content began `"                You are the APEX story_analysis agent…"` — sixteen spaces
inserted. The gate flipped to the **`edited`** state and its primary action changed from
`▶ Execute Phase` to `Save Edit & Re-review`. A keyboard user who simply tries to Tab to the
Approve button has, without any visible warning, modified the prompt that will be sent to the
model and changed which decision the gate offers.
Screenshot `a11y-03-prompt-gate-tab-trap-edited.jpg`.

There **is** an exit — CodeMirror's `Escape` then `Tab` — but nothing anywhere tells the user:

```
K8 Escape, K9 Tab → button.btn.btn-primary «▶ Execute Phase»
```

SC 2.1.2 is met only if the exit method is advertised; it is not. Clean path, knowing the trick:
**Enter + 6 Tab + Escape + Tab = 9 keystrokes** to `▶ Execute Phase`.

Note: only 3 of the 4 `prompt-review-tab` buttons ever receive focus ("Application Prompt" is
present in the DOM but skipped in this gate's state).

### 5.3 (c) Output gate → decision control — **clean, 5 keystrokes**

From `/approvals` with the selection on a `phase_review` gate:

```
K1 Enter → div.gate-module-root «Review phase results — Story Analysis»
K2 Tab   → summary «Technical details»
K3 Tab   → textarea.gate-composer-input
K4 Tab   → summary «Technical output»
K5 Tab   → button.btn.btn-primary «Accept phase output & continue»
K6 → «Request changes»   K7 → «Abort»
K8 → button.workspace-tab «Phase Details»   K9 → div[role=tabpanel]   K10, K11 → summaries
K12 → wraps to a.skip-link
```

No trap — the composer is a plain `<textarea>`, which does not capture Tab. The trap in §5.2 is
specific to the surfaces that mount CodeMirror (prompt gate, phase workspace prompt editor,
prompt catalog editor).

### 5.4 (d) Five or more items in the approvals queue — **completed, but keystrokes are dropped (A11Y-05)**

Paced at ~1.2 s between presses, five `j` presses walked five distinct gates, one row each, and
`k` walked back correctly. At normal typing speed it collapses:

| trial | presses | `keydown` events the handler received | rows advanced |
|---|---|---|---|
| rapid (batch, no delay) | 5 × `j` | 5 | **2** |
| rapid (batch, no delay) | 6 × `j` | 6 | **1** |
| paced ~1.2 s | 5 × `j` | 5 | 5 |

Root cause is visible in the source: `ApprovalsInboxPage.tsx:227-236`, `moveSelection()` reads
`stateRef.current.selection`, and `stateRef.current` is only refreshed during render
(`:154-155`). Each `j` writes the selection through the router, so until React commits, every
further `j` recomputes the *same* `index + delta`. The faster you triage, the more presses vanish.

Also measured during (d): **zero** live-region mutations across five gate changes, and the queue
`<ul role="listbox" aria-label="Needs review queue">` carries `aria-activedescendant` pointing at
the selected row while having **no `tabindex`**, so it can never be focused and the
`aria-activedescendant` is inert; meanwhile each of the 14 `role="option"` rows has
`tabIndex="0"`, which is the opposite of the listbox pattern and puts 14 extra stops in the Tab
order (A11Y-09).

---

## 6. Viewports, zoom and target size

### 6.1 Reflow — **pass everywhere tested**

`document.scrollingElement.scrollWidth > clientWidth` and the same for `<main>`, measured on
`/`, `/runs`, `/runs/:id`, `/runs/new`, `/approvals`, `/analytics`, `/evidence`, `/logs`,
`/settings`, `/cycles`, `/apex-load`, `/admin/connections`, `/runs/compare`:

| width | horizontal page scroll | notes |
|---|---|---|
| 1440 | none | — |
| 768 | none | — |
| 720×450 (≈1440 at 200%) | none | sidebar collapses to "Open navigation"; sticky topbar 66 px of 450 (14.7%) |
| 390 | none | run list becomes stacked cards |
| 320 | none | wide tables scroll inside `.data-table-wrap` (699–900 px), which SC 1.4.10 permits |

Screenshot `a11y-06-reflow-720x450-200pct.jpg`. No clipped controls and no overlapping sticky
headers were found at any of these widths; the `/approvals` two-column grid collapses correctly
to a single 328 px column at 390 (`grid-template-columns` measured as `328px`).

### 6.2 Target size (SC 2.5.8) — **no failures; several controls pass only by exception** (A11Y-15)

I implemented the actual criterion rather than eyeballing: for every undersized target, a 24 px
**diameter** circle centred on its bounding box must not intersect another target's box, nor the
circle of another undersized target (centres ≥ 24 px apart). Elements inside a closed `<details>`
were excluded — Chromium keeps stale rects for them, which produced false positives on my first
pass.

| Route | viewport | targets | undersized | **2.5.8 failures** |
|---|---|---|---|---|
| `/`, `/runs`, `/approvals`, `/analytics`, `/logs`, `/settings` | 1440 | 18–61 | 0–28 | **0** |
| `/`, `/runs`, `/approvals`, `/analytics`, `/logs`, `/settings`, `/runs/new` | 390 | 19–69 | 0–28 | **0** |

Measured sizes of the undersized controls:

| Control | size (CSS px) | nearest other target | verdict |
|---|---|---|---|
| `.overflow-menu-trigger` (run row "⋯") | **28.3 × 20.0** | 22.1 px from centre to "Open run" | passes (needs > 12) |
| `.overflow-menu-trigger` (run detail "▾") | 22.3 × 32.0 | — | passes |
| `.engine-metric-tab` | **23.00 × 42.7–66.5** | siblings 57.2 px centre-to-centre (2 px gap) | passes |
| `.level-chip` / `.analytics-chip` | 38.5–142.3 × **23.0** | — | passes |
| `.analytics-sort` (column sort) | 24.0–94.0 × **15.0** | — | passes |
| `.runs-run-title.runs-run-button` (≤768) | 120–286 × **15.6** | — | passes |
| `.home-recent-link` / `.home-view-all` | 46–297 × **16–18** | — | passes |
| Settings theme radio `<input>` | 13 × 13 | — | the real target is its `<label class="settings-theme-card">`, **141.4 × 157** — passes |

**Correcting the earlier estimate:** `.engine-metric-tab` is **23.00 px** tall, not ~22. Measured
with `getBoundingClientRect()` on the real markup and CSS (`live.css:256-268`: `padding: 3px 10px`,
`font-size: 10.5px`, `line-height: normal` from the UA button style, `border: 1px`). It misses the
24 px minimum by one pixel and survives only on the spacing exception — one extra pixel of
vertical padding would put it over the line outright.

**Pre-empting a different false positive:** `.phase-seg` is 18 × 8 px (`PhaseStrip.css`) and
becomes a `<button>` when `onSelect` is passed — but `PhaseStrip` has exactly one call site
(`RunDetailPage.tsx:344`) and it uses `size="lg"`, whose `.phase-step` buttons measure 167 × 34.
The 18 × 8 buttons are never rendered. Not a finding.

---

## 7. Verified passes worth recording

1. `scripts/check_dashboard_contrast.py` — 43 shared pairs × 6 themes, min 4.59:1, exit 0 (§4.1).
2. Focus-ring contrast — min 4.63:1 across all themes and surfaces (§4.6).
3. Placeholder text — min 4.63:1; `--text-muted` is in the script's coverage (§4.5).
4. Reduced motion — both paths work. `accessibility.css` has `@media (prefers-reduced-motion:
   reduce)`, and `ux.css:188-190` keys off `:root[data-reduce-motion='true']`, which
   `useTheme.ts:75` sets from preferences. Measured with the attribute on: `animation-name: none`,
   `animation-duration: 0s`, `transition-duration: 0s`.
5. `components/Dialog.tsx` — correct modal: focus trap with wrap, `Escape` to close, `inert` +
   `aria-hidden` on every other body child (with a `MutationObserver` for late-added nodes),
   `role="dialog" aria-modal="true"`, and focus restored to the opener on unmount.
6. Route-change focus management — `AppShell.tsx:30-49` focuses `<main tabIndex={-1}>` on every
   pathname change, and restores per-history-entry scroll on POP.
7. Skip link — first tab stop, visible on focus, lands on `#main-content` (§5.1).

---

## 8. Corrections to claims carried into this review

1. **"Inbox `j`/`k` … never move DOM focus."** In connected mode on `/approvals` they **do**.
   Each press rewrites the URL to `/runs/:threadId/reviews/:interruptId?source=queue`, which moves
   focus to `main.main-content` and then to `h2.run-detail-title` of the newly selected run.
   Recorded trail:
   `K1:j from h2.run-detail-title «Queue item 2 …» → focus h2.run-detail-title «Queue item 3 …»`.
   A screen reader will therefore hear the new run's title. What is still missing — and what the
   finding should say — is any announcement of **queue position or gate kind**, and focus never
   enters the queue itself, so the user cannot tell where they are in 14 rows. The "no live region
   announces it" half is confirmed (zero live-region mutations over five moves).
2. **`.engine-metric-tab` "~22 px"** — measured **23.00 px**. See §6.2.
3. **`theme/useTheme.ts` defaults to `apex-light`** — confirmed, with the blast radius measured:
   with `prefers-color-scheme: dark` emulated and no stored preference, `matchMedia(...).matches`
   is `true` while `data-theme` is `apex-light`. Once the user picks "System (Auto)" it resolves
   correctly (`system` + dark → `data-theme="dark"`; `system` + light → `"light"`), and the only
   axe violation under `system`/dark is the same `.dash-badge` node.

---

## 9. Findings

### A11Y-01 — `.dash-badge` hardcodes white instead of `--accent-contrast`
**P1 · WCAG 1.4.3 Contrast (Minimum), AA · all routes · connected + demo · dark, solarized-dark,
monokai-dimmed · all viewports**

Repro: open any route, set `data-theme` to `dark`, look at the sidebar "Tests" count badge.
Expected ≥ 4.5:1. Actual **2.98:1** (`dark`), **2.19:1** (`solarized-dark`), **2.04:1**
(`monokai-dimmed`) — white on `--accent`. This is 99 of the 129 axe violation nodes in the sweep:
one node on each of the 33 reachable routes in each of the three dark themes.

Screenshot: `a11y-01-dash-badge-dark-theme.jpg`.

Fix: `apps/dashboard/src/theme/primitives.css` line 190 — change `.dash-badge`'s `color: white;`
to `color: var(--accent-contrast);`. Every theme then passes (5.15 / 7.04 / 6.29 / 9.61 / 6.24 /
10.31). While there, give `.btn-success` (line 162) a `--success-contrast` token in
`theme/accessibility.css` and drop the hardcoded `#16a34a` hover, so the latent occurrence cannot
ship. The remaining two `color: white` declarations are already overridden by
`accessibility.css` and need no change.

### A11Y-02 — CodeMirror chrome is coloured with page tokens over `--code-bg`
**P2 · WCAG 1.4.3 · `/prompts`, `/prompts/:ns/:name`, `/prompts/:ns/:name/versions/:v`,
`/runs/:threadId/phases/:phase`, `/runs/:threadId/reviews/:interruptId`, golden-config and log
viewers · connected · apex-light, solarized-light (and solarized-dark for `.cm-activeLine`) ·
all viewports**

Repro: `/prompts/phase/env_triage%2Fsystem` in `apex-light`; read the line numbers.
Expected ≥ 4.5:1. Actual **3.30:1** (`#666e79` on `#1a1a2e`). In `solarized-light` the editor body
text itself is **4.39:1** (`--code-text #586e75` on `--code-bg #eee8d5`) over nine `.cm-line`
nodes.

Screenshot: `a11y-05-codemirror-gutter-apex-light.jpg`.

Fix: `apps/dashboard/src/components/viewers/viewers.css:28` — stop using `var(--text-muted)` for
`.cm-gutters`; introduce a `--code-text-muted` token derived from `--code-bg` (e.g. `#9aa3b2` for
apex-light's dark code surface) in `theme/accessibility.css`. Separately raise
`solarized-light`'s `--code-text` in `theme/themes.css` from `#586e75` to at least `#4a5a60`
(4.39 → ≥ 4.5).

### A11Y-03 — chart series are encoded by colour alone, and the colours do not separate
**P1 · WCAG 1.4.1 Use of Color (A) + 1.4.11 Non-text Contrast (AA) · `/analytics`, EngineStrip on
run detail · demo (connected has one series) · all six themes · all viewports**

Repro: `/analytics` in demo mode, "Tokens over time by model", theme `solarized-light`. Three
series render `#b58900`, `#859900`, `#cb4b16`; the first two have **identical luminance (1.00:1)**
and read as one line. Expected: series distinguishable without colour, and ≥ 3:1 against the
plot surface. Actual: no series pair reaches 3:1 in any theme (best 2.62:1), and series-vs-surface
drops to 2.32:1 (apex-light `--chart-3`), 2.67:1 (solarized-light `--chart-1`/`-2`), 2.87:1
(solarized-dark `--chart-4`). Area fills at `fillOpacity` 0.12–0.16 land at 1.05–1.44:1.

Screenshot: `a11y-04-chart-series-solarized-light.jpg`.

Fix, two parts. (a) `apps/dashboard/src/features/analytics/AnalyticsPage.tsx:52-57` and
`:529-543` — add a non-colour channel: a `strokeDasharray` cycle per series index and `dot`
shapes on `<Line>`/`<Area>`, so series remain separable in greyscale. (b)
`apps/dashboard/src/theme/themes.css` and `apex-light.css` — re-pick `--chart-1…6` per theme for
monotone luminance spacing; the six solarized hues in particular are all mid-luminance.

### A11Y-04 — the prompt gate traps Tab in the editor, and the trapped keys edit the prompt
**P0 · WCAG 2.1.2 No Keyboard Trap (A) · `/approvals`, `/approvals/:threadId/:interruptId`,
`/runs/:threadId/reviews/:interruptId`, `/runs/:threadId/phases/:phase` (any `prompt_review` gate)
· connected · all themes · all viewports**

Repro, pointer-free: load `/approvals`, press `Enter` to enter the gate workspace of a
`prompt_review` gate, press `Tab` six times to reach `div.cm-content[role="textbox"]`, keep
pressing `Tab`.
Expected: Tab leaves the editor and reaches `▶ Execute Phase` / `Skip Phase` / `Abort`.
Actual: focus does not move. Every further Tab inserts indentation into the prompt — eight
presses produced sixteen leading spaces — the gate flips to the **`edited`** state, and the
primary action silently changes from `▶ Execute Phase` to `Save Edit & Re-review`. `Escape` then
`Tab` does escape, but nothing in the UI says so.

Screenshot: `a11y-03-prompt-gate-tab-trap-edited.jpg`.

Fix: `apps/dashboard/src/features/runs/PromptTabsEditor.tsx` — the `<CodeMirror>` element from
`@uiw/react-codemirror` is mounted without `indentWithTab`, which that wrapper defaults to
**`true`**; set `indentWithTab={false}`. CodeMirror 6 deliberately leaves Tab as a focus key for
exactly this reason, and prompt text is prose, not code, so tab-to-indent buys nothing here. If
it must stay, bind indentation to `Mod-]`/`Mod-[` and add visible help text beside the editor
("Press Esc, then Tab, to leave the editor") — advertising the exit is the minimum SC 2.1.2
requires. The same wrapper is mounted in `features/prompts/PromptEditor.tsx` and
`features/prompts/PromptDiff.tsx`; apply the same prop there.

### A11Y-05 — the approvals inbox drops `j`/`k` keystrokes at triage speed
**P1 · supports SC 2.1.1 Keyboard (operability of the documented model) · `/approvals` ·
connected · all themes · all viewports**

Repro: load `/approvals`, press `j` six times at normal typing speed.
Expected: selection advances six rows. Actual: the document handler receives all six `keydown`
events (verified) but the selection advances **one** row. A five-press trial advanced two. With
~1.2 s between presses all five land correctly.

Root cause: `apps/dashboard/src/features/approvals/ApprovalsInboxPage.tsx:227-236` —
`moveSelection()` resolves the current index from `stateRef.current.selection`, and
`stateRef.current` is only rewritten during render (`:154-155`). `setSelection()` goes through
the router, so presses arriving before React commits all compute the same next index.

Fix: hold the intended index in a ref that `moveSelection` updates **synchronously** before
calling `setSelection`, and reconcile it from `actionable` on each render — e.g. a
`pendingIndexRef` initialised from the committed selection and advanced locally, so N presses move
N rows regardless of commit timing.

### A11Y-06 — no live region announces a gate opening, a phase change, or a stream change
**P1 · WCAG 4.1.3 Status Messages (AA) · `/runs/:threadId`, `/runs/:threadId/phases/:phase`,
`/approvals` and its detail routes · connected + demo · all themes · all viewports**

Repro: open a run with a pending gate and inspect every `[aria-live]`, `role="status"`,
`role="alert"` and `role="log"` node; then drive five gate changes with `j` on `/approvals` under
a `MutationObserver`.
Expected: an announcement when a gate opens (naming the phase and kind), when a phase changes
state, and when the live stream drops or reconnects.
Actual: the only live region on a run page is the thread-status badge; `.phase-strip` has no live
region and `orchestrationStatusLabel()` returns `"Running"` for every phase, so the badge's text
does not change as phases advance; `LiveStatusChip` carries neither `role` nor `aria-live`; and
`RunRail` — whose `.gate-banner` is the only `role="status"` written for gates
(`RunRail.tsx:109`, `:130`) — **is never rendered by any route**, referenced only by its own test,
CSS and `ROUTES.md`. Five gate changes produced zero live-region mutations.

Fix, in order of value:
1. `apps/dashboard/src/features/runs/RunDetailPage.tsx` — mount a permanently-present, visually
   hidden `<div role="status" aria-live="polite">` in `RunWorkspace` and write one sentence into
   it on gate open / phase transition / stream state change ("Prompt review gate open on Story
   Analysis", "Test Planning running", "Live updates reconnecting"). It must be mounted **before**
   the event: screen readers do not announce a live region that appears at the same moment as its
   content.
2. `apps/dashboard/src/features/runs/runDisplay.ts` — make `orchestrationStatusLabel()` include
   the current phase for `busy`, so the existing badge carries phase transitions.
3. `apps/dashboard/src/features/runs/RunRail.tsx` — either render it or delete it; leaving dead
   code that other components defer to is how this gap survived.
4. `apps/dashboard/src/features/runs/LiveStatusChip.tsx` — update the comment once (3) is settled.

### A11Y-07 — phase buttons expose raw backend keys as their accessible name
**P2 · WCAG 2.5.3 Label in Name (A), also 1.3.1 · `/runs/:threadId`,
`/runs/:threadId/phases/:phase`, `/runs/:threadId/reviews/:interruptId`, `/approvals` detail ·
connected + demo · all themes · all viewports**

Repro: inspect a `.phase-step` button.
Expected: the accessible name contains the visible label. Actual —

| | |
|---|---|
| visible | `1 Story Analysis Results Ready` |
| `aria-label` / `title` | `story_analysis — awaiting_output_review (attempt 1)` |

"Story Analysis" is not contained in "story_analysis" (the underscore breaks the match), and the
visible status "Results Ready" is replaced by the internal `awaiting_output_review`. Seven buttons
per run route. A speech-input user saying "Story Analysis" gets no match, and a screen-reader user
hears database identifiers instead of the product's own vocabulary.

Fix: `apps/dashboard/src/components/runs/PhaseStrip.tsx`, `segmentLabel()` (line ~47) — build the
name from `PHASE_LABELS[phase]` and `pipelineStatusLabel(status)`, the same two functions the
visible markup already uses: `` `${PHASE_LABELS[phase]} — ${pipelineStatusLabel(status)}${attemptSuffix}` ``.

### A11Y-08 — the New Test wizard steals focus after the user has moved on
**P2 · WCAG 2.4.3 Focus Order (A) · `/runs/new` · connected · all themes · all viewports**

Repro, pointer-free: on any wizard step, press `Enter` on `Next`, then immediately press `Tab`
twice, then wait ~2 s.
Expected: focus moves once, predictably, and stays where the user put it. Actual: focus stays on
`Next`, the two Tabs take the user to "Skip to content" and then the "New Test" sidebar link, and
then — with no further input — the wizard moves focus to the new step's tab button.

Fix: `apps/dashboard/src/features/new-test/NewRunWizard.tsx:219-225` — make the
`pendingFocusRef` effect a no-op if `document.activeElement` has moved outside the wizard panel
since the request was queued, or move focus synchronously in the `Next` handler
(`jumpToTab`) rather than through an effect plus `afterNextPaint` (`:467`).

### A11Y-09 — the approvals queue is a listbox that can never be focused
**P2 · WCAG 4.1.2 Name, Role, Value (A) · `/approvals` · connected · all themes · all viewports**

Repro: inspect `ul.approvals-queue`.
Expected (ARIA listbox pattern): the listbox is focusable (`tabindex="0"`), carries
`aria-activedescendant`, and its options are `tabindex="-1"`.
Actual: `role="listbox"`, `aria-label="Needs review queue"`, **`tabindex` absent**,
`aria-activedescendant="approvals-row-…"` — which has no effect because the element it sits on can
never hold focus — while each of the 14 `role="option"` rows has `tabIndex="0"`, adding 14 stops
to the Tab order. This is the structural reason the `j`/`k` model is unannounceable: there is no
focused listbox for `aria-activedescendant` to speak through.

Fix: `apps/dashboard/src/features/approvals/ApprovalsQueue.tsx` — put `tabIndex={0}` on the
`<ul role="listbox">`, set `tabIndex={-1}` on every `<li role="option">`, keep
`aria-activedescendant` pointing at the selected row, and route `j`/`k`/Arrow handling through the
listbox's own `onKeyDown` as well as the document listener. That change alone makes queue
movement announce the row without any new live region.

### A11Y-10 — heading levels skip from `h1` to `h3`
**P3 · WCAG 1.3.1 Info and Relationships (A) · `/`, `/runs/new`, `/settings` · connected ·
theme-independent · all viewports**

axe `heading-order`, 1 node each. `/` — `h3.home-section-title` "Needs your attention" follows the
topbar `h1`. `/runs/new` — `h3.wizard-section-title` "Test Scope". `/settings` —
`h3.settings-section-title` "Appearance".

Fix: `features/home/HomePage.tsx`, `features/new-test/NewRunWizard.tsx` (`WizardSection`),
`features/settings/SettingsPage.tsx` — make the first in-page section heading an `h2`, or give
each page an `h2` under the topbar `h1`.

### A11Y-11 — the CodeMirror editing surface has no accessible name
**P3 · WCAG 4.1.2 (A) · `/runs/:threadId/phases/:phase`,
`/runs/:threadId/reviews/:interruptId` · connected · theme-independent · all viewports**

axe `aria-input-field-name`, 1 node each: `div[contenteditable="true"][role="textbox"]` with no
`aria-label`. A screen-reader user focusing the prompt editor hears "edit text" with no indication
of which prompt.

`PromptTabsEditor.tsx` already passes `aria-label={activeLabel}` to `<CodeMirror>`, but
`@uiw/react-codemirror` forwards unknown props to its **wrapper** `<div>`, not to the
contenteditable that actually carries `role="textbox"` — so the name never reaches the control.

Fix: `apps/dashboard/src/features/runs/PromptTabsEditor.tsx` — pass the label through a real
CodeMirror extension instead, `EditorView.contentAttributes.of({ 'aria-label': activeLabel })`
in `extensions`, so it lands on `.cm-content`. Keep the wrapper prop or drop it; it does nothing.

### A11Y-12 — the editor's scroller is keyboard-focusable but empty of focusable content
**P3 · WCAG 2.1.1 Keyboard (A) · same two routes as A11Y-11 · theme-independent**

axe `scrollable-region-focusable`, 1 node each: `div.cm-scroller[tabindex="-1"]`. Low practical
impact (the content div inside is focusable); listed for completeness because it is one of the
five rules the sweep flagged. Upstream CodeMirror markup — resolve by upgrading CodeMirror or
suppressing the rule with a documented exception rather than patching the DOM.

### A11Y-13 — `<aside class="gate-decision-context">` is nested inside another landmark
**P3 · WCAG 1.3.1 (A) · `/approvals`, `/approvals/:threadId/:interruptId`,
`/runs/:threadId/phases/:phase`, `/runs/:threadId/reviews/:interruptId` · theme-independent**

axe `landmark-complementary-is-top-level`, 1 node each. The gate's explanatory aside sits inside
`<section aria-label="Run workspace">`, so it appears in a screen reader's landmark list
detached from its context.

Fix: `apps/dashboard/src/hitl/GateDecisionContext.tsx:12` — change `<aside
className="gate-decision-context">` to a `<div>`. It is inline guidance inside the gate, not a
page-level complement, so it should not be a landmark at all.

### A11Y-14 — no theme declares `color-scheme`, so UA widgets stay light on dark themes
**P3 · no single SC (supports 1.4.3 / 1.4.11) · all routes · all dark themes · all viewports**

Repro: set `data-theme="dark"` and read `getComputedStyle(document.documentElement).colorScheme`.
Expected `dark`. Actual **`normal`**, on a `#050508` page. Native scrollbars, `<select>` option
popups, date pickers and autofill chrome therefore render in light mode over near-black surfaces.

Fix: `apps/dashboard/src/theme/tokens.css` — add `color-scheme: dark;` to `:root`;
`theme/themes.css` — `color-scheme: light` on `[data-theme='light']` and
`[data-theme='solarized-light']`, `dark` on `[data-theme='solarized-dark']` and
`[data-theme='monokai-dimmed']`; `theme/apex-light.css` — `color-scheme: light`.

### A11Y-15 — controls sized 15–23 px pass SC 2.5.8 only on the spacing exception
**P3 · WCAG 2.5.8 Target Size (Minimum), AA — currently met · measured at 1440, 768, 390 ·
all themes**

No failures (see §6.2 for the full computation). Recording it because four of these controls are
within 1–9 px of the minimum and any future tightening of layout spacing turns them into failures:
`.engine-metric-tab` **23.00 px** tall with a 2 px sibling gap, `.level-chip` **23.0 px**,
`.overflow-menu-trigger` **28.3 × 20.0** with 22.1 px of clearance from "Open run",
`.analytics-sort` **15.0 px** tall, `.runs-run-title.runs-run-button` **15.6 px** tall at ≤ 768.

Fix (cheap, pre-emptive): `features/runs/live.css:256-268` — `min-height: 24px` on
`.engine-metric-tab`; `features/runs/preflight.css:203-213` — `min-width: 24px; min-height: 24px`
on `.overflow-menu-trigger`; the analytics chips and sort buttons likewise.

### A11Y-16 — disabled controls drop to ~2:1
**P3 · exempt from WCAG 1.4.3 ("inactive user interface components"), reported as legibility ·
all routes · all themes**

`ux.css:17` `.btn:disabled { opacity: 0.45 }` puts every disabled button's label between **1.95:1
and 3.31:1** against its own fill (table in §4.4). The wizard's `Next` and the gate's primary
action both spend time disabled, and at that ratio a low-vision user cannot read which action is
blocked.

Fix: `apps/dashboard/src/theme/ux.css:17` — replace the blanket opacity with explicit disabled
tokens (`--control-disabled-bg`, `--control-disabled-text`) defined per theme in
`theme/accessibility.css` at ≥ 4.5:1, and keep the opacity only for the shadow/border.

### A11Y-17 — run-calendar chip tokens fail, but the surface is not reachable in this build
**P3 · WCAG 1.4.3 · surface not reachable · apex-light, solarized-dark**

`--run-calendar-cron-text` on `--run-calendar-cron-bg` is **2.82:1** (apex-light) and **3.17:1**
(solarized-dark); `--run-calendar-ad-hoc-text` is **2.65:1** (solarized-dark). Token-level only —
I found no route in this build that renders the run calendar, so this is a latent defect.

Fix: `theme/apex-light.css` — `--run-calendar-cron-text` from `#0faa6c` to `#0f7a50` (the value
`--success-text` already uses); `theme/themes.css` `[data-theme='solarized-dark']` — raise both
calendar text tokens the way `accessibility.css` already raises the tonal `-text` tokens.

---

## 10. Route × theme coverage

`F` = full axe ruleset (`color-contrast-enhanced` disabled). `C` = `color-contrast` +
`link-in-text-block`. The number is violation **nodes** found; `·` is zero. Structural rules were
run once per route on `apex-light` — themes only redefine colour custom properties, so no DOM
differs between them.

| # | Route (id used) | apex-light `F` | dark `C` | light `C` | sol-dark `C` | sol-light `C` | monokai `C` |
|---|---|---|---|---|---|---|---|
| 1 | `/` (Home, authenticated) | · | 1 | · | 1 | · | 1 |
| 2 | `/runs` | · | 1 | · | 1 | · | 1 |
| 3 | `/runs/new` | 1 (heading-order) | 1 | · | 1 | · | 1 |
| 4 | `/runs/compare?ids=…` (2 runs) | · | 1 | · | 1 | · | 1 |
| 5 | `/runs/01a14ad6-3c64…` | · | 1 | · | 1 | · | 1 |
| 6 | `/runs/01a14ad6-3c64…/reviews/27be803f…` | · | 1 | · | 1 | · | 1 |
| 7 | `/runs/01a14ad6-3c64…/phases/story_analysis` | 4 | 1 | · | 2 | 8 | 1 |
| 8 | `/runs/01a14ad6-3c64…/timeline` | · | 1 | · | 1 | · | 1 |
| 9 | `/runs/019eb97c…/artifacts/story_analysis-a1-transcript` | · | 1 | · | 1 | · | 1 |
| 10 | `/approvals` | · | 1 | · | 1 | · | 1 |
| 11 | `/approvals/01a14ad7-4a0e…/b05a19da…` | · | 1 | · | 1 | · | 1 |
| 12 | `/cycles` (empty state) | · | 1 | · | 1 | · | 1 |
| 13 | `/cycles/:cycleId` | — not reached — | | | | | |
| 14 | `/work-items` | · | 1 | · | 1 | · | 1 |
| 15 | `/work-items/saved` | · | 1 | · | 1 | · | 1 |
| 16 | `/work-items/:provider/:itemId` | — not reached — | | | | | |
| 17 | `/environments` | · | 1 | · | 1 | · | 1 |
| 18 | `/environments/326c7d3e…` | · | 1 | · | 1 | · | 1 |
| 19 | `/context` | · | 1 | · | 1 | · | 1 |
| 20 | `/evidence` | · | 1 | · | 1 | · | 1 |
| 21 | `/golden-configs` (backend 404 → error state) | · | 1 | · | 1 | · | 1 |
| 22 | `/golden-configs/:assistantId` | — not reached — | | | | | |
| 23 | `/prompts` | 1 | 1 | · | 1 | 9 | 1 |
| 24 | `/prompts/phase/env_triage%2Fsystem` | 1 | 1 | · | 1 | 9 | 1 |
| 25 | `/prompts/phase/env_triage%2Fsystem/versions/1` | · | 1 | · | 1 | · | 1 |
| 26 | `/prompts/phase/env_triage%2Fsystem/playground` | · | 1 | · | 1 | · | 1 |
| 27 | `/analytics` | · | 1 | · | 1 | · | 1 |
| 28 | `/logs` | · | 1 | · | 1 | · | 1 |
| 29 | `/apex-load` | · | 1 | · | 1 | · | 1 |
| 30 | `/admin/connections` | · | 1 | · | 1 | · | 1 |
| 31 | `/admin/connections/080164a4…` | · | 1 | · | 1 | · | 1 |
| 32 | `/admin/consumers` | · | 1 | · | 1 | · | 1 |
| 33 | `/admin/consumers/3ebe0a0f…` | · | 1 | · | 1 | · | 1 |
| 34 | `/admin/system` | · | 1 | · | 1 | · | 1 |
| 35 | `/settings` | 1 (heading-order) | 1 | · | 1 | · | 1 |
| 36 | `*` → `/no-such-route-404` | · | 1 | · | 1 | · | 1 |

Structural nodes inside the `apex-light` column, by rule:

| Rule | Impact | Routes |
|---|---|---|
| `heading-order` | moderate | `/`, `/runs/new`, `/settings` |
| `aria-input-field-name` | serious | `/runs/:t/phases/:p`, `/runs/:t/reviews/:i` |
| `landmark-complementary-is-top-level` | moderate | `/approvals`, `/approvals/:t/:i`, `/runs/:t/phases/:p`, `/runs/:t/reviews/:i` |
| `scrollable-region-focusable` | serious | `/runs/:t/phases/:p`, `/runs/:t/reviews/:i` |
| `color-contrast` | serious | see table above |

**`system` preference**, tested with `prefers-color-scheme` emulation on `/runs`:

| emulated | stored preference | resolved `data-theme` | axe `color-contrast` |
|---|---|---|---|
| dark | none (fresh browser) | `apex-light` — the OS preference is ignored (known finding, blast radius in §8.3) | n/a |
| dark | `system` | `dark` | 1 node (`.dash-badge`) |
| light | `system` | `light` | 0 nodes |

Note on emulation: switching `prefers-color-scheme` in this harness changes
`matchMedia(...).matches` without firing a `change` event, so the provider's runtime listener
could not be exercised; the resolutions above were taken after a reload. I am **not** reporting a
"theme does not follow a live OS switch" finding, because the evidence would be an artifact.

### Could not reach

| Route | Reason |
|---|---|
| `/cycles/:cycleId` | `GET /v1/cycles` returns `[]` in connected mode — no cycle exists to open, and creating one would be a write. `/cycles` itself was covered in its empty state. |
| `/golden-configs/:assistantId` | The index endpoint is not served by this backend (`/v1/golden-configs` → 404). `/golden-configs` renders its error state with a "Retry" button, which I covered; there are no ids to open. |
| `/work-items/:provider/:itemId` | No work-tracking provider is bound in connected mode; `/work-items` shows "Run a query to see work items" and yields no item links. |
| `/` as the **ApiKeyGate** | The origin was already authenticated (the key is persisted per-origin and shared with the other reviewers' tabs), so `/` rendered Home. The gate's `landmark-one-main` / `region` failures are already confirmed and were not re-tested; I found no wider blast radius — every other route is inside `<main id="main-content">` from `AppShell.tsx:61`. |
| Live-stream state transitions (`connecting` → `live` → `reconnecting` → `error`) | No run was streaming during the review; `LiveStatusChip` was observed in `idle`. The finding in A11Y-06 rests on the chip's measured absence of `role`/`aria-live`, which holds in every state. |
| Multi-series charts in **connected** mode | `/analytics` has a single "unknown" series there; A11Y-03's screenshot is from demo (`:4173`). The contrast numbers come from the theme token files and are mode-independent. |
| `.engine-metric-tab` in situ | No run in connected mode carries live engine stats, so `EngineStrip` never mounted. Measured instead by rendering the component's exact markup and classes into the live page and reading `getBoundingClientRect()` — see §6.2. |

---

## 11. Artifacts

| File | Shows |
|---|---|
| `a11y-01-dash-badge-dark-theme.jpg` | A11Y-01 — white-on-violet "15" badge, `dark` theme, `/runs`, 1440 |
| `a11y-02-analytics-chips-solarized-light.jpg` | A11Y-15 — 23 px `.level-chip` filter chips, `solarized-light`, `/analytics`, 1440 |
| `a11y-03-prompt-gate-tab-trap-edited.jpg` | A11Y-04 — gate marked `edited` and primary action changed to "Save Edit & Re-review" after eight trapped Tab presses |
| `a11y-04-chart-series-solarized-light.jpg` | A11Y-03 — two chart series at 1.00:1 against each other, demo mode |
| `a11y-05-codemirror-gutter-apex-light.jpg` | A11Y-02 — 3.30:1 gutter line numbers, `apex-light`, `/prompts/phase/env_triage%2Fsystem` |
| `a11y-06-reflow-720x450-200pct.jpg` | §6.1 — clean reflow at 720×450 (≈1440 at 200%), no horizontal scroll |
