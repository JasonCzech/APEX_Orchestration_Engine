# Independent verification — holistic UX review, 2026-10-17

**Verifier:** independent reviewer. I did not write any of the findings under review.
**Method:** reproduce each finding from its own stated steps and evidence, in the running
environments (`:4173` demo, `:4175` connected, API `:2024`), against the current source tree.
**Scope worked, in the brief's priority order:** every P0/P1 in the six documents; every finding
whose evidence is computed or inferred rather than observed; cross-document contradictions; a
sample of P2s and a few P3s that fell out of the same probes.

**Browser:** my own tab (`tab-12`), 1440×900 unless stated. Per the standing method warning I
injected `*{transition:none!important;animation:none!important}` before any colour read and gated
every read on the computed value stabilising across three consecutive frames. I did not patch
`window.fetch` (it cannot fault this build); every data claim below is checked against real server
responses. Viewport emulation was reset at the end.

**Environment drift matters for the counted claims.** The connected fleet was 45 runs when
`CORRECTNESS.md` was written and 48 when `AGENT_UX.md` was written; it is **50** now
(23 `idle`, 18 `interrupted`, 9 `error`). Where a finding states a denominator I re-derived the
ratio from a fresh snapshot and say whether the numerator still holds. `GET /v1/pipelines` caps
`limit` at 20, so the full fleet needs pagination — a reviewer who misses that will under-count.

---

## Summary table

| ID | Doc | Stated | Verdict | Severity I would assign |
|---|---|---|---|---|
| A11Y-01 | ACCESSIBILITY | P1 | **CONFIRMED** | P1 |
| A11Y-03 | ACCESSIBILITY | P1 | **CONFIRMED** (2 incidental numbers off) | P1 |
| A11Y-05 | ACCESSIBILITY | P1 | **PARTIALLY CONFIRMED** — consequence not reproducible | **P3** |
| A11Y-06 | ACCESSIBILITY | P1 | **CONFIRMED** (one runtime measurement corrected) | P1 |
| A11Y-07 | ACCESSIBILITY | P2 | **CONFIRMED** (incidental) | P2 |
| A11Y-09 | ACCESSIBILITY | P2 | **PARTIALLY CONFIRMED** — "14 extra tab stops" is false | **P3** |
| A11Y-15 | ACCESSIBILITY | P3 | **CONFIRMED** (and it settles a contradiction) | P3 |
| A11Y §1 arithmetic | ACCESSIBILITY | — | **CONFIRMED** | — |
| CON-01 counts | CONSISTENCY | P1 | **CONFIRMED** (27 / 36 exact) | P1 |
| CON-02 | CONSISTENCY | P1 | **PARTIALLY CONFIRMED** — SC 2.5.8 verdict is wrong | P2 |
| CON-03 | CONSISTENCY | P1 | **CONFIRMED** | P1 |
| CON-06 | CONSISTENCY | P2 | **PARTIALLY CONFIRMED** — z-index count undercounts | P2 |
| CON-09 | CONSISTENCY | P2 | **CONFIRMED** | P2 |
| CONSISTENCY verdict counts | CONSISTENCY | — | **PARTIALLY CONFIRMED** — `role=alert/status` ~240 is 185 | — |
| COR-04 | CORRECTNESS | P2 | **CONFIRMED** (10 of 50, still exactly 10) | P2 |
| COR-05 | CORRECTNESS | P2 | **CONFIRMED** | P2 |
| COR-06 | CORRECTNESS | P2 | **CONFIRMED** (6 + 12 = 18 exact) | P2 |
| COR-07 | CORRECTNESS | P2 | **CONFIRMED** with a scope qualifier | P2 |
| COR-08 | CORRECTNESS | P2 | **CONFIRMED** | P2 |
| COR-09 | CORRECTNESS | P2 | **CONFIRMED** | P2 |
| COR-10 | CORRECTNESS | P2 | **CONFIRMED**, numbers drifted (worse) | P2 |
| COR-11 | CORRECTNESS | P2 | **PARTIALLY CONFIRMED** (tone half verified) | P2 |
| COR-17 | CORRECTNESS | P3 | **CONFIRMED**, numbers drifted | P3 |
| AGT-01 | AGENT_UX | P1 | **CONFIRMED** | P1 |
| AGT-08 | AGENT_UX | P2 | **CONFIRMED** | P2 |
| ARE-01 | AREAS | P1 | **CONFIRMED** verbatim | P1 |
| ARE-02 | AREAS | P1 | **CONFIRMED** by source + consequence chain | P1 |
| ARE-08 | AREAS | P1 | **CONFIRMED** | P1 |
| ARE-10 | AREAS | P1 | **CONFIRMED** | P1 |
| ARE-13 | AREAS | P1 | **CONFIRMED** (read side; POST rows not re-run) | P1 |
| LOAD gap #1 (54/57) | LOAD_CONVERGENCE | — | **CONFIRMED** exactly | — |
| LOAD gaps #3,#4,#6,#13,#22 counts | LOAD_CONVERGENCE | — | **CONFIRMED** exactly | — |
| LOAD gap #7 / §7 "declared twice" | LOAD_CONVERGENCE | — | **NOT REPRODUCED** — 6 declarations | — |
| LOAD gap #17 (5 undeclared tokens) | LOAD_CONVERGENCE | — | **CONFIRMED** | — |

---

## ACCESSIBILITY

### A11Y-01 — `.dash-badge` hardcodes white — **CONFIRMED**

This is a contrast claim computed from tokens, so I did both halves: recomputed the arithmetic
from the effective cascade, then measured the rendered pixels.

**Cascade.** `theme/index.css` imports `accessibility.css` last, after `tokens.css`, `themes.css`,
`apex-light.css` and `primitives.css`, so `accessibility.css`'s `--accent` and `--accent-contrast`
win at equal specificity. `apex-light.css:119-123` does touch `.dash-badge` but only sets
`border-radius: 20px`, so it does not disturb the colours. `primitives.css:188-190` is
`background: var(--accent); color: white` as quoted.

**Measured on rendered pixels** (demo, sidebar "tests need review" badge, transitions killed,
each read gated on the computed value stabilising):

| theme | rendered fg/bg | measured | claimed | fix value (recomputed) | claimed |
|---|---|---|---|---|---|
| apex-light | `#fff` on `rgb(0,118,157)` | **5.15** | 5.15 | 5.15 | 5.15 |
| dark | `#fff` on `rgb(165,128,248)` | **2.98** | 2.98 | 7.04 | 7.04 |
| light | `#fff` on `rgb(79,70,229)` | **6.29** | 6.29 | 6.29 | 6.29 |
| solarized-dark | `#fff` on `rgb(112,184,223)` | **2.19** | 2.19 | 9.61 | 9.61 |
| solarized-light | `#fff` on `rgb(89,103,0)` | **6.24** | 6.24 | 6.24 | 6.24 |
| monokai-dimmed | `#fff` on `rgb(255,151,176)` | **2.04** | 2.04 | 10.31 | 10.31 |

All twelve numbers reproduce exactly. The three dark themes fail SC 1.4.3. The latent
`.btn-success` figures also reproduce exactly (2.54 / 1.85 / 1.59, and 3.30 for the hardcoded
`#16a34a` hover).

**Scope nuance, not a defect in the finding.** The finding says "all viewports". At 390 px the
sidebar is **unmounted**, not hidden — `.dash-badge` does not exist in the DOM until the
"Open navigation" drawer is opened, after which it measures 5.15 as expected. So the
"33 routes × 3 dark themes = 99 nodes" figure is a 1440-width figure; an axe sweep at 390 without
opening the drawer finds zero. The badge does render in connected mode (it read "15", matching the
15 open gates), so the "every route" claim holds there too.

**§1 arithmetic.** 129 + 4 + 3 + 2 + 2 = 140 ✓. 33 × 3 = 99 ✓. 129 − 99 = 30 ✓. Internally consistent.

### A11Y-03 — chart series encoded by colour alone — **CONFIRMED**

**Source claims, all exact.** `AnalyticsPage.tsx:529-543` sets `stroke` and `fill` only;
`fillOpacity` is 0.16 / 0.12 / 0.14 (the finding's "0.12–0.16"); every series carries
`dot={false}`; `strokeDasharray` appears only on `CartesianGrid`, never on a series. Colour is
therefore the sole encoding (SC 1.4.1). Six `--chart-*` tokens exist, as claimed.

**Series-vs-series, recomputed from the effective cascade — every worst pair matches:**
solarized-light `--chart-1`/`--chart-2` **1.00**, solarized-dark `--chart-2`/`--chart-3` **1.00**,
monokai-dimmed `--chart-2`/`--chart-5` **1.00**, dark 1.02, light 1.05, apex-light 1.10. The claim
"no pair of series reaches 3:1 in any theme; the best anywhere is 2.62:1" reproduces exactly —
the global maximum is **2.62** (apex-light `--chart-3` vs `--chart-5`).

**Two incidental numbers I could not reproduce.** In the series-vs-surface table, apex-light 2.32,
solarized-light 2.67, solarized-dark 2.87 and light 3.04 all match, but `dark` (claimed 3.89) and
`monokai-dimmed` (claimed 3.81) measure **4.59** and **5.27** against `--bg-primary` and the
composited `--bg-card`. Both are rows the finding itself marks "pass", and my figures are *higher*,
so no verdict changes; the finding presumably measured against a different surface token. Worth a
footnote, not a downgrade.

### A11Y-05 — approvals inbox drops `j`/`k` at triage speed — **PARTIALLY CONFIRMED** (propose P3)

**The mechanism is real and I confirmed it in source.** `ApprovalsInboxPage.tsx:216-224` —
`moveSelection` resolves the current index from `stateRef.current.selection`, and `stateRef.current`
is only rewritten during render (`:154-155`). `setSelection` routes through the router. So presses
that arrive before React commits all compute the same next index.

**The stated user-visible consequence does not reproduce.** On `/approvals` (15 rows, connected),
resetting to row 0 before each trial:

| trial | presses | net advance |
|---|---|---|
| synchronous burst, no yield between dispatches | 6 | **1** |
| 0 ms gap via `setTimeout(…,0)` (separate macrotask) | 6 | **6** |
| 5 ms gap | 6 | **6** |
| 10 ms gap | 6 | **6** |
| 16 ms gap | 6 | **6** |
| 25 / 30 / 120 ms gap | 6 | **6** |
| 1200 ms gap | 5 | 5 |

The drop needs all six `keydown`s to land in **one JS task**. Any separation at all — even
`setTimeout(…, 0)` — lets React commit and every press lands. Real key events always arrive as
separate input tasks: one real `j` driven through the browser advanced exactly one row, and the
driver's own guard ("the page changed during the key sequence") shows each press commits a route
change before the next is delivered. Key auto-repeat (~30 ms) also lands cleanly.

So "press `j` six times at normal typing speed → selection advances one row" is not reproducible;
no human or auto-repeat cadence produces the required condition. **Method limitation I will own:**
my multi-press trials used synthetic `KeyboardEvent` dispatch, because the automation layer aborts
a real multi-key sequence when the page navigates. But the single real press behaved 1:1, and the
mechanism (a commit between tasks) predicts exactly what I measured.

Verdict: latent race, correctly diagnosed, with no demonstrable keystroke loss. I would assign
**P3** and reword it as a robustness fix rather than "keystrokes are dropped at triage speed".

### A11Y-06 — no live region announces a gate, a phase change or a stream change — **CONFIRMED**

Every checkable claim holds.

- **Exactly three `aria-live` attributes in the dashboard source**, at exactly the cited lines:
  `ApexLoadWorkspacePage.tsx:877`, `NewRunWizard.tsx:537`, `RunDetailPage.tsx:216` ✓
- **`RunRail` is dead code** ✓ — the only import anywhere is its own unit test
  (`__tests__/RunRail.test.tsx:7`); no route, page or layout imports it. Other references are
  `ROUTES.md:201` and a comment in `testUtils.tsx:205`.
- **`LiveStatusChip` has no `role` and no `aria-live`** ✓, and its source comment is verbatim:
  "Deliberately NOT role="status": the run rail's gate banner owns that landmark on this page" —
  so the justification does rest on a component that never ships.
- **`orchestrationStatusLabel()` maps `busy` → `'Running'`** for every phase ✓ (`runDisplay.ts`).
- **Runtime, on the cited run** `01a14ad7-4a0e-7f03-bd22-70df491f2e40` (open `phase_review`):
  `document.querySelector('.gate-banner')` is absent on both the run route and the phase route ✓.
  `.phase-strip` is `role="group"`, `aria-label="Phase progress"`, `aria-live` **null**, 7 button
  segments ✓. `.live-status-chip` has `role` null, `aria-live` null, `data-status="idle"`, text
  "Snapshot refresh active" ✓ — all four values exactly as §3.3 reports.

**One runtime measurement to correct.** §3.2 says "live regions present: exactly one". On the
*run* route that is right (one `span.status-badge.warning[role=status][aria-live=polite]`, text
"Awaiting approval"). On the *phase* route with the prompt editor mounted there are **four** —
the same badge plus three `div.cm-announced[aria-live=polite]` regions injected by CodeMirror.
They are library-authored and empty, and they announce none of the three events, so the finding's
substance is untouched; the "exactly one" measurement is just route-specific.

### A11Y-07 — phase buttons expose raw backend keys — **CONFIRMED** (incidental)

Fell out of the A11Y-06 probe. The first phase-strip segment's accessible name is
`"story_analysis — awaiting_output_review (attempt 1)"` while its visible text is
"Story Analysis / RESULTS READY".

### A11Y-09 — approvals queue is a listbox that can never be focused — **PARTIALLY CONFIRMED** (propose P3)

**The real half.** `ul.approvals-queue` is `role="listbox"`, `aria-label="Needs review queue"`,
carries `aria-activedescendant="approvals-row-…"`, and has **no `tabindex` attribute** ✓. So the
`aria-activedescendant` sits on an element the keyboard never focuses, and it cannot speak.

**The false half.** The finding says "each of the 14 `role="option"` rows has `tabIndex="0"`,
adding 14 stops to the Tab order." Measured on 15 rows:

| `tabindex` | count |
|---|---|
| `"0"` | **1** (and it is the row `aria-activedescendant` points at, `aria-selected="true"`) |
| `"-1"` | **14** |

The options implement a **correct roving tabindex**. The queue adds exactly **one** Tab stop, not
14. The recommended fix ("set `tabIndex={-1}` on every `<li role="option">`") is already true for
14 of 15 rows.

What is left is a redundant/ineffective `aria-activedescendant` on an unfocusable container,
alongside a focusable selected option that `j`/`k` never moves focus to (the lead already confirmed
that focus never moves). That is worth fixing but it is not Tab-order pollution. I would assign
**P3**.

### A11Y-15 / §6.2 — target sizes — **CONFIRMED**, and it settles a contradiction

I replicated `EngineStrip.tsx`'s exact tab markup inside a live phase workspace so the real CSS
and font inheritance applied, then measured with `getBoundingClientRect()`:

| control | measured height | sibling gap | centre-to-centre |
|---|---|---|---|
| `.engine-metric-tab` | **23.00 px** | **2.00 px** | **56.87 px** |

`font-size` resolved to 10.5 px, `padding` 3px 10px, `border-width` 1px, `min-height` auto,
`line-height` normal — the CSS the finding cites (`live.css:256-268`). §6.2's explicit correction
is right: the control is **23.00 px, not ~22**, it misses the 24 px minimum by one pixel, and it
**passes** SC 2.5.8 on the spacing exception, with centres 56.87 px apart against a 24 px
requirement. (A11Y reported 57.2 px; the small delta is label widths in my replica and does not
affect the test.)

I also measured the other tab classes on their real routes: `.ctx-tab` **25.00 px** (`/context`),
`.adm-tab` **28.00 px** (`/admin/connections`), `.prompt-review-tab` **33.00 px**,
`.workspace-tab` **36.00 px** with `min-height: 36px`, `.phase-step` 167 × 34. Every one of
CON-02's estimates is accurate to the pixel except `.engine-metric-tab`.

---

## CONSISTENCY

### CON-01 — counts — **CONFIRMED** (mechanism already confirmed by the lead)

- `.tonal-card` is defined **only** in `features/runs/run-detail.css` (lines 368, 376, 382). The
  one hit in `context.css:4` is a comment. ✓
- **27** `.tonal-card` call sites in `.tsx` — exactly as claimed ✓ — across 18 files, including the
  shared `components/CachedDataWarning.tsx:10` (`className="tonal-card danger" role="alert"`) ✓
- **36** routes in `routes/router.tsx` ✓ — the denominator used across all six documents.

I did not independently re-derive "21 of 36 routes"; the 18 consuming files include `home`,
`prompts`, `cycles`, `context`, `new-test` and `hitl`, which is consistent with it.

### CON-02 — 7 of 11 tablists — **PARTIALLY CONFIRMED**

**The tablist inventory is exact.** `role="tablist"` appears 10 times in `.tsx`; one is inside
`components/ux/TabList.tsx` itself, and `TabList` is used at exactly **two** call sites
(`SettingsPage.tsx:337`, `PhaseWorkspace.tsx:101`) — so 9 hand-rolls + 2 = **11 tablists** ✓.
All nine hand-roll line references are correct: ContextPage `:41`, PromptDetailPage `:357`,
ConnectionsPage `:333`, ConnectionDetailPage `:703`, ApexLoadWorkspacePage `:566`, EngineStrip
`:115`, PromptTabsEditor `:64`, NewRunWizard `:501`, EvidenceTab `:239`. I read rows 1, 2 and 6
in full and confirmed no `aria-controls`, no roving `tabIndex`, no `onKeyDown` and no linked panel.
`role="tabpanel"` exists in PromptTabsEditor at `:85` and `:98` (unlinked, as the table's ⚠️ says),
so "six of the seven have no `role="tabpanel"` anywhere" is right. `role="tab"` with `disabled`
confirmed at `PromptDetailPage.tsx:366` and `PromptTabsEditor.tsx:76`.

**The target-size verdict is wrong.** `.engine-metric-tab` is **23.00 px**, not ~22, and it does
**not** "fail WCAG 2.5.8 Target Size (Minimum, 24×24 px) outright" — SC 2.5.8's spacing exception
is satisfied with a wide margin (56.87 px between centres, requirement 24 px). See A11Y-15 above
and "Contradictions" below. Everything else in that table is accurate to the pixel, and the
underlying consistency point — five different tab heights because the local classes don't inherit
`theme/ux.css`'s `min-height` — stands in full.

Severity: the accessibility half of the P1 rests partly on a target-size failure that does not
exist. The roving-`tabIndex`/unlinked-panel half is real. I would assign **P2**.

### CON-03 — one concept, many words — **CONFIRMED**

Structural claims check out: **6** subsections (3a–3f) = "6 concepts" ✓; the section cites **116**
`file:line` references across 47 files and quotes **121** user-visible strings, so "90+ strings"
is conservative ✓.

I spot-checked 17 cited lines against the source. Fifteen are exact, including
`ApprovalsInboxPage.tsx:359` ("All clear. No gates awaiting review."), `:360`, `:377`
(`aria-label="Pending gates"`), `:402` ("Select a gate from the queue (j / k)."),
`PhaseWorkspace.tsx:278` ("Approvals") and `:109` ("Pipeline log"), `RunDetailPage.tsx:328`/`:331`
("Pipeline progress" / "Pipeline Progress"), `LaunchRunButton.tsx:168`
("What should this pipeline run test?"), `HomePage.tsx:186` ("Start your first pipeline") and
`:223` ("Approvals unavailable"), `LogsPage.tsx:266` ("Thread id filter"),
`cycles/pages.tsx:57` ("Open Test History"), `RunsListPage.tsx:567`
("Launch your first pipeline run to see it here."). Two drift: `RunRail.tsx:156` is
`<dl className="run-meta">` with the `<dt>Thread</dt>` at 157-159 (off by one), and
`runDisplay.ts:163` is blank — the `'Awaiting approval'` mapping is around line 151 (off by ~12).

The live product confirms the drift it describes: the sidebar says "Tests" while the Home empty
state says "Start your first pipeline" and the runs empty state says "Launch your first pipeline
run".

### CON-06 — one dialog shape, six times — **PARTIALLY CONFIRMED**

**Confirmed.** `--modal-backdrop` is declared in all six themes at exactly the cited lines —
`tokens.css:62`, `themes.css:67,139,218,293`, `apex-light.css:50` — and consumed by **zero**
components (I audited every `var(--…)` reference in `src/**/*.css` and `src/**/*.tsx`). The nine
feature overlay rules and their scrim literals are as tabulated, plus `navigation-overlay` for ten.
`role="dialog"` appears 3 times in `.tsx`.

**The z-index count undercounts.** "Three z-index scales" lists 1000 (portal host), 300
(approvals) and 2000 (cycles) and **omits the dominant tier**: `z-index: 60` is used by seven
overlays (`context.css:639`, `environments.css:236`, `admin.css:488`, `work-items.css:196`,
`prompts.css:318`, `preflight.css:16`, `live.css:296`). The measured set among overlays is
**60 / 80 / 300 / 2000** (80 is `AppShell.css:35`), plus 1000 on the portal host — five distinct
values, not three. LOAD §7's "four z-index tiers" is closer to right. Also the portal host
`zIndex = '1000'` is at `components/Dialog.tsx:201`, not `:193`.

### CON-09 — tokens consumed without a usable fallback — **CONFIRMED**

I reimplemented the audit independently (parse every `--x:` declaration in `theme/*.css`, parse
every `var(--x` reference across `src/**/*.css` and `src/**/*.tsx`, record whether a fallback is
supplied). Results match the finding exactly:

**Referenced but declared nowhere — exactly five**, with the claimed sites and fallback status:

| token | site | fallback |
|---|---|---|
| `--bg-app` | `features/new-test/wizard.css:447` | **none** |
| `--shadow-lg` | `features/admin/admin.css:590` | **none** |
| `--bg-overlay` | `components/layout/AppShell.css:98` | yes |
| `--panel-border` | `features/context/context.css:148` | yes (a literal white) |
| `--text-tertiary` | `features/work-items/work-items.css:331` | yes |

`--accent-results` is declared **only** at `apex-light.css:21` and referenced at four sites
(`home.css:66`, `PhaseStrip.css:146`, `:147`, `:148`) — **all four without a fallback** ✓.
`--subtle-fill` has **9** reference sites of which **7 carry a fallback** ✓; the one that matters
is `context.css:600`. (My second no-fallback hit is inside the `apex-light` block itself, where the
token is defined, so it is harmless — the finding's analysis is right.)

One incidental number I could not reproduce: "the base `:root` block defines 72 tokens" — I count
**77** `--x:` declarations across the `:root`/`:root[data-theme]` blocks in `theme/`. Different
denominator, no bearing on the finding.

### CONSISTENCY verdict paragraph — counts — **PARTIALLY CONFIRMED**

The verdict praises primitive adoption with three numbers. Two are off by one and one is well off:

| claim | measured |
|---|---|
| "`ProblemCard` is adopted at 42 call sites" | **41** `<ProblemCard` across 27 files |
| "`Dialog` at 22" | **23** `<Dialog` (excluding the 4 in `Dialog.test.tsx`), 18 files |
| "`role="alert"`/`role="status"` at ~240" | **185** (106 + 79; no dynamic `role={…}` variants) |

The 185 figure matters because it is the evidence for "the primitives are in good shape". It
reaches ≈226 only if you also count the `role="alert"` that `ProblemCard.tsx:15` renders internally
at each of its 41 call sites, which may be what was intended. The qualitative point stands either
way; the number should be corrected or its basis stated.

---

## CORRECTNESS

### COR-04 — two test-result labels for one run — **CONFIRMED**

Mechanism exact: `RunsListPage.tsx:69`, `:124`, `:172` and `:249` all call
`executionResultLabel(run)` with **no `state`** argument, so `executionWasOmitted(undefined)` is
false and the grid always says "Not available"; `RunDetailPage.tsx:219` calls
`executionResultLabel(detail, state)` and can reach "Not applicable".

I re-derived the population across all 50 runs using `executionWasOmitted`'s exact predicate:
**exactly 10 runs** diverge — the same number the finding states for a 45-run fleet, still 10 at
50 runs. All ten have `phases_plan: ['story_analysis']` with no `execution` result, no
`external_results` and no `engine_handle`.

### COR-05 — "x/y phases finished" counts failed, aborted and skipped — **CONFIRMED**

`runDisplay.ts:124-126` — `isPipelinePhaseComplete` is
`['succeeded','failed','aborted','skipped'].includes(status)` ✓.

Found and confirmed on screen: run `01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90`
("ACC add-a-line — prompt gate skip", 1 `skipped` + 5 `failed` + 1 `succeeded`) renders
**"ORCHESTRATION FAILED · Test result: Not available · 7/7 phases finished"**. It is the only run
in the current fleet in that state.

### COR-06 — phase status read from two disagreeing sources — **CONFIRMED**

I pulled all 50 run details and diffed `phase_strip[].status` against
`values.phase_results[].status`. The finding's two families reproduce **exactly**:

| family | runs claimed | runs measured |
|---|---|---|
| errored mid-execution — `execution: running` vs `pending` | 6 | **6** |
| parked at a gate — `awaiting_*_review` vs `pending` | 12 | **7** `awaiting_output_review` + **5** `awaiting_prompt_review` = **12** |

6 + 12 = 18, as stated.

My snapshot shows two further runs in a family the finding does not list
(`phase_strip: none` vs `phase_results: succeeded`, both titled "M1 demo"). I chased these because
they looked like they realised the divergence COR-06 calls hypothetical — **they do not.** Both
have `phases_plan: ['env_triage']`, and `normalizedPhases` zeroes every phase outside the plan, so
grid and detail both compute **1/1**. COR-06's claim that "the `x/y` counters agree today" survives.
The raw-payload disagreement is real but masked correctly.

### COR-07 — analytics day buckets labelled one day early — **CONFIRMED**, with a scope qualifier

`/v1/analytics/agents` returns exactly one series bucket, `bucket_start: "2026-10-17T00:00:00Z"`.
The rendered x-axis tick reads **"Oct 16"**. Exactly the stated example.

Mechanism: `bucketTick` (`AnalyticsPage.tsx:81-89`) does
`new Date(iso).toLocaleDateString(undefined, {month:'short', day:'numeric'})` — a UTC→local
conversion. The host is `America/New_York` (UTC−4). **The qualifier the finding should carry:**
the label is one day *early* only for viewers at negative UTC offsets; at UTC it is correct and at
positive offsets it can be a day *late* for late-day buckets. The defect is real everywhere (a
UTC instant rendered in local time with no zone stated); "one day early" is locale-specific.

### COR-08 — `/analytics` names no time window — **CONFIRMED**

The payload carries `window: {from: "2026-10-10T19:15:19Z", to: "2026-10-17T19:15:19Z",
bucket: "day"}`. I probed the rendered page text for `2026-10-10`, `2026-10-17`, `Last 7`,
`7 days`, `day`, `Window`, `window` — **all seven absent**. `data.window.bucket` is used only to
pick a tick formatter. Nothing on screen states the window.

### COR-09 — MODELS filter lists four models in neither dataset — **CONFIRMED**

`AnalyticsPage.tsx:59-64` hardcodes `['claude-3-5-sonnet-latest','claude-3-5-haiku-latest',
'gpt-4o','gpt-4o-mini']`, and all four render as chips under "MODELS". The dataset's only model key
is **`unknown`** (`breakdown[0].key`), and `totals.models` is **0**. Every option therefore selects
an empty window.

### COR-10 — gate wait recorded as agent latency — **CONFIRMED**, numbers drifted worse

API now: `avg_latency_ms: 81937.11`, `p95_latency_ms: 5587.60`. Rendered:
**"AVG LATENCY 81.9s · p95 5.6s"**. The p95 matches the finding's 5.6s exactly; the average has
drifted from 53.0s to 81.9s as more events accumulated. The absurdity the finding identifies is
sharper now — the mean is **14.6×** the p95, which only happens when a handful of multi-hour human
waits are averaged into an agent-latency measure. Diagnosis well supported.

### COR-11 — run detail badge tone — **PARTIALLY CONFIRMED**

I verified the tone half on run `01a14ad6-1f91-…`: the header badge is
`class="status-badge neutral"` with text "Orchestration failed", even though
`orchestrationTone()` returns `'danger'` for that label — `RunDetailPage.tsx:216` passes a
different tone via `threadStatusBadge(detail.thread_status, …)`. Confirmed.
I did not re-run Repro A (the demo "Stopped by operator" text contradiction).

### COR-17 — "0 models" over a table listing a model — **CONFIRMED**, numbers drifted

Rendered: **"AGENTS / RUNS 7 / 15 · 0 models"**, with the breakdown table showing one row:
`unknown | 15 | 0 | 0 / 0 | 81.9s | 5.6s | 5`. The agents figure (7) matches the finding exactly;
runs drifted 11 → 15. Incidentally the ERROR RATE 6.3% checks out: 5 errors / 79 events = 6.33%.

---

## AGENT_UX

### AGT-01 — no agent evidence at a `phase_review` gate — **CONFIRMED**

**API, on the cited run** `01a14ad6-2be7-7981-bc48-d5915e182552`. The `phase_review` payload keys
are verbatim the quoted list: `["schema_version","kind","phase","actions","summary",
"result_preview","artifacts","warnings","dialogue_tail"]` — no provenance, no reasoning, no tool
calls, no model.

The asymmetry is exact too. The `prompt_review` gate on `01a14ad6-1b76-…` carries
`["schema_version","kind","phase","actions","prompt","additional_context","context_packets",
"tools","editable"]`, and `prompt.source` is verbatim
`{"origin":"catalog","ref":"phase/story_analysis/system@v1,phase/story_analysis/user@v1"}`.
The gate that approves input is fully provisioned; the gate that accepts output is not.

**Live DOM.** With `?tab=reasoning`, the "Agent Reasoning" tab is selected and its tabpanel text is
exactly **"No reasoning details recorded for this phase yet."** On `?tab=details` the panel reads
**"Output source: Not recorded"**.

Two small imprecisions: the finding shows `phase_results.story_analysis` with
`provenance: null, reasoning_digest: null, transcript_ref: null, resolved_prompt: null`, but those
keys are **absent** from the payload rather than null (same effect, nothing to render); and
"EVIDENCE reads No phase artifacts" did not appear in this state — the details panel instead says
"Accepted phase evidence appears below after approval."

### AGT-08 — strip and metrics row print two statuses for one phase — **CONFIRMED**

On `01a14ad7-4a0e-…/phases/story_analysis`, same page, same phase: the strip's first segment reads
**"1 | Story Analysis | RESULTS READY"** while the metrics row reads **"STATUS | Pending"**. The
segment's `aria-label` is `story_analysis — awaiting_output_review (attempt 1)`. AGT-08's
correction to COR-06 — that the detail page does *not* mask the disagreement, because the unmasked
value is on screen simultaneously at the decision point — is correct.

---

## AREAS

### ARE-01 — wizard footer asserts a target that is not set — **CONFIRMED** verbatim

Source: `emptyDraft()` hardcodes `scope: { project_id: 'demo', app_id: null, environment_id: null }`
and `engine: 'sim'` (`wizardState.ts:104-115`); `assessWizardReadiness` at `:443` pushes a *note*
rather than an *issue* when `engine === 'sim'`, so `executionIssues` stays empty; the footer
(`NewRunWizard.tsx:612-618`) prints one sentence for `issues.length === 0`. Its second branch
requires `executionIssues.length > 0` and is therefore unreachable for the default engine ✓.

Live, connected: Title and Request filled, Application **"— none —"**, Environment **"— none —"**,
and the footer reads **"Ready to launch: the plan and execution target are set."** On `step=review`
the same screen shows, together: "Test plan is complete.", "Execution readiness: Simulated
execution does not require an external application or environment.", Application **None**,
Environment **None**, footer "Ready to launch: the plan and execution target are set.", and
**Launch Test enabled**. The `demo` project prefill is confirmed on screen
(`wizard-project` value `demo`). I did not save the draft or launch.

### ARE-02 — deleted document still reports "complete" — **CONFIRMED** (by source + consequence chain)

I reproduced this without deleting shared data, since an unresolvable `document_ids` entry is
exactly what a deleted document leaves behind.

`document_ids` is **never validated anywhere**: across `features/new-test/` it is only counted
(`contextPacketCount`, `wizardState.ts:346`), mapped for display (`ReviewStep.tsx:109,176,179`),
edited (`ContextStep.tsx`), and passed straight into the launch payload
(`useWizardLaunch.ts:149,160`). `stepIssues(draft,'context')` compares the packet **count** against
`maxContextPackets` and nothing else, and `allIssues` is just the union of `stepIssues` over the
six steps. `ReviewStep.tsx:39` renders `'Name unavailable'` when a reference has an id but no
resolved name, and `:182` appends `' — Inclusion not verified'` when `doc` is undefined — both
strings verbatim as quoted.

So an unresolvable id contributes zero issues, and I demonstrated live (in ARE-01) that
`issues.length === 0` produces exactly "Test plan is complete.", "Ready to launch…" and an enabled
**Launch Test**. The stale id would then be submitted in the launch payload.

### ARE-08 — preset silently downgrades an unrecognised engine — **CONFIRMED**

Source: `ENGINES = ['sim','apex_load','loadrunner']` (`wizardState.ts:48`);
`applyGoldenConfig` sets `engine: 'sim'` as the baseline and only overrides it when the bundle
value is in `ENGINES` (`ConfigStep.tsx:77-86`); `engineLabel` falls back to the raw string
(`configView.ts:184-186`); the demo fixture pins `engine: 'apexload'` (`store.ts:2175`). Three
spellings coexist: `apex_load` (wizard/connection), `apex-load` (`ExecutionWorkspace.tsx:218`),
`apexload` (preset fixture) ✓.

Live, demo: `/golden-configs/asst-release` prints **"Engine: apexload"** (raw, unlabelled, next to
raw ids `telecom-modernization` / `app-digital-device` / `env-perf`). Clicking **Start run with this
config** lands on `/runs/new?step=config` showing **"config inherited"** and **"Draft saved"**,
with the Execution-engine radiogroup's **Simulated** option `aria-checked="true"` and "APEX Load"
unchecked. A release-certification preset would launch as a simulation.

### ARE-10 — review actions fire on one unconfirmed click — **CONFIRMED**

Source, all as cited: `review.mutate` is called straight out of `onClick` with no dialog
(`pages.tsx:435-454`); the className is
`action === 'approve' ? 'btn btn-primary' : 'btn btn-secondary'`, so every non-approve action
including `archive` is visually identical secondary; the success message is the single generic
`"Review action recorded for version {id.slice(0,8)}."` (`:466`) for all five actions; `note`
(`:277`) and `saved` (`:486`, printed at `:573`) are never reset on success; and review history
renders raw as `{r.action} · {r.actor} · {r.at}` inside a collapsed `<details>` headed "Version
identity and review history" (`:416-421`). `components/Dialog` is already imported elsewhere in
`features/cycles/` (`CycleEditor`, `CycleHandoff`, `ProviderScriptImport`) but not for these
actions, so the recommended fix is a local pattern match.

API, on the cycle the finding names (`a71b83a5763f45f588b4de1b11f063e6`, "ARE review cycle — ACC
add-a-line 26.3.1", revision 23, 8 versions) — read-only; I fired no review actions:

- the draft `postmortem` version has `available_actions: ['archive','submit']` — **archive first**,
  exactly the claimed destructive-first ordering;
- all four `approved` versions (`8ae5d150`, `c88fec80`, `caeea3e4`, `177508c8`) have
  `available_actions: ['archive']` — the only action on an approved compliance artifact is an
  unconfirmed Archive;
- review entries carry raw values exactly as quoted: `archive` / consumer UUID
  `3ebe0a0f08cb4268a05cf563e6ed477b` / `2026-10-17T18:27:06.955831+00:00`, and every `note` is `''`;
- version `8ae5d150` records `archive → restore → submit`, consistent with the end-to-end exercise
  the finding describes.

### ARE-13 — UI gates on role, server gates on scope — **CONFIRMED** (read side)

Source, all as cited: `pages.tsx:193` shows **New cycle** on
`roleAtLeast(consumer?.role ?? 'viewer','operator')`; `:516` sets `canEdit` the same way; the
Project field at `:104-113` is a free-text `<input required>`. The scope-aware helper
`canMutateAudience` does exist at `auth/RequireRole.tsx:33` and is used by `WorkItemsPage.tsx:442`,
`DocumentsTab.tsx:146,373`, `WorkItemDetailPage.tsx:252` and `NewRunWizard.tsx:290`, while
`cycles/pages.tsx` and `EnvironmentsPage.tsx:431` use role-only `roleAtLeast` ✓.

API role probes (GETs only — I did not re-run the POST rows, which create cycles and versions):

| call | admin | operator | viewer |
|---|---|---|---|
| `GET /v1/cycles` | **200, 1 cycle** | **200, `[]`** | **200, `[]`** |
| `GET /v1/cycles/a71b83a5…` | **200** | **404 "Cycle not found"** | **404 "Cycle not found"** |

Both rows match the finding exactly. Permission denial is rendered as not-found, and an operator
who is shown the New-cycle button cannot see or open the one cycle that exists.

---

## LOAD_CONVERGENCE

The Load reference tree is present at `/Users/jasonczech/Documents/GitHub/Project_Stormrunner/dashboard`
(the controller on `:4176` is no longer running, so I verified source-side claims, not its
screenshots). I scoped every count to `dashboard/src`, excluding the many `.claude/worktrees/*`
copies — a reviewer who does not will over-count by ~50×.

**Confirmed exactly, on the Orchestration side:**

| claim | measured |
|---|---|
| 63 hard-coded `text-transform: uppercase` | **63** |
| 370 literal `font-size` px declarations | **370** |
| across 18 distinct values | **18** (9, 10, 10.5, 11, 11.5, 12, 12.5, 13, 14, 15, 16, 18, 19, 20, 22, 24, 26, 28 px) |
| seven at 9px | **7** |
| 60 `<select>` elements | **60** |
| 97 `<input>` elements | **97** |
| 6 chart series tokens | **6** (`--chart-1`…`--chart-6`) |
| `--select-*` / `--bg-input` absent | **0 references** |
| 5 tokens referenced but never declared, 2 without fallback (gap #17) | **confirmed** (see CON-09) |

**Confirmed exactly, on the Load reference side:**

| claim | measured |
|---|---|
| 309 `text-transform` moved behind `--label-case` | **309** `text-transform: var(--label-case` |
| 273 `letter-spacing` moved behind `--label-tracking` | **273** `letter-spacing: var(--label-tracking` |
| tokens set to `none`/`normal` | `--label-case: none`, `--label-tracking: normal` at `index.css:58-59` |
| `themeComponents.css` layer exists | present at `dashboard/src/themeComponents.css` |
| Load has 8 chart series | **8** (`--chart-1`…`--chart-8`) |
| Load has five themes vs Orchestration's six | **4** `data-theme` blocks + default `:root` = 5 |
| `--select-*` / `--bg-input` exist in Load | `--select-arrow`, `--select-bg`, `--select-hover-bg`, `--select-menu-bg`, `--bg-input` |

**Gap #1, the document's top-ranked item, reproduces to the digit.** Parsing
`:root[data-theme='light']` from both trees: Load declares 70 tokens, Orchestration 58, **57 are
shared, and exactly 54 hold different values**. The three that match are `--accent-contrast`,
`--bg-elevated` and `--theme-toggle-active-text`. Claim: "54 of 57 shared tokens in `light` now
hold different values." Measured: 54 of 57.

**One error.** Gap #7 and §7 both state that in Orchestration `--modal-backdrop` is
"**declared twice** — `theme/tokens.css:62` and `theme/themes.css:293`". It is declared **six**
times, once per theme: `tokens.css:62`, `themes.css:67`, `:139`, `:218`, `:293`,
`apex-light.css:50`. `CONSISTENCY.md` has this right. "Consumed zero times" is correct in both
documents. The recommendation is unaffected, but §7's own argument is strengthened by the
correction: four of the six declared values are deliberately *not* black (including a pale cream
scrim for `solarized-light` and a 24%-opacity one for `apex-light`), so the hardcoded 45% black
literals override more per-theme intent than "declared twice" implies.

---

## Contradictions between documents

**1. `.engine-metric-tab` size and its SC 2.5.8 verdict — CON-02 vs A11Y-15/§6.2. A11Y is right.**

- `CONSISTENCY.md` CON-02: "`~22 px` ❌ … `.engine-metric-tab` at ~22 px **fails WCAG 2.5.8 Target
  Size (Minimum, 24×24 px) outright**."
- `ACCESSIBILITY.md` §6.2: "**Correcting the earlier estimate:** `.engine-metric-tab` is
  **23.00 px** tall, not ~22 … It misses the 24 px minimum by one pixel and survives only on the
  spacing exception", and A11Y-15 is titled "currently met".

My measurement on the real markup and CSS: **23.00 px**, 2.00 px sibling gap, **56.87 px between
centres**. SC 2.5.8's spacing exception requires 24 px between centres, so the control **passes**.
CON-02's "fails outright" is wrong, and so is its ~22 px figure. `CONSISTENCY.md` is a source-only
review (its header says "no browser") written at 13:35; `ACCESSIBILITY.md` measured in a browser at
14:30. The final report should carry A11Y's number and verdict and drop CON-02's target-size
failure claim, keeping CON-02's real point — five inconsistent tab heights because the local classes
bypass `theme/ux.css`.

**2. `--modal-backdrop` declaration count — LOAD §7 vs CON-06. CONSISTENCY is right.**
LOAD says "declared twice"; CON-06 says "defined in all six themes" and lists all six lines.
Measured: six declarations at exactly CON-06's cited lines. Both agree on zero consumers.

**3. Dialog z-index tiers — CON-06 vs LOAD §7. LOAD is closer; neither is exact.**
CON-06 "Three z-index scales" (1000 / 300 / 2000) omits `z-index: 60`, which seven of the nine
overlays use. LOAD "four z-index tiers (60, 300, 2000, and `.navigation-overlay`)" captures the
60 tier. The measured set among overlays is 60 / 80 / 300 / 2000, plus 1000 on the portal host.

**4. Overlay count, 9 vs 10 — not a real contradiction.**
LOAD §7 tabulates nine feature overlays and treats `.navigation-overlay` separately; CON-06
tabulates ten by including it. Both state their membership. Worth normalising for the final report.

**5. AGT-08 vs COR-06 on masking — AGT-08 is right, and it is already flagged in-document.**
COR-06 says the run-detail page "happens to mask the disagreement" for the 12 gated runs. AGT-08
says the mask covers the strip only and the unmasked `pending` is on screen at the same time.
I confirmed AGT-08: strip "RESULTS READY", metrics row "STATUS Pending", same page, same phase.
AGT-08 already says it is correcting COR-06, so this is a resolved conflict rather than an open one
— but the final report must not repeat COR-06's masking sentence unqualified.

**6. Fleet size across documents — drift, not error.** CORRECTNESS 45 (19/18/8, sums correctly),
AGENT_UX 48, now 50 (23/18/9). Each document timestamps its snapshot and CORRECTNESS explicitly
flags the drift. The ratios that matter survived re-derivation (COR-04's 10, COR-06's 6 and 12).

**7. A11Y-06 §3.2 "exactly one live region" vs its own §3 source count — internal, minor.**
"Exactly three `aria-live` attributes in the whole dashboard" is a source count and is correct.
"Live regions present: exactly one" is a runtime count and holds on the run route but not on the
phase route with the editor mounted, where CodeMirror adds three `.cm-announced` regions. No
effect on the finding.

---

## Findings I would drop entirely

Nothing should be dropped wholesale — every finding I examined has a real mechanism behind it.
What follows should be **reclassified or reworded**, which is a different thing:

- **A11Y-05's headline claim**, not the finding. "The approvals inbox drops `j`/`k` keystrokes at
  triage speed" should not ship as stated: at every realistic key interval, including auto-repeat
  and a deliberate 0 ms `setTimeout` gap, all presses land. Keep the finding as a latent
  same-task race at **P3** and delete the "six presses advance one row / five advanced two"
  numbers, which I could only reproduce by dispatching six events inside a single JS task.
- **A11Y-09's "adding 14 stops to the Tab order"** should be deleted outright. The options
  implement a correct roving tabindex (1 × `tabindex="0"`, 14 × `"-1"`). Keep the real defect —
  `aria-activedescendant` on a container that can never take keyboard focus — at **P3**, and drop
  the half of the recommended fix that is already implemented.
- **CON-02's target-size failure claim** should be deleted and replaced by a cross-reference to
  A11Y-15. The finding's consistency argument does not need a WCAG failure to stand.
- **LOAD gap #7's "declared twice"** should be corrected to six, which strengthens its own
  argument.
- **The CONSISTENCY verdict's "~240" `role="alert"`/`role="status"` sites** should be corrected to
  185, or restated as "≈226 rendered instances including `ProblemCard`'s internal `role="alert"`".
  Likewise `ProblemCard` 42 → 41 and `Dialog` 22 → 23.
- **COR-07 needs a locale qualifier** ("one day early at negative UTC offsets"), not a downgrade.

---

## Findings I did not reach

Priority ran out before these. None of them is implicated by anything above.

**ACCESSIBILITY** — A11Y-02, A11Y-08, A11Y-10, A11Y-11, A11Y-12, A11Y-13, A11Y-14, A11Y-16,
A11Y-17. Also §5's keystroke counts (67 for `/runs/new` → Review, 5 for the output gate), §6.1's
reflow table, §7's seven verified passes, and §10's route × theme coverage grid. I did not re-run
the axe sweep itself, so the 140-node total is checked only for internal arithmetic, not
reproduced.

**CONSISTENCY** — CON-05, CON-07, CON-08, CON-10, CON-11, CON-12, CON-13, CON-14, CON-15, CON-16,
CON-17. CON-04 was already downgraded by the lead and I saw nothing that contradicts that.

**CORRECTNESS** — COR-12, COR-13, COR-14, COR-15, COR-16, COR-18, COR-19, COR-20, COR-21, COR-22,
and COR-11 Repro A (the demo text contradiction). COR-01/02/03 were pre-confirmed by the lead and
I found no contradiction with them. I did not re-derive the "Verified correct" arithmetic table.

**AGENT_UX** — AGT-02 through AGT-07 and AGT-09 through AGT-16, except AGT-08 which I confirmed.
The state × surface matrix (§1), the four-facts analysis (§7) and §8 were not exercised.

**AREAS** — ARE-03, ARE-04, ARE-05, ARE-06, ARE-09, ARE-11, ARE-12, ARE-14 through ARE-27, and the
per-area scorecard. ARE-07 was pre-confirmed by the lead. For ARE-10 and ARE-13 I verified the
read-only and source-level evidence but deliberately did not fire the mutating probes (review
actions, `POST /v1/cycles`, `POST /versions`) on shared state.

**LOAD_CONVERGENCE** — §§2, 4–6, 8–14, 16–19 in detail, the three-way split (a/b/c) and §20's
token block. I verified the counts behind gaps #1, #3, #4, #6, #7, #13, #17 and #22 and spot-checked
the reference tree's structure, but not the rendered-screenshot comparisons, since the Load
controller on `:4176` is no longer running.

**FINDINGS-DRAFT** — the lead's own findings were out of scope for re-verification per the brief;
I noticed nothing that contradicts L-01, G-02, G-03, H-01 or R-01. H-01's subject matter is
adjacent to what I saw on Home in demo ("FAILURES 6" alongside other failure counts), consistent
with the finding.

---

## Changes I made to the running environments

Read-only wherever possible. The exceptions, all additive and all in the wizard:

- **Connected (`:4175`)** — typed a Title and Request into a New Test draft to reproduce ARE-01.
  The wizard auto-assigned a draft id (`?draft=82b9e331…`). I did **not** click Save Draft and did
  **not** launch. No gate decision, review action, cycle mutation or deletion was performed
  anywhere.
- **Demo (`:4173`)** — clicked "Start run with this config" on preset `asst-release` to reproduce
  ARE-08, which created local draft `draft-103` and showed "Draft saved". Nothing launched.
- Approvals queue selection moved repeatedly via `j`/`k` while testing A11Y-05. Selection is URL
  state only; no gate was actioned.
- Servers on `:2024`, `:4173` and `:4175` were left running and I did not sign out of `:4175`.
- Scratch files written only under my scratchpad, prefixed `v-`/`v_` to avoid colliding with the
  earlier reviewers' files already there.
