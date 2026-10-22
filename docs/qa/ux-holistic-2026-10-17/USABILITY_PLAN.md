# Usability and presentation — remediation plan

Scope: the usability and presentation findings from the five area sweeps
([CONSISTENCY](CONSISTENCY.md), [AREAS](AREAS.md), [AGENT_UX](AGENT_UX.md),
[ACCESSIBILITY](ACCESSIBILITY.md), [CORRECTNESS](CORRECTNESS.md)), re-scoped against the state of
`codex/holistic-ux-remediation-2026-10-17` @ `822b412` as it runs today — plus presentation
parity with APEX Load, which is the reference product (workstream 5).

**The headline is that most of this is already done.** I re-probed the running build across all
14 primary routes before writing this, and the presentation layer the sweeps complained about has
largely been rebuilt: one `h1` per route with no heading skips, sentence case throughout, no
glyphs in button labels, a shared `ux-state-card` for empty/loading/error states, shared table
toolbars with sortable headers, timestamps carrying year and zone, disabled controls at 4.63:1,
and zero axe violations on the routes and themes I swept.

So this plan is deliberately short. It covers **one new defect introduced by the remediation**,
**three findings that remain open**, **a small presentation tail**, **a triage pass** over the
roughly sixty P2/P3 sweep findings nobody has re-tested since the branch landed, and **the
remaining gap to APEX Load** — which, measured rather than assumed, comes down to the typeface and
the sidebar's default state. Writing a plan
over the original sweep lists without re-scoping would have produced a backlog that is mostly
already complete.

> **Status at time of writing (17 Oct, 18:05).** The remediation worktree has uncommitted work in
> flight, touching `routers/pipelines.py`, `services/pipeline_read.py`, `app/security.py`,
> `theme/apex-light.css` and a new `ApplicationLabel.tsx` — i.e. workstream 2a, 2b and parts of
> workstream 3, started within minutes of the re-review. On inspection the 2a fix is the right one:
> `identity: CurrentIdentity` on the route, `principal_id` threaded into `resume_gate` with
> validation, and `run_config["langgraph_auth_user_id"]` stamped after the durable contract check.
> **Re-scope workstreams 2 and 3 against that branch once it lands** rather than against this
> document; nothing in it was runtime-verified, because the files were changing as I wrote.
> Workstreams 1, 4 and 5 are untouched by that work.


---

## Workstream 1 — Fix the target-size regression on the runs grid · P1 · ~half a day

The remediation closed AGT-15 by rendering the compact phase strip on `/runs` that
`PARITY.md` row 3a had always claimed. The micro-viz is genuinely useful — it restores per-phase
state to a column that was previously bare `n/m` text — but each segment ships as a real,
focusable `<button>`:

```
tag            BUTTON            tabIndex 0        onclick present
size           12 × 6 CSS px
spacing        15 px centre-to-centre (3 px gap)
count          42 on a six-run grid (7 phases × 6 runs)
accessible name "Story Analysis — Complete (attempt 1)"
```

WCAG 2.5.8 (Target Size, Minimum, AA) wants 24 × 24 px, or a spacing exception where a 24 px
circle centred on each target does not intersect its neighbour's. At 15 px centre-to-centre those
circles overlap, so **the exception does not apply and this fails outright** — unlike the
15–23 px controls A11Y-15 catalogued, which survive on spacing. It also puts 42 six-pixel tab
stops between a keyboard user and the far side of the table.

**Do this.** Keep the visual; drop the per-segment interactivity.

1. Render the strip as one non-interactive graphic per row — `role="img"` with an `aria-label`
   summarising all seven phases, which is the pattern the run-detail summary line already uses
   ("Story Analysis: Skipped. Test Planning: Failed. …").
2. Make the row's existing run link the single target. One tab stop per row, not seven.
3. If per-phase navigation is wanted from the grid, put it behind the row's overflow menu rather
   than seven 12 × 6 px buttons.

**Acceptance.** `/runs` has one tab stop per row for the strip; no element under
`.phase-seg` is focusable; a screen reader announces the whole phase sequence from one label; the
rendered strip is visually unchanged.

---

## Workstream 2 — Close the three findings that are still open · P1–P3 · ~1 day

### 2a. Gate approvals still record `actor: "unknown"` · P1

Detail and diagnosis in [RE-REVIEW.md](RE-REVIEW.md). The mechanism exists and works elsewhere in
the branch (`/cycles` renders "Owner: apex-admin"); it is never reached because
`routers/pipelines.py:737 resume_gate` is the only mutating pipeline route that does not take
`identity: CurrentIdentity`.

1. Add `identity: CurrentIdentity` to the route.
2. Thread it into `PipelineReadService.resume_gate`.
3. Set `configurable["langgraph_auth_user_id"]` on the config built at `pipeline_read.py:1778`,
   mirroring `rerun_pipeline`'s existing `principal_id` plumbing.
4. Render the actor in the APPROVALS panel, which already has the slot.

**Acceptance.** A gate decision made as `apex-admin` records that consumer in
`phase_results[phase].approvals[].actor`, and the panel shows it. A decision made with the dev
key still records something truthful rather than a fabricated name.

### 2b. Rejected bodies on native routes return 500, not 422 · P2

`apex/app/security.py:510` maps `_RequestInputRejected` to 422, but native LangGraph routes read
the body inside an anyio TaskGroup, so the exception arrives wrapped in an `ExceptionGroup` and
the bare `except` misses it. Reproduces on `POST /assistants/search` with any body. Pre-existing,
not a regression — but it is the real cause of the original ARE-07 symptom, and it currently
prevents the dashboard from telling "your request was invalid" from "the server broke".

Use `except*`, or unwrap `ExceptionGroup` before matching. **Acceptance:** a malformed body on
`/assistants/search` and `/threads/*` returns 422 with its detail; the existing 422 tests still
pass.

### 2c. Demo mode still promises live activity it cannot deliver · P3

Connected mode now says the honest thing — *"No live activity was captured in this session. Open
Agent reasoning for the durable evidence and full transcript."* That copy is conditional on the
run being finished, so demo's `/runs/run-active`, an explicitly executing run, still shows *"No
live activity for this phase yet"* and *"Activity streams in while the run is executing."*

Make the copy conditional on the transport actually being able to attach, not on run state. In
demo mode, say plainly that the bundled dataset records no live stream and point at the durable
transcript. **Acceptance:** no surface in demo mode promises streaming that the demo transport
cannot produce.

---

## Workstream 3 — Presentation tail · P3 · ~half a day

Small, verified, and cheap. Worth doing as one pass rather than tracking individually.

| Item | Observed | Change |
|---|---|---|
| Application renders two ways on one screen | Run detail header resolves "Application **ACC**"; the Run context panel beside it shows `02971bd36e5347dcb7d8bebc7abcf34b` | Resolve the catalog name in Run context too, with the id behind the existing copyable-identity affordance |
| Casing escaped the sentence-case sweep | "Reasoning Digest" and "Tool Calls" in the Agent reasoning panel, against sentence case everywhere else | Sentence case, via the shared label token |
| apex-light chart series below 3:1 | `--chart-1` `#00a8e0` at 2.74:1 and `--chart-3` `#e5920a` at 2.49:1 against the card surface (dark 3.95, monokai 3.19 both clear) | Darken those two apex-light values until ≥3:1; patterns and dash arrays already carry the encoding, so this is polish not a blocker |
| Queue announcement is long | Every `j`/`k` reads run, gate, all seven phase states and transport — roughly forty words per keystroke | Announce run, gate kind, phase and position ("3 of 12"); leave full phase detail to the workspace |

---

## Workstream 4 — Triage the unverified tail · ~1 day, then scope

Roughly sixty P2/P3 findings across the five sweeps have **not** been re-tested since the branch
landed. Their remediation doc claims coverage of most, and their own suite (1,098 tests / 135
files) passes — but three of their claims did not hold when I checked them, so the claims are not
sufficient evidence on their own.

Rather than re-test all sixty by hand, run a scripted pass that answers most of them at once, in
the shape I used for this plan: drive the built bundle, walk every route, and assert the
properties the findings describe.

**Cover, in priority order:**

1. **Cycles (ARE-10…ARE-21, 12 findings).** The only area I gave a "not production-ready"
   verdict and the only one I did not re-test beyond a smoke check. Its list page now renders
   with shared pagination and "Owner: apex-admin", which is encouraging, but the review-action
   confirmations, the "Cancel editing" guard bypass and the unstyled dialogs all need a look.
2. **Resources (ARE-22…ARE-27).** Document preview, evidence run selection, saved-query identity.
3. **Remaining agent-UX (AGT-06, AGT-13…AGT-16).** AGT-06 in particular — whether `timeout`,
   `denied` and `running` tool calls still get a green "success" badge — needs seeded data with
   those statuses, which this environment does not produce.
4. **Consistency tail (CON-07, CON-12, CON-16, CON-17)** and **accessibility tail
   (A11Y-07, A11Y-08, A11Y-12, A11Y-13)**.

**Acceptance.** Every sweep finding carries a verdict of closed, open, or not-testable-here with
a reason — the same discipline [VERIFICATION.md](VERIFICATION.md) applied to the original set.
Only what comes back open gets scheduled.

---

## Workstream 5 — Presentation parity with APEX Load · P2 · ~2–3 days

APEX Load is the reference. I measured the current gap rather than assuming it, and **the token
work is essentially finished** — what remains is small, specific, and mostly about two things.

### Already converged — do not redo

Comparing Load's current theme layer against the remediation branch, per theme:

| Theme | Shared tokens | Values differing | Load-only tokens missing |
|---|---|---|---|
| `:root` / dark | 103 | **4** | 0 |
| `light` | 70 | **0** | 0 |
| `solarized-dark` | 70 | **2** | 0 |
| `solarized-light` | 64 | **0** | 0 |
| `monokai-dimmed` | 64 | **0** | 0 |

That is **130 value divergences reduced to 6, and 34 missing tokens to zero**. The `light` theme —
which was the worst case at 54 of 57 tokens differing, Orchestration's indigo glassmorphism against
Load's cobalt — is now byte-identical. Sentence case, the flat shell, form surfaces,
`color-scheme`, the six-step type scale and the eight-colour chart palette are all in. Sidebar
widths already match Load exactly: 240 px expanded, 64 px collapsed.

### 5a. Adopt Load's typeface — the one genuinely visible difference

Two of the six remaining divergences are the font tokens, and they affect every character on
screen:

```
--font-sans   Load  'ATT Aleck Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, …
              Orch  'Inter Variable', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif

--font-mono   Load  ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', …
              Orch  'IBM Plex Mono', ui-monospace, Menlo, monospace
```

Orchestration already self-hosts its fonts — 32 `woff2` files ship in the bundle (Inter, Nunito,
IBM Plex Mono) — so the mechanism is in place; it is loaded with the wrong faces. Load bundles
ATT Aleck Sans from `pkg/api/static/fonts/att/` with four `@font-face` declarations in its
`index.css`.

1. Bundle ATT Aleck Sans the way Load does and set `--font-sans` to Load's exact stack.
2. Set `--font-mono` to Load's exact stack, which is system mono — this *removes* a dependency
   rather than adding one, and drops the IBM Plex Mono files from the bundle.
3. Drop Inter and Nunito once nothing references them; that is roughly 32 font files out of the
   bundle for one brand face in.

**Decide first:** ATT Aleck Sans is a licensed brand face. If Orchestration cannot redistribute
it, do not silently diverge — match Load's *fallback* stack exactly after the brand face, record
the substitution in the UX contract, and accept that the two products differ only where the
licence requires. That is a deliberate, documented exception rather than drift.

**Acceptance.** `--font-sans` and `--font-mono` resolve to the same stacks in both products, or
to a documented exception; no Inter/Nunito/IBM Plex assets remain in the bundle; a side-by-side
capture of the same screen in both products shows the same typeface at the same sizes.

### 5b. Default the sidebar to the icon rail, and expand as an overlay

Load (`dashboard/src/components/Sidebar.tsx:210`):

```js
// Collapsed to the icon rail by default; expanding opens the full sidebar as
const [collapsed, setCollapsed] = useState(true)
```

Orchestration (`apps/dashboard/src/components/layout/Sidebar.tsx:224`):

```js
const [collapsed, setCollapsed] = useState(readStoredCollapsed)  // → false on a fresh browser
```

Two differences, not one:

1. **Default state.** Load starts collapsed to the 64 px rail; Orchestration starts expanded.
2. **Expansion model.** Load expands as an **overlay** (`.sidebar-shell.overlay-open`) that closes
   on outside click or Escape, and clicking empty rail space opens it. Orchestration expands by
   **pushing** the main column, so the content reflows.

Change both. Default `collapsed` to `true` while still honouring a stored preference once the user
sets one, and make expansion an overlay with Load's dismissal behaviour. The widths need no change.

**Watch the regressions this can cause.** The sidebar is where the pending-review badge lives, and
a prior audit (`sidebar-audit-2026-09-14`, P1) found collapsed mode had previously hidden
destinations and the approval count. So:

- every destination stays reachable from the rail, with its accessible name;
- the review badge stays visible on the rail and keeps its "N tests need review" accessible name;
- the overlay traps focus, closes on Escape, and returns focus to the rail trigger;
- the mobile drawer is unaffected.

**Acceptance.** A fresh browser profile lands on the 64 px rail in both products. Expanding
overlays the content rather than reflowing it. The badge is legible on the rail. Keyboard users can
reach every destination without expanding.

### 5c. Leave the four run-calendar tokens alone, and say why

The other four divergences are `--run-calendar-ad-hoc-text` and `--run-calendar-cron-text` in dark
and solarized-dark. Orchestration has **no run-calendar surface** — the accessibility sweep
recorded it as unreachable in this build — and Orchestration's values are the lightened,
higher-contrast ones. Converging them would copy a worse value onto a surface that does not exist.
Record them in the contract's domain-exceptions list instead.

### 5d. Put the parity rules in the contract

`docs/design/APEX_UX_CONTRACT.md` was rewritten to v2 on the remediation branch and is a genuine
improvement — it now encodes naming, state components, dialog and library-control rules. It does
**not** mention the sidebar default or the typeface, which is why both drifted. Add to the
Shell row: Load's collapsed-by-default icon rail and overlay expansion, and the shared font
stacks with any licence exception named explicitly. A parity rule that is not in the contract is a
parity rule that will drift again.

---

## Sequence and effort

| | Workstream | Why this order | Effort |
|---|---|---|---|
| 1 | Target-size regression | A fix introduced it; it fails a success criterion outright, and the longer the grid ships this way the more it is relied on | ~0.5 day |
| 2 | The three open findings | 2a is the one unfixed P1 and the only thing standing between this product and a usable governance audit | ~1 day |
| 5a | Typeface parity | The single most visible difference between the two products, and blocked on a licence decision — start that decision now even if the work lands later | ~1 day + decision |
| 3 | Presentation tail | Cheap, visible, and best batched into one commit | ~0.5 day |
| 5b | Sidebar rail default | Changes the first thing every user sees, and carries real regression risk around the review badge — do it with the tail, not alone | ~1 day |
| 4 | Triage the unverified tail | Scopes whatever remains; its output decides if there is more after this | ~1 day, then scope |
| 5d | Contract update | Last, so it records what actually shipped | ~2 hours |

About **five days** in total. Workstreams 1, 3, 5a and 5b are dashboard-only and 2a/2b are backend,
so the two tracks run in parallel. Start the ATT Aleck Sans licence question on day one — it is the
only item here with an external dependency, and 5a cannot close without it.

Sequencing note: 5b changes the default state of the sidebar, and workstream 1 removes 42 tab stops
from the runs grid. Both alter keyboard traversal, so land them in that order and re-run the
keyboard walkthrough once, after both.

## What this plan deliberately does not do

- **Re-plan work already finished.** CON-01, CON-02, CON-05, CON-13, CON-14, CON-15, A11Y-01,
  A11Y-06, A11Y-10, A11Y-11, A11Y-16, AGT-02, AGT-09, AGT-10, AGT-11, AGT-12 and the LOAD
  convergence items were verified closed against the running build and are not listed as work.
- **Re-open terminology.** CON-03 is effectively closed: "work item" and "golden config" are gone
  from the UI. The surviving "pipeline" strings are an assistant's own name on `/test-presets`
  and the "Pipeline prompts" category on `/prompts`, which names the graph rather than the test —
  a defensible distinction, not drift.
- **Chase chart hue separation.** Adjacent series pairs still measure 1.07–1.49:1, but patterns
  and `stroke-dasharray` now carry the encoding, so colour is no longer load-bearing. The
  accessible outcome is met; tightening hues further is cosmetic.
- **Certify WCAG.** Workstream 1 fixes a specific, measured 2.5.8 failure. Nothing here is a
  conformance audit.
- **Re-converge the theme tokens.** Five of six themes are already identical to Load and none is
  missing a token. Only the two font stacks and four tokens for a surface Orchestration does not
  have remain, and the latter should stay as they are.
