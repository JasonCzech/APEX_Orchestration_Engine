# New-test workflow — concentrated UX review

18 October 2026 · `codex/holistic-ux-remediation-2026-10-17` @ `2b81afa` · connected production
build behind the same-origin proxy · admin · apex-light · 1440×900, with 1024 and 768 checks.

Scope: the end-to-end path an operator actually walks — Home → the six wizard steps → launch →
run detail → gate decision → timeline. Judged on usability, layout and whether the feature set is
complete enough to do the job.

---

## Verdict

**The authoring flow is the strongest part of this product, and it cannot complete.** Steps 1–6
are genuinely well designed: readiness is honest, consequences are stated before you commit,
prompt provenance is visible, and the Review step is a real launch preview with an Edit affordance
on every section. Then "Launch test" fails with a bare status code, twice, for two different
reasons, neither explained — including one that the server had already described precisely.

Downstream of a successful launch the run screens are equally strong, with one structural flaw:
**three panels on the gate screen read the durable phase record while the review panel beside them
reads the gate payload**, so the same screen simultaneously lists two artifacts and says there are
none.

Two feature gaps stand out: there is no workload contract anywhere in authoring, and the
environment-target precondition that blocks launch is invisible and unmanageable in the UI.

---

## P1 — The happy path fails at launch, and the error says nothing useful

**Repro.** Wizard → fill Title, Request, Project `demo`, Application `ACC`, Environment
`PERFHYPER2` → Config (Simulated) → Review → Launch test.

**Expected.** The run launches, or the UI explains what to fix.
**Actual.** `Launch failed: Pipeline launch failed (404)` — screenshot `08`.

The server's own response was precise:

```json
{"type":"about:blank","title":"environment target not found","status":404}
```

The UI discarded that and substituted a generic message with a raw status code. This is
especially avoidable because **the pattern already exists in this same wizard**: the
scope-authorization banner surfaces the server's own words ("Pipeline scope is not authorized")
verbatim, with guidance. The launch error should do the same.

Confirmed by isolating the cause:

```
POST /v1/pipelines  with    environment_id  → 404 "environment target not found"
POST /v1/pipelines  without environment_id  → 202 Created
```

`PERFHYPER2` ships from `scripts/seed_catalog.py` with `target_approved: false`,
`target_version: 0`. So **selecting the only environment the wizard offers makes the launch fail,
while leaving it blank succeeds** — the opposite of what the UI's affordances suggest.

**Recommended change.** Surface `problem.title` from the response in the launch error, with the
failing field named and a link to the resource that fixes it. Keep the status code in the
technical disclosure, not in the headline.

## P1 — Readiness says "Plan is valid" for a launch that cannot succeed

The Review step's Readiness card reads:

> Test plan is complete.
> Execution readiness: Simulated execution does not require an external application or environment.

Both sentences are true and the second is reassuring — but the backend still validates the
*selected* environment's target and rejects the launch. Readiness validates the form, not the
launch. This is the same class of defect the 2026-09-14 audit raised ("'All required sections look
ready' means form validity, not verified execution readiness"), which the remediation otherwise
fixed well; the environment target simply is not among the things readiness checks.

**Recommended change.** Have the readiness check resolve the selected environment's target state
and either block with a named reason, or — since the copy already says simulated runs do not need
an environment — stop sending `environment_id` for simulated runs.

## P1 — Environment target approval is invisible and unmanageable

The precondition that blocks the launch has no presence in the UI at all. On
`/environments/{id}` the word "target" **does not appear**; the available controls are
`Copy`, `Edit project name`, `Edit`, `Rescan`. There is no target status, no approval action, and
no explanation.

So an operator who hits the 404 has no path to resolve it inside the product, and no way to learn
that an environment needs an approved target before it can be used. For a seeded dev environment
this means the documented quickstart produces an environment that cannot be launched against.

**Recommended change.** Show target state on the environment detail page (approved / not approved,
version), provide the approval action under the existing role rules, and show the state in the
wizard's Environment picker so an unusable choice is visible before it is selected.

## P1 — The gate screen contradicts itself about the phase under review

Screenshot `10`. On one screen, for the Execution phase:

| Panel | Says |
|---|---|
| Review panel | `Artifacts: engine_results, results.json` |
| Review panel | `Engine run sim-ce2a92c63cdaae2c (sim) completed; SLA passed. KPIs: error_rate=0.004, p95_ms=212, tps_avg=24, vusers_peak=10` |
| Metrics row | `Evidence: No phase artifacts` |
| Metrics row | `Elapsed time (includes reviews): —` · `Started: —` |
| "What happened" | `No operator-readable summary has been recorded for this phase.` |
| Timeline | 8 events covering four phases — **Execution absent entirely** |

One root cause: the review panel reads the pending gate payload, which is populated; the metrics
row, the summary panel and the timeline read `phase_results[execution]`, which is not written until
the decision is accepted. Their triage records AGT-01 as closed because the gate payload now
carries evidence — that half is real and visible. The other half, the panels beneath it, still read
the empty durable record.

The timeline instance matters most: a reviewer consulting the audit trail at decision time sees no
record of the phase they are being asked to approve.

**Recommended change.** Have the phase panels and the timeline prefer the pending gate payload for
the phase currently under review, falling back to the durable record. Where a value genuinely is
not yet known (started/elapsed), say "not recorded until the decision" rather than printing an
em dash that reads as missing data.

## P2 — There is no workload contract anywhere in authoring

Across all six steps the operator can specify: title, request (free text), project, application,
environment, engine, phase set, review policy, tickets, documents, prompt overrides.

They cannot specify, and never see: **virtual users, duration, ramp profile, think time,
throughput target, acceptance thresholds, or hard-stop criteria.** Selecting APEX Load as the
engine adds contextual readiness copy — *"APEX Load will use the project default connection unless
a mapped release is selected"* — but no workload controls.

My own Request text carried "85 TPS aggregate across 300 employee sessions" as prose, which is
exactly the problem: the central parameters of a performance test are free-text for an agent to
infer, with no structured field, no validation, and nothing to compare a later run against.

This is a deliberate architecture — `script_scenario` is meant to produce the workload — but the
operator persona is a performance engineer, and the authoring flow gives them no way to state the
target or review what will be used before committing to 14 review pauses.

**Recommended change.** Add a workload section to Config, pre-filled by the agent where it can
infer, editable, and shown in the Review preview. Even read-only display of the derived workload
at Review would close most of the gap.

## P2 — "N items still need attention" never says which

The counter is live and correct — 3 → 1 as fields are filled, and it switches to "Plan is valid"
when satisfied. But on a six-step wizard it never names the outstanding item, and no field is
marked. On step 1 with five fields that is a short hunt; by step 6 it would not be.

**Recommended change.** Make the count a list, or link it to the first unsatisfied field. The
Review step's per-section `Edit` affordance is the right model to reuse.

## P3 — Smaller observations

- **No retry on launch failure.** The error is `role="alert"` (good) and the Launch JSON
  disclosure shows the exact payload (also good), but there is no Retry control and nothing
  states whether re-clicking is safe. For an operation with an idempotency key, say so.
- **A content error surfaced as a gateway error.** The second launch attempt returned `502`; the
  backend logged `pipeline.launch_reconciliation_failed` with `error_type=UnprocessableEntityError`.
  A 422-class problem reaching the user as "Bad Gateway" semantics is misleading.
- **Raw application UUID in three places** — Home's Recent runs table, the Prompts step chip, and
  the gate's Reviews panel actor. The Review step does this correctly (friendly name + full id +
  Copy); the others show the id alone. This is the identifier-display issue already on record.
- **Route params keep the old vocabulary** — `step=work-items` under a tab labelled "Tickets",
  `step=context` under "Knowledge". Harmless until someone shares a URL or reads a support log.
- **Run context is six rows to say almost nothing** — four of the six read "Not recorded" (Load
  application, Load release, Mapping revision, Source), occupying a third of the viewport.
  Collapse the unrecorded ones behind a disclosure.
- **Execution's system prompt is nine lines explaining that it does nothing**, shown at decision
  time. Correct as provenance, noisy as decision support; collapse it by default.

---

## What is excellent, and should not be changed

- **Consequence disclosure before commitment.** *"14 planned review pauses: 7 prompt and 7 output
  reviews. Revisions may require additional reviews."* on the Config step is the single best piece
  of copy in the product — it tells an operator what they are signing up for while they can still
  change it.
- **Review is a true launch preview.** Every section carries an `Edit` that returns you to it,
  phases render as an ordered chain, IDs are shown with Copy, and the Launch JSON is available
  under a technical disclosure.
- **Readiness separates plan validity from execution readiness** and adapts to the chosen engine.
  The idea is right; only its environment check is missing.
- **Prompt step.** Per-phase focus, system/user/application prompts each with a provenance chip
  (`catalog@v1`, `empty`), and "Override for this run" — scoped language that makes clear this is
  a run override, not a catalog edit. The primary action is "Review test", not "Launch".
- **The gate decision screen.** KPIs at the decision point, the next phase named in the
  consequence copy ("continues to **Reporting**"), the resolved prompt with provenance, output
  source labelled "simulator", and an action bar whose primary button carries its own scope caveat
  ("does not approve the test result or a release").
- **Run detail prioritises the decision.** When a gate is open the section nav leads with
  **Review**, and the Overview card offers "Review pending decision" directly.
- **Timeline** is a clean audit trail with durations and an explicit provenance note.
- **Autosave and draft resume.** The draft is created silently, "Draft saved" confirms it, and the
  Scope step offers saved drafts with project and timestamp.
- **Accessible summary lines.** Each run screen opens with a screen-reader sentence carrying run,
  gate, phase states and transport status.
- **Responsive behaviour is clean.** No horizontal page scroll at 1440, 1024 or 768; the sidebar
  becomes a drawer at 768 and the two-pane review stacks.

---

## Layout notes

- **Step 1 fields span the full 1376 px column.** A single-line Title input 1272 px wide is far
  past a comfortable measure; the eye travels the whole viewport for a short value. Cap form
  fields at a readable width (~640 px) and use the freed space for the guidance column that
  currently holds one sentence.
- **Steps 3 and 4 are sparse** — a card about a third of the viewport tall, then empty space to the
  footer. They are correctly optional and light, but the imbalance makes them feel unfinished
  beside steps 2 and 6.
- **At 768 the approvals queue stacks above the workspace**, so the decision controls sit a long
  scroll below the queue. Consider collapsing the queue to a summary row on narrow widths.

## Coverage and limits

Walked: Home, all six wizard steps, draft autosave and resume, launch (twice, both failed), run
detail Overview, the gate decision screen, Timeline, and responsive behaviour at 1024 and 768.
Runs used for the post-launch screens were created through the API because the wizard launch could
not complete.

Not covered: the Workspace tab's per-phase tabs in depth, artifact viewer, compare, a completed
run's reporting/postmortem output, APEX Load as a live engine (no connection configured), and
keyboard-only traversal of the wizard. Prior audits cover the wizard's information architecture;
this review deliberately did not re-tread it.

---

# Addendum — theme set, tab styling and the collapsed rail

Four items raised after the walkthrough, each verified against the running build and against
Load's source.

## A1 — Remove the CTx theme and default to `light` · P2

Orchestration ships **six** themes; Load ships **five**. The extra one is the compatibility theme,
and it is also the default:

```
Orchestration  apex-light ("Light (CTx)")  dark  light  solarized-dark  solarized-light  monokai-dimmed
Load                                    :root(dark)  light  solarized-dark  solarized-light  monokai-dimmed
```

`theme/useTheme.ts:31` sets `DEFAULT_THEME = 'apex-light'`, and `public/theme-init.js` hard-codes
`var theme = 'apex-light'` for cold loads. Load's `index.html` ships `data-theme="light"`.

So dropping CTx and defaulting to `light` does two things at once: it removes the one theme Load
has no counterpart for, and it makes the default identical across the two products. After this
change the theme sets match exactly, name for name.

**Footprint is small and contained.** `theme/apex-light.css` is 148 lines, and `apex-light` is
referenced in 7 files: `useTheme.ts`, `theme-init.js`, `themeComponents.css`, `theme/index.css`,
`accessibility.css`, `features/settings/SettingsPage.tsx`, `useTheme.test.tsx`.

**Do not forget the migration.** `apex.theme` is persisted in `localStorage`, so existing users
carry `"apex-light"`. `getStoredTheme()` validates against `THEMES` and falls back to
`DEFAULT_THEME`, so removing the entry degrades cleanly to `light` — but confirm that path rather
than assuming it, and make sure `theme-init.js`'s allowlist is updated in the same change or cold
loads will flash.

Also update `docs/design/APEX_UX_CONTRACT.md`, whose domain-exceptions clause currently reads
*"Orchestration retains … its Light (CTx) compatibility theme"*. That sentence is the reason the
theme survived; remove it with the theme.

## A2 — Tabs are filled, bordered boxes; Load's are clean underlines · P2

Orchestration adopted Load's class name but not Load's rule. Both products style
`.ux-underline-tabs`, and the selected-tab declarations differ in three ways:

**Load** — `dashboard/src/topbarNavigation.css:147`
```css
body .ux-underline-tabs [role='tab'][aria-selected='true'] {
  background: transparent;
  box-shadow: inset 0 -3px 0 var(--brand-highlight, var(--accent));
  color: var(--text-primary);
}
```

**Orchestration** — `apps/dashboard/src/theme/themeComponents.css:72`
```css
body .workspace-tabs.ux-underline-tabs .workspace-tab[aria-selected='true'] {
  border-bottom-color: var(--brand-highlight);
  color: var(--accent-text);
  background: var(--accent-soft);     /* Load: transparent */
  font-weight: 700;
}
```

Measured on `/context` in apex-light, the selected tab renders
`background: rgb(229, 246, 252)` with a 3 px bottom **border** — a filled, bordered block. Load
paints no fill and draws its indicator with an **inset box-shadow**, which keeps the tab's box
model clean and the label in primary ink.

**Change.** Adopt Load's three declarations verbatim: `background: transparent`,
`box-shadow: inset 0 -3px 0 var(--brand-highlight, var(--accent))`, `color: var(--text-primary)`,
and drop the `border-bottom` approach. Keep the unselected state's transparent indicator so
nothing shifts on selection.

This is also worth a wider sweep: the run-detail section nav (Review / Overview / Workspace /
Timeline) renders as `.btn btn-secondary` pill buttons rather than tabs at all, so the same screen
shows two different "set of choices" treatments.

## A3 — The collapsed rail has no usable hover text · P2

Every rail item does carry a `title`, so a native tooltip technically exists — but for a 64 px
icon-only rail that is the entire navigation affordance, and `title` is the weakest possible
version of it: roughly a one-second delay, no keyboard equivalent, no styling, and it cannot be
positioned clear of the rail.

Load does not rely on `title` for its rail; its expanded overlay is the discovery mechanism, and
it opens on click anywhere in the empty rail.

**Change.** Either adopt Load's model — clicking empty rail space expands the overlay — or render
a real tooltip on hover **and** focus, positioned to the right of the rail, with the item's label.
Keep `title` as the fallback.

## A4 — The last two rail icons are identical, and Settings is 57% unclickable · P1

Two separate defects stacked on the same two items.

**Identical glyphs.** `System status` and `Settings` use the same gear path, byte for byte:

```
d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1"
```

It is the only duplicated `d` value in `Sidebar.tsx`, and the two items are adjacent and last, so
on the collapsed rail the final two destinations are indistinguishable. Give System status its own
glyph — a server, pulse or heartbeat reads as status; keep the gear for Settings.

**Settings is occluded by the account footer.** Measured at 1440×900 on `/context`:

```
nav column     scrollHeight 734 > clientHeight 690   (overflow-y: auto)
footer         position: sticky, top 757
Settings link  top 749, bottom 789, height 40
vertical overlap with footer: 23px of 40px  →  57% occluded
```

Sampling straight down the Settings link, only the top 15% hits the link; from 32% downward the
pointer resolves to `.sidebar-footer`, `.sidebar-identity`, then the avatar. **Clicking the centre
of the Settings icon opens the account menu instead.** It also explains why that item appears to
have no hover text — the pointer never reaches the element carrying the `title`.

This happens whenever the nav column overflows, which at this content height needs roughly a
932 px viewport to clear — so a typical laptop at 1440×900 with browser chrome is inside the
failing range.

**Change.** Reserve the footer's height in the nav column's layout (`padding-bottom` equal to the
sticky footer, or make the footer a flex sibling that cannot overlap) so the last item is always
fully hittable. Add a regression check that the last nav item's rect does not intersect the
footer's at 800, 900 and 1000 px viewport heights.
