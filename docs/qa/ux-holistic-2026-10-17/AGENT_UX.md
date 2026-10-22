# Agent-specific UX review — APEX Orchestration dashboard

**Step 4 of the holistic UX review · 2026-10-17**

The question behind every finding here: *can a human tell what the AI agent did, why, and what it
is waiting on?* Two readers are in scope — the **operator** who launches a pipeline and answers its
gates, and the **governance reviewer** who audits a finished run.

Evidence comes from both environments. The connected fleet (`:4175`, 48 runs) turned out to carry
more agent evidence than the demo dataset: it is the only place in the product where a durable
`tool_calls` record, a `reasoning_digest`, a `resolved_prompt_source` and a `skipped` phase all
exist on real data. Where a claim rests on one specific run, that run's id is given.

Findings already recorded in `CORRECTNESS.md`, `CONSISTENCY.md`, `FINDINGS-DRAFT.md`,
`docs/dashboard-usability-remediation.md` or `apps/dashboard/PARITY.md` are not repeated; where
this review found a *new surface* for an existing finding, it says so and stays brief.

---

## 1. The state × surface matrix

This is the centre of the report. It records, for each phase state, exactly what each surface
renders: **glyph · colour token · label**.

### 1.1 What the matrix is not

Three surfaces that were expected to carry per-phase state do not carry it at all. That is a
finding in itself, and it collapses most of the matrix:

| Surface | What it actually shows per phase |
|---|---|
| `/runs` **PROGRESS** column | `n/m` text only (`RunsListPage.tsx:163`). **No strip, no glyph, no colour.** |
| Home **Recent runs** table | Nothing. Columns are RUN / APPLICATION / **PHASE** (= `current_phase` name) / STATUS / UPDATED (`HomePage.tsx:104-118`). |
| Run detail → **Overview** section | Nothing. `RunOverview.tsx` renders no phase list and mounts no `PhaseStrip`. |
| **Approvals queue** | Only the two gate states, as a text chip ("Review prompts" / "Review phase results"). All other states absent (`ApprovalsQueue.tsx:92-94`). |

`PhaseStrip` has exactly **one** call site in the whole application — `RunDetailPage.tsx:344`,
`size="lg"`, `selectedOnly` — see **AGT-15**. So the per-phase state vocabulary reaches the user
through four live surfaces only: the large phase strip, the phase metrics row, the timeline, and
compare.

### 1.2 The matrix

Colour tokens are the ones the CSS actually resolves. `phase-step--*` rules are in
`components/runs/PhaseStrip.css`.

| State (snapshot value) | **Phase strip** (`phase-strip--lg`, run detail) | **Phase metrics row** (`PhaseOperatorSummary`, run detail) | **Timeline** (`/runs/:id/timeline`) | **Compare** (`/runs/compare`) | `/runs` PROGRESS | Home recent | Approvals queue |
|---|---|---|---|---|---|---|---|
| `succeeded` | **✓** · `--success` border/text on `--success-bg` · **"COMPLETE"** | — · — · **"Complete"** | grey-blue dot · `--success` · **"Env Triage succeeded"** | — · `status-badge success` · **"SUCCEEDED"** | counted in numerator | absent | absent |
| `failed` | **✕** · `--danger` on `--danger-bg` · **"FAILED"** | — · — · **"Failed"** | dot · `--danger` · **"… failed"** | — · `status-badge danger` · **"FAILED"** | **counted in numerator** | absent | absent |
| `aborted` | **■** · `--danger` on `--danger-bg` *(same class as `failed`)* · **"ABORTED"** | — · — · **"Aborted"** | dot · `--danger` · **"… aborted"** | — · `status-badge danger` · **"ABORTED"** | **counted in numerator** | absent | absent |
| `skipped` | **–** · `--text-muted` 30 % **45° hatch** · **"SKIPPED"** | — · — · **"Skipped"** | dot · `--text-muted` · **"Story Analysis skipped"** | — · `status-badge neutral` · **"SKIPPED"** | **counted in numerator** | absent | absent |
| `pending` | **sequence number** · `--border` / `--bg-secondary` / `--text-muted` · **"PENDING"** | — · — · **"Pending"** | **absent** (no `started_at`/`ended_at` → no row) | — · `status-badge neutral` · **"PENDING"** | not counted | absent | absent |
| `running` | **sequence number** · `--accent` on `--accent-soft`, pulsing · **"EXECUTING"** | — · — · **"Executing"** | dot · `--accent` · **"… started"** only | — · `status-badge accent` · **"RUNNING"** | not counted | absent | absent |
| `awaiting_prompt_review` | **sequence number** · `--info-border` / `--info-bg` / `--info-text` · **"PROMPT REVIEW"** | — · — · **"Prompt Review"** | **absent** | — · `status-badge warning` · **"AWAITING PROMPT REVIEW"** | not counted | absent | chip **"Review prompts"**, `accent` |
| `awaiting_output_review` | **sequence number** · `--accent-results` mix · **"RESULTS READY"** | — · — · **"Results Ready"** | **absent** | — · `status-badge warning` · **"AWAITING OUTPUT REVIEW"** | not counted | absent | chip **"Review phase results"**, `info` |
| `none` (excluded from plan) | **hidden** by `selectedOnly`; listed in a collapsed `<details>` "Phases outside this run" (`RunDetailPage.tsx:355`) | n/a (no entry) | **absent** | — · `status-badge neutral` · **"NOT SELECTED"** | excluded from denominator | absent | absent |
| *unrecognised value* | **sequence number** · pending treatment · **"PENDING"** ← **AGT-05** | — · — · **"Pending"** | dot · `--text-muted` · raw value | — · `status-badge neutral` · raw value | not counted | absent | absent |

### 1.3 Cells that render identically, or rely on colour alone

1. **`pending` vs an unrecognised status** — identical on every surface that shows state, because
   `pipelinePhaseVisual()` and `pipelineStatusLabel()` both fall through to `pending`
   (`runDisplay.ts:81-121`). An unknown is presented as a known. **AGT-05.**
2. **`pending`, `awaiting_prompt_review`, `awaiting_output_review`, `running`** share the
   **sequence-number glyph** in the strip (`PhaseStrip.tsx:88` — the glyph map has no entry for
   them, so `index + 1` is used). Four of nine states carry no information in the glyph channel;
   they are separated by circle colour plus an 11px uppercase caption.
3. **`awaiting_output_review` vs `pending`** is predicted to collapse completely in five of six
   themes. `--accent-results` is defined only in `apex-light` (CON-09); both `color-mix()`
   declarations at `PhaseStrip.css:146-147` are then dropped at computed-value time and the circle
   falls back to `.phase-step-circle`'s base `--border` / `--bg-secondary` / `--text-muted`, which
   *is* the `pending` treatment. Same glyph, same colour, different caption only. **I could not
   measure this in the browser — see "What I could not verify".** CON-09 already owns the token
   gap; this is its state-collision consequence.
4. **`failed` vs `aborted`** share `.phase-step--failed` and the `danger` badge tone everywhere.
   They are separated by glyph (✕ vs ■) and label (FAILED vs ABORTED) only — two channels, so this
   is acceptable, but it means **no surface distinguishes "the agent failed" from "a human stopped
   it" by colour**, and compare/timeline have no glyph at all.
5. **`pending`, `awaiting_*` and `none`** are all **absent** from the timeline, and a `skipped`
   phase with no timestamps would be too. Four states collapse to "not in the audit trail".
6. **Nothing relies on colour alone.** Every surface that shows state shows a text label beside
   it, the strip's segments carry `aria-label`, and the global reduced-motion rule
   (`accessibility.css:89-91`) kills the `running` pulse without removing any other channel. This
   is worth saying plainly: the product passes the colour-alone test.

### 1.4 Vocabulary drift across the matrix

One snapshot enum, **four** formatters, reachable on **one** screen:

| snapshot | strip & metrics (`pipelineStatusLabel`) | timeline (`statusLabel`) | compare (`statusLabel`) | activity feed (`statusLabel`) |
|---|---|---|---|---|
| `succeeded` | **Complete** | succeeded | SUCCEEDED | succeeded |
| `running` | **Executing** | running | RUNNING | running |
| `awaiting_prompt_review` | **Prompt Review** | awaiting prompt review | AWAITING PROMPT REVIEW | awaiting prompt review |

COR-18 records the compare-vs-detail half of this. **New surfaces for it:** the run **timeline**
(`timelineModel.ts:72`) and the **activity feed divider** (`ActivityFeed.tsx:348`) are a third and
fourth consumer of `statusLabel`, and the activity feed sits on the *same page* as the strip — so
"EXECUTING" and "running" can be on screen simultaneously for one phase. The fix in COR-18 should
be widened to these two call sites.

---

## 2. Question 1 — is it clear what is running, what the agent is doing, and what it waits on?

**Largely yes at the run level, and the architecture is sound.** The run header keeps four facts
apart deliberately (`RunDetailPage.tsx:216-221`): an orchestration badge, `Test result: …`,
`n/m phases finished`, and — in the hero below — a `LiveStatusChip`. `GateDecisionContext` and the
`gate-decision-scope` caption state consequences. `GateModuleView`'s `awaiting_agent` state says
"Agent working on your prompt edits — the gate will reopen", which is exactly the right answer to
"what is it doing now".

Three defects.

<a id="agt-08"></a>
### AGT-08 — At a gate, the phase strip and the phase metrics row print two different statuses for the same phase, 200px apart

- **Severity** P2
- **Area / route** `/runs/:threadId/phases/:phase` and `/runs/:threadId/reviews/:interruptId`
- **Mode / role** connected, admin, apex-light (mode-independent — it is a code path, not data)
- **Screenshot** `agent-05-output-gate-results-ready-vs-pending.jpg`

**Repro**

1. Open `http://127.0.0.1:4175/runs/01a14ad6-2be7-7981-bc48-d5915e182552/phases/story_analysis`
   (an open `phase_review` gate).
2. Read the first segment of the phase strip, then read the STATUS cell directly below the gate.

**Expected** One phase, one status.

**Actual** The strip reads **"Story Analysis · RESULTS READY"**; the metrics row reads
**"STATUS · Pending"**. The same contradiction occurs at a `prompt_review` gate
(`.../01a14ad6-1b76-7d40-943e-0bf969efa44f/phases/story_analysis`): strip "PROMPT REVIEW", metrics
"Pending" — see `agent-06-prompt-gate-skip-vs-abort.jpg`.

**Why** `RunDetailPage.tsx:127-130` patches the *normalised strip* from `hitl.gate` after
`normalizedPhases()` has run, but `PhaseWorkspace` passes the unpatched `state` down, and
`PhaseOperatorSummary` reads `state.phase_results[phase].status` directly
(`PhaseWorkspace.tsx:306`). The API confirms the raw value is `pending` while a gate is open.
COR-06 predicted this ("remove the pending gate and the detail page would show Pending") and
treated the page as masking the disagreement; it does not — the mask covers the strip only, and
the unmasked value is on screen at the same time, at the decision point.

**Recommended change** `apps/dashboard/src/features/runs/RunDetailPage.tsx:126-130` — build the
patched status once and pass it into `PhaseWorkspace` (e.g. a `gateStatus?: string` prop consumed
by `PhaseOperatorSummary`), instead of mutating only the strip array. One source, one answer.

<a id="agt-11"></a>
### AGT-11 — "Waiting 1h ago": the only element that says how long the agent has been blocked on a human is ungrammatical

- **Severity** P3
- **Area / route** `/runs/:threadId/reviews/:interruptId` (run header, inbox embed)
- **Mode / role** connected, admin
- **Screenshot** `agent-05-output-gate-results-ready-vs-pending.jpg` (header area)

**Repro** Open any row in `/approvals`. Read the run header.

**Expected** "Waiting 1h" or "Blocked on you for 1h".

**Actual** **"Waiting 1h ago"**, and for the older seeded gates **"Waiting Jun 11"**.
`ApprovalsInboxPage` passes `waitAge={row.age}`, where `age` is `formatRelative(...)`
(`useApprovalsInbox.ts:142`), which returns a *point-in-time* phrase — "just now", "35m ago",
"1h ago", "3d ago", or a date. `RunDetailPage.tsx:221` renders it after the word "Waiting".

**Recommended change** `apps/dashboard/src/utils/time.ts` — add a sibling `formatElapsed()` that
returns the bare duration ("1h", "3d"), and use it for `waitAge` in
`apps/dashboard/src/features/approvals/ApprovalsInboxPage.tsx` while the queue row keeps
`formatRelative`. The queue's "1h ago" is correct in its own context.

<a id="agt-07"></a>
### AGT-07 — The connectors between phase steps are dead CSS: the progress line never shows progress

- **Severity** P2
- **Area / route** every run detail workspace, all modes, all themes
- **Screenshots** `agent-11-all-complete-grey-connectors.jpg` (7 × COMPLETE, 6 grey connectors),
  `agent-01-strip-skipped-failed-7of7.jpg`

**Repro** Open any run with ≥2 selected phases at ≥1120px and look at the short horizontal rules
between the step circles.

**Expected** A connector tinted by the state it follows — that is plainly what
`PhaseStrip.css:119-176` was written to do (seven `… .phase-step-connector` declarations).

**Actual** Every connector is `var(--border)` grey regardless of state, on a fully-succeeded run
and on a fully-failed one alike.

**Why** `PhaseStrip.tsx:115` renders the connector as a **sibling** of `.phase-step` inside
`.phase-step-wrap`, but all seven CSS rules are **descendant** selectors
(`.phase-step--completed .phase-step-connector`). They can never match. The modifier class the
component does emit — `phase-step-connector--{visual}` — has **zero** definitions anywhere in
`src/**/*.css`. Nine declarations of dead styling, and a visual channel that silently carries
nothing.

**Recommended change** `apps/dashboard/src/components/runs/PhaseStrip.css:119-176` — change the
seven descendant selectors to the modifier the component already emits, e.g.
`.phase-step-connector--completed { background: var(--success-border); }`. Keeping the
`--pending` connector grey is correct; the rest should match their step.

---

## 3. Question 2 — are the phase states distinct everywhere?

The matrix in §1 is the answer. The state-specific defects follow.

<a id="agt-05"></a>
### AGT-05 — An unrecognised phase status is rendered as "Pending", not as unknown

- **Severity** P2
- **Area / route** every surface that shows phase state
- **Mode / role** both modes (code path)

**Repro** `pipelinePhaseVisual()` and `pipelineStatusLabel()`
(`apps/dashboard/src/features/runs/runDisplay.ts:81-121`) both end in
`case 'pending': default: return 'pending'` / `'Pending'`. Any status the dashboard does not know —
a new backend state, a drifted schema, a typo — is asserted to the operator as "not started yet".

**Expected** The same treatment the transport chip already gives: show the raw value, muted, and
do not claim a known state. `LiveStatusChip.tsx:41-52` does this correctly and even documents it
("Unknown statuses … render muted with the raw text"). `orchestrationStatusLabel()`
(`runDisplay.ts:155`) returns the literal `'Unknown'`. `executionResultLabel()` returns
"Not available" / "Not applicable". **Three of the four status vocabularies handle unknown
correctly; the phase vocabulary is the exception**, and it is the one with the most states.

**Actual** "Pending", in the pending colour, with the pending glyph — indistinguishable from a
phase that genuinely has not started.

**Recommended change** `apps/dashboard/src/features/runs/runDisplay.ts:81-121` — add an `unknown`
member to `PipelinePhaseVisual`, return it for any status outside the known set, map
`pipelineStatusLabel` to the raw value, and add `.phase-step--unknown` to
`components/runs/PhaseStrip.css` with a dashed `--warning-border` circle. `pending` keeps its own
branch.

<a id="agt-04"></a>
### AGT-04 — The approvals queue's prompt-gate / output-gate colour coding has no effect: `.info` and `.accent` are the same rule

- **Severity** P2
- **Area / route** `/approvals` queue; also every `topbar-meta-chip` in the app
- **Mode / role** connected, admin, apex-light — and **all six themes**, because the cause is a
  shared CSS rule, not a token value
- **Screenshot** `agent-07-queue-identical-gate-chips.png`

**Repro**

1. Open `/approvals` with both gate kinds in the queue.
2. Compare the "Review prompts" chip with the "Review phase results" chip.

**Expected** `ApprovalsQueue.tsx:19-22` is explicit about the intent — *"Chip tone per gate kind:
accent = prompt_review, info = phase_review"*. These are two materially different decisions: one
approves **input to** the agent before any work happens; the other accepts the agent's **output**
and commits the plan forward.

**Actual** Measured computed styles on the live queue:

```
"Review phase results"  topbar-meta-chip info    color rgb(0,117,165)  bg rgb(229,246,252)  border rgba(0,168,224,.22)
"Review prompts"        topbar-meta-chip accent  color rgb(0,117,165)  bg rgb(229,246,252)  border rgba(0,168,224,.22)
```

Byte-identical. `theme/primitives.css:248-252` declares `.topbar-meta-chip.info` and
`.topbar-meta-chip.accent` as **one shared rule** reading `--accent-border` / `--accent-soft` /
`--accent-hover`. The `--info-bg` / `--info-border` / `--info-text` tokens — which *are* defined in
all six themes — are never consumed by the chip. `gateKindTone()` is dead code.

**Recommended change** `apps/dashboard/src/theme/primitives.css:248-252` — split the rule and give
`.topbar-meta-chip.info` its own `var(--info-border)` / `var(--info-bg)` / `var(--info-text)`
declaration. The tokens already exist per theme, so this is a four-line change that makes an
intended distinction visible everywhere the chip is used.

<a id="agt-15"></a>
### AGT-15 — `PhaseStrip`'s compact variant is dead code, and PARITY row 3a claims it is on the runs grid

- **Severity** P3
- **Area / route** `/runs` (claimed), `components/runs/PhaseStrip.tsx`
- **Screenshot** `agent-04-runs-grid-7of7.jpg`

`apps/dashboard/PARITY.md:22` describes `RunsListPage.tsx` as having a "phase-strip micro-viz".
`grep -rn "PhaseStrip" apps/dashboard/src --include="*.tsx"` returns **one** non-test consumer:
`RunDetailPage.tsx:344`, at `size="lg"`. The runs grid renders `n/m` text
(`RunsListPage.tsx:163`) and nothing else.

Consequence: the `md`/`sm` branch of `PhaseStrip.tsx:120-154` and the nine `.phase-seg--*` rules
(`PhaseStrip.css:199-251`) are unreachable. So is the `none: '–'` entry in the `lg` glyph map
(`PhaseStrip.tsx:88`), because `selectedOnly` filters `none` out before it can render — which is
fortunate, since `–` would otherwise be the same glyph as `skipped`.

This matters for the review because the unreachable variant is the one that *would* have failed
the colour-alone test: 18×8px bars with no text, where `aborted` is `failed` at `opacity: .45` and
`pending` is `skipped` without the hatch.

**Recommended change** Decide one of two things and do it:
(a) put the strip in the grid — `apps/dashboard/src/features/runs/RunsListPage.tsx:163`, render
`<PhaseStrip strip={run.phase_strip} size="sm" />` beside the `n/m`, which would fix the far more
serious problem that a 7/7 run with five failures looks identical to a clean 7/7 run; or
(b) delete the `md`/`sm` branch and the `.phase-seg` block and correct `apps/dashboard/PARITY.md:22`.
Option (a) is the better product. If taken, `.phase-seg--aborted`'s opacity-only distinction from
`.phase-seg--failed` must be fixed first, and each segment needs a text equivalent beyond `title`.

<a id="agt-12"></a>
### AGT-12 — Home's "Execution Health" counts two of the nine phase states and says so nowhere

- **Severity** P3
- **Area / route** `/` → Execution Health card
- **Mode / role** connected, admin

**Repro** Open `/`. Read "PHASES SUCCEEDED 73 · PHASES FAILED 5" against the fleet, whose
`phase_strip` entries total 108 succeeded, 97 pending, 107 none, 12 awaiting, 6 running, 5 failed,
1 skipped.

**Expected** Either all states, or a caption naming the subset.

**Actual** Only two tiles. `services/usage.py:866-873` sums `phase:%:succeeded` and
`phase:%:failed` and nothing else, so **`skipped` and `aborted` phases are invisible in the
product's only phase-level health summary** — including the skipped phase that caused five
downstream failures on `01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90`. The caption
(`HomePage.tsx:258`) explains the time window but not the state subset, so "73 / 5" reads as a
complete picture.

**Recommended change** `apps/dashboard/src/features/home/HomePage.tsx:259-267` — add a third tile
fed by a new `phases_skipped` field, and amend the caption to "Succeeded, failed and skipped
phases; phases still pending or awaiting review are not counted." The backend change is one more
`case()` in `src/apex/services/usage.py:866-873`.

<a id="agt-16"></a>
### AGT-16 — The phase strip announces a different vocabulary than it displays

- **Severity** P3
- **Area / route** run detail phase strip, all modes

`PhaseStrip.tsx:46-48` builds `aria-label` as `` `${phase} — ${status}${attempt}` `` from the raw
snapshot values, and that label overrides the element's text content. A sighted user reads
**"Story Analysis / RESULTS READY"**; a screen-reader user hears **"story_analysis —
awaiting_output_review (attempt 1)"**. Neither the human phase name nor the human status reaches
assistive technology, and the two users cannot talk to each other about what they see.

**Recommended change** `apps/dashboard/src/components/runs/PhaseStrip.tsx:46-48` — build the label
from `PHASE_LABELS[phase]` and `pipelineStatusLabel(status)`, keeping the raw values in `title` for
debugging: `` `${PHASE_LABELS[phase]} — ${pipelineStatusLabel(status)}${attemptSuffix}` ``.

### Extensions to findings already on record (not re-reported)

- **A terminal run whose strip claims a phase is executing.** Demo `run-aborted`
  (`run_aborted: true`, header "ORCHESTRATION FAILED") renders **"7 · Postmortem · EXECUTING"** in
  the accent colour with the live pulse — `agent-08-demo-postmortem-executing-on-failed-run.jpg`.
  This is a fourth variant of the known "terminal run with live vocabulary" finding, and the one
  that uses the strongest *right-now* signal the UI has.
- **Skipped phases and gate wait time.** `story_analysis` on
  `01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90` is `skipped` and reports **"in 32m 13s"** on the timeline.
  Compare then tints that cell `compare-cell--slow` — the UI ranks a phase that never ran as the
  slowest phase in the comparison (`agent-12-compare-phase-states.jpg`). New surfaces for the known
  gate-wait-in-duration finding.
- **`current_phase` on a failed run.** On the same run the overview's only call to action is
  **"Open Env Triage"** — the single phase that succeeded among five failures
  (`agent-09-overview-cascade-errors.jpg`). `RunOverview.tsx:46` uses `targetPhaseFor()`. New
  surface for the known `current_phase` finding.

---

## 4. Question 3 — is AI-generated content kept distinct from human-edited content?

The product has the right vocabulary in three places and loses it in the places that matter most.

**What works** (details in §7): the prompt gate's provenance chip and read-only/edited chips; the
phase Details tab's `Output source` line, whose map includes an explicit `llm → "AI-generated"`
(`PhaseWorkspace.tsx:272`); and — the best implementation in the codebase — the **new-test
wizard**, which chips every prompt as either `run override` (warning tone) or `catalog@v{N}`
(accent tone) and offers "Revert to catalog" (`features/new-test/steps/PromptsStep.tsx:139-151`).

<a id="agt-03"></a>
### AGT-03 — The backend records who edited a prompt; no screen in the dashboard shows it

- **Severity** P2
- **Area / route** `/runs/:id/phases/:phase` → "Prompt configuration"; the `prompt_review` gate
  panel; the Agent Reasoning tab
- **Mode / role** connected and demo; both

**Repro**

1. On a run-scoped prompt edit the backend writes
   `source = {"origin": "run_override", "ref": <prior ref>, "editor": <actor>}`
   — `src/apex/services/pipeline_read.py:1424-1429`. For a catalog-resolved prompt it writes
   `{"origin": …, "ref": …, "editor": None}` — `src/apex/services/prompts.py:518`.
2. `grep -rn "editor" apps/dashboard/src --include="*.ts*"` outside the dev-data store: **no
   consumer**. The only write is the demo fixture at `dev-data/store.ts:1354`.

**Expected** The gate and the phase page say who changed the prompt the agent ran, and when. The
data is already in the payload, and `PhasePromptReview` additionally carries `updated_by` and
`updated_at` (`src/apex/services/prompts.py:500-509`), which the dashboard also drops.

**Actual** Every provenance surface renders `origin` and `ref` only:
`PromptReviewSection.tsx:100-104` (`sourceLabel`), `PromptReviewPanel.tsx:61-70` (`gate-provenance`),
`PhaseWorkspace.tsx:377-387` (`prompt-provenance`). A human-authored prompt is signalled by one
un-humanised token — `run_override` — in the **same accent chip** used for `catalog`. The "edited"
chip beside it (`PromptReviewPanel.tsx:76-84`, `PromptReviewSection.tsx:200`) is **session-local**:
its own tooltip says "Your draft differs from the prompt the agent resolved", and it disappears as
soon as the edit is saved. So after a human edits a prompt, nothing on any screen says a human did
it, who, or when.

For the governance reviewer this is the question "who changed what the agent was told to do", and
the answer is on the wire and discarded by the client.

**Recommended change**

1. `apps/dashboard/src/features/runs/PromptReviewSection.tsx:100-104` — change `sourceLabel()` to
   humanise the origin (`run_override → "Edited for this run"`, `catalog → "Catalog"`) and append
   `source.editor` when present: `Edited for this run by apex-admin · phase/env_triage/system@v1`.
2. Give the override chip the `warning` tone, matching the wizard
   (`features/new-test/steps/PromptsStep.tsx:140`) rather than reusing `accent` for both.
3. Apply the same helper at `apps/dashboard/src/hitl/PromptReviewPanel.tsx:61-70` and
   `apps/dashboard/src/features/runs/PhaseWorkspace.tsx:377-387` — one `promptSourceLabel()` in
   `runDisplay.ts`, three call sites.
4. Render `updated_by` / `updated_at` in the Prompt Review header; the fields are already on
   `PhasePromptReview`.

<a id="agt-09"></a>
### AGT-09 — The agent's reasoning digest is rendered as unlabelled body copy directly under the "Output source" line

- **Severity** P3
- **Area / route** `/runs/:id/phases/:phase?tab=details`
- **Mode / role** connected, admin
- **Screenshot** `agent-01-strip-skipped-failed-7of7.jpg` (bottom of frame)

**Repro** Open `/runs/01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90/phases/env_triage`. Read the two lines
under PHASE DETAILS.

**Expected** If the digest is worth repeating outside the Agent Reasoning tab, it should carry the
same "Reasoning Digest" heading it has there.

**Actual** Two consecutive `<p class="workspace-caption">` elements with no heading between them:

```
Output source: Stub — demonstration output
Deterministic stub reasoning for env_triage (attempt 1, revision 0).
```

`PhaseWorkspace.tsx:271-275`. The second line is the model's own account of its reasoning, in the
same type style as the provenance caption above it, with nothing marking it as agent-authored or
distinguishing it from UI chrome. It is also a verbatim duplicate of the labelled copy on the
Agent Reasoning tab (`PhaseWorkspace.tsx:360-365`).

The adjacent "**What happened**" block has the same shape of problem at larger scale:
`PhaseOperatorSummary` (`PhaseWorkspace.tsx:324-325`) renders `entry.summary` — several paragraphs
of model-written prose — as plain `<p>` elements **above** the tab bar, so the `Output source`
label that would qualify them sits inside a tab panel below and does not apply to them visually.
See `agent-11-all-complete-grey-connectors.jpg` for the full-width version of this.

**Recommended change** `apps/dashboard/src/features/runs/PhaseWorkspace.tsx:275` — delete the
duplicate and link to the tab instead, or give it the `workspace-section-title` heading used at
`:362`. Separately, move the `Output source:` line out of `PhaseDetailsTab` and into
`PhaseOperatorSummary`'s metrics row (`:305-311`) as a sixth cell, so one provenance statement
governs the "What happened" prose it describes.

### Surfaces checked and found adequate

- **Gate dialogue** (`hitl/DialogueThread.tsx`, `hitl.css:224-250`): operator bubbles are
  right-aligned on `--accent-soft`; agent bubbles are left on `--bg-card`; both carry an uppercase
  role label. Three channels. The durable copy on the Agent Reasoning tab
  (`run-detail.css:537-559`) styles `.agent` and `.operator` explicitly. Good. One gap: an
  unrecognised `role` string falls back to the agent styling, so a future `system` or `tool` role
  would be presented as the agent.
- **Artifacts**: in connected mode the transcript ref carries `kind: "transcript"` and the chip
  reads TRANSCRIPT. In demo it is seeded as `evidence` and the same file reads EVIDENCE — a fixture
  inconsistency worth tidying in `apps/dashboard/src/dev-data/store.ts:385-390`.
- **Prompt catalog** (`/prompts/phase/…`): `active v1`, version list, note. Human edits there are
  versioned by construction.

---

## 5. Question 4 — are reasoning, tool calls and prompt provenance reachable from the decision point?

The honest answer splits in two. **At a prompt gate: yes, and it is well done.** At an **output
gate — the decision that accepts the agent's work — no, and it cannot be, by construction.**

<a id="agt-01"></a>
### AGT-01 — At a `phase_review` gate, neither the payload nor the snapshot carries any agent evidence, and the "Agent Reasoning" tab on the same page is empty

- **Severity** **P1**
- **Area / route** `/runs/:threadId/reviews/:interruptId` and `/runs/:threadId/phases/:phase` with
  an open `phase_review` gate
- **Mode / role** connected (admin) and demo; both
- **Screenshot** `agent-05-output-gate-results-ready-vs-pending.jpg`

**Repro**

1. Open `http://127.0.0.1:4175/runs/01a14ad6-2be7-7981-bc48-d5915e182552/phases/story_analysis`.
2. Read the gate panel, then click **AGENT REASONING**.
3. `curl -H "x-api-key: …" http://127.0.0.1:2024/v1/pipelines/01a14ad6-2be7-7981-bc48-d5915e182552`

**API values at the moment of decision**

```jsonc
// interrupts[0].payload keys — the whole contract the operator decides on:
["schema_version","kind","phase","actions","summary","result_preview",
 "artifacts","warnings","dialogue_tail"]

// values.phase_results.story_analysis — the durable record, not yet written:
{ "status": "pending", "provenance": null, "reasoning_digest": null,
  "tool_calls": [], "transcript_ref": null, "resolved_prompt": null }
```

**Expected** The human accepting agent output can see which prompt version produced it, which
model ran, what the agent reasoned, and which tools it called — at the gate, not after.

**Actual** None of those facts exists anywhere on the screen. The tab literally named **Agent
Reasoning** returns *"No reasoning details recorded for this phase yet."* (verified in the live DOM:
the rendered tabpanel text is exactly that string). `Output source` reads **"Not recorded"**.
`EVIDENCE` reads **"No phase artifacts"**. The operator sees a summary paragraph, an optional
`result_preview` JSON blob, the last three dialogue turns, and four buttons.

This is not a rendering bug — it is a sequencing one. `phase_subgraph.py`'s `finalize` writes
`reasoning_digest`, `tool_calls` and `transcript_ref` into the phase result **when the phase
finalises**, which happens *after* the gate resolves. The evidence the reviewer needs is produced
by accepting the thing they are being asked to accept.

The asymmetry with the other gate makes the gap concrete. A `prompt_review` payload carries
`prompt.source` (`{"origin":"catalog","ref":"phase/story_analysis/system@v1,phase/story_analysis/user@v1"}`),
`tools`, `context_packets` and `editable`. The gate that approves **input** is fully provisioned;
the gate that accepts **output** is not provisioned at all.

**Recommended change** Backend first, then one UI change:

1. `src/apex/graphs/pipeline/phase_subgraph.py` — add `reasoning_digest`, `tool_calls`,
   `resolved_prompt_source`, `model` and `transcript_ref` to the `phase_review` interrupt payload
   that the gate raises. They are all in hand at that point; only `finalize` is deferred.
2. `packages/pipeline-events/src/state.ts` — extend `PhaseReviewPayload` with those optional fields.
3. `apps/dashboard/src/hitl/PhaseReviewPanel.tsx:37-55` — render a `gate-chip-row` above the
   summary with the provenance chip, the model, and a "Reasoning & tool calls" disclosure, mirroring
   `PromptReviewPanel.tsx:60-85`. The panel currently has a `phase-review-sidebar` holding only the
   `result_preview` disclosure (`:92-104`); the evidence belongs there.

Until the backend lands, the smallest honest improvement is in the dashboard alone:
`apps/dashboard/src/features/runs/PhaseWorkspace.tsx:350-356` — when a gate is open on this phase,
replace the bare *"No reasoning details recorded for this phase yet"* with an explanation
("The agent's reasoning digest, tool calls and transcript are written when this phase finalises —
after this decision"). Saying so is better than an empty panel that reads as a data gap.

<a id="agt-02"></a>
### AGT-02 — The durable transcript path works end to end, but the tab named "Agent Reasoning" never links it

- **Severity** P2
- **Area / route** `/runs/:id/phases/:phase?tab=reasoning`
- **Mode / role** connected, admin
- **Screenshots** `agent-02-reasoning-tab-clipped-provenance.jpg`,
  `agent-10-transcript-artifact-viewer.jpg`

**This is the PARITY 4b verification requested in the brief. The durable path works.** Row 4b
defers reasoning-token streaming and says "durable reasoning remains available through the
`transcript_ref` artifact". Traced end to end on connected data:

- `values.phase_results.<phase>.transcript_ref` is populated for **all seven** phases of
  `01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90`, and `finalize` also appends the id to `artifact_ids` and
  to the run-level `artifacts` index (`phase_subgraph.py:2579-2599`).
- `GET /v1/artifacts/transcripts/<thread>/<phase>/attempt-1.txt` → **200, `text/plain`, 1.6 KB**.
- `/runs/:id` → "Evidence coverage" lists all seven transcripts by name and links them.
- The Artifact Viewer renders the file with a TRANSCRIPT kind chip, size and Download
  (`agent-10-transcript-artifact-viewer.jpg`). The body contains `reasoning_digest`,
  `resolved_prompt`, `tool_calls`, `approvals` and `dialogue`.

**The defect is signposting, not plumbing.**

1. `ReasoningTab` (`PhaseWorkspace.tsx:330-415`) renders the digest, tool calls, resolved prompt
   and dialogue — and **never mentions the transcript**, even though it holds the full record of
   everything the tab summarises. `resolveArtifacts()` (`:206-216`) only surfaces the
   `transcript_ref` as a side effect of its id also being in `artifact_ids`, and it does so on the
   *Details* tab.
2. On a phase with no finalised reasoning the tab says *"No reasoning details recorded for this
   phase yet"* **while a transcript for that phase exists and is linked two tabs away**. On
   `run-aborted`/`execution` in demo the Agent Reasoning tab is empty while the Details tab lists
   `execution-transcript.txt` — `agent-08-demo-postmortem-executing-on-failed-run.jpg`.
3. The word "transcript" appears in the UI only as an artifact `name` and `kind` chip. Nothing tells
   a governance reviewer that this file *is* the durable reasoning record that row 4b points them
   to. The ActivityFeed truncation notice (`ActivityFeed.tsx:325-327`) tells them "the timeline and
   transcript artifacts hold the full record" — but the timeline holds no transcript link either
   (see AGT-13's note), and the feed is not where they would be looking.

**Recommended change** `apps/dashboard/src/features/runs/PhaseWorkspace.tsx:358-372` — render the
transcript first in `ReasoningTab`, unconditionally when `entry.transcript_ref` exists, as a
labelled `ArtifactLink`: *"Full agent transcript (attempt N) — the durable record of this phase's
reasoning, prompts and tool calls."* Then change the `hasReasoning` guard at `:342-348` to include
`Boolean(entry?.transcript_ref)`, so the tab is never empty when a transcript exists.

<a id="agt-06"></a>
### AGT-06 — Every tool call that is not literally `"error"` gets a green "success" badge, including `timeout`, `denied` and `running`

- **Severity** P2
- **Area / route** `/runs/:id/phases/:phase?tab=reasoning` (durable) and `?tab=log` (live)
- **Mode / role** connected, admin
- **Screenshot** `agent-02-reasoning-tab-clipped-provenance.jpg` (the `OK` badge)

**Repro** `apps/dashboard/src/features/runs/PhaseWorkspace.tsx:195`:

```tsx
<span className={`status-badge ${call.status === 'error' ? 'danger' : 'success'}`}>
  {call.status ?? 'ok'}
</span>
```

and the live twin at `apps/dashboard/src/features/runs/ActivityFeed.tsx:357`:

```tsx
const tone = entry.status === 'error' ? 'danger' : 'success'
```

`ToolCallRecord.status` and `LiveToolCall.status` are both typed `string` — the wire format is not
an enum the client constrains (`features/runs/liveTypes.ts:26-32`). Any value other than the exact
string `error` is painted **green with the `success` tone**. A `timeout`, a `cancelled`, a
`denied`, a `partial`, or an in-flight `running` would all read as a success to a governance
reviewer scanning the Tool Calls list, with the true word printed inside the green pill.

**Expected** A three-way map at minimum — success / in-flight / failure — and `neutral` for
anything unrecognised, consistent with AGT-05's principle.

**Actual** Binary, with the non-`error` half asserted as success.

**Recommended change** Add `toolCallTone(status)` to
`apps/dashboard/src/features/runs/runDisplay.ts` next to `statusVisual()`:
`ok`/`success` → `success`; `error`/`failed`/`timeout`/`denied`/`cancelled` → `danger`;
`running`/`started`/`pending` → `accent`; anything else → `neutral`. Use it at
`PhaseWorkspace.tsx:195` and `ActivityFeed.tsx:357`.

While there: the durable row shows tool name, status, duration and timestamp but **no arguments and
no result** (`PhaseWorkspace.tsx:188-204`), and the live card shows even less — name and status only
(`ActivityFeed.tsx:356-365`). "Which tools were called" is answerable; "what did it call them with,
and what came back" is not, short of opening the transcript. Linking the row to the transcript
(AGT-02) closes that gap cheaply.

<a id="agt-10"></a>
### AGT-10 — On the Agent Reasoning tab the prompt-version chip is clipped at 170px and its tooltip shows an explainer instead of the value

- **Severity** P3
- **Area / route** `/runs/:id/phases/:phase?tab=reasoning`
- **Mode / role** connected, admin, apex-light
- **Screenshot** `agent-02-reasoning-tab-clipped-provenance.jpg`

**Repro** Open `/runs/01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90/phases/env_triage?tab=reasoning` and
read the chip under RESOLVED PROMPT.

**Measured in the live DOM**

```
text       "catalog · phase/env_triage/system@v1,phase/env_triage/user@v1"
scrollWidth 378   clientWidth 168   max-width 170px   overflow hidden   text-overflow ellipsis
title      "Where the winning prompt text came from"
```

**Expected** The prompt version a run used is the governance reviewer's primary artefact. It should
be readable, and if it must be truncated the `title` should carry the full value.

**Actual** The chip renders **`catalog · phase/env_triage/sy`** — 55 % of the value is clipped —
and hovering yields the explainer sentence, not the ref. `theme/primitives.css:212-214` caps every
`.topbar-meta-chip` at 170px.

**This is already known and fixed in one place.** `theme/ux.css:261` lifts the cap for
`.approvals-page .topbar-meta-chip` only, and I confirmed the chip renders in full at the gate in
the inbox (`scrollWidth 393 === clientWidth 393`). The run-detail gate panel also escapes the worst
of it, because its sidebar prints *"Prompt source: catalog phase/story_analysis/system@v1,…"* in
full (`PromptReviewPanel.tsx:106-109`) — see `agent-06-prompt-gate-skip-vs-abort.jpg`. The Agent
Reasoning tab has no such fallback, so it is the one surface where the value is genuinely
unreadable.

**Recommended change** `apps/dashboard/src/features/runs/PhaseWorkspace.tsx:378-387` — set
`title={`${source.origin}${source.ref ? ` · ${source.ref}` : ''}`}` so the full value is always
available, and add `.prompt-provenance-chip { max-width: none; white-space: normal; }` rather than
widening the `ux.css:261` route hack further. The two unlabelled `CodeViewer`s below it
(`:389-393`) should also gain visible "System" / "User" / "Application" captions; they have
`ariaLabel`s but no on-screen heading, so a reviewer cannot tell which block is which.

<a id="agt-14"></a>
### AGT-14 — The durable transcript is served as `text/plain` wrapping JSON, so the prompts inside it render as one escaped line

- **Severity** P3
- **Area / route** `/runs/:id/artifacts/:artifactId`
- **Screenshot** `agent-10-transcript-artifact-viewer.jpg`

The transcript body is `APEX phase transcript\n{…json…}` with `media_type: "text/plain"`.
`renderKindOf()` (`ArtifactViewerPage.tsx:38-44`) keys on the media type, so the file renders
through the plain `CodeViewer` with no JSON mode. The result is readable but poor: the embedded
`resolved_prompt.system` arrives as a single wrapped line studded with literal `\n` sequences,
which is a worse rendering of the same prompt that the Agent Reasoning tab displays correctly ten
lines above.

**Recommended change** Either emit the transcript as `application/json` with the banner moved into
a `_format` key (`src/apex/graphs/pipeline/phase_subgraph.py`), or special-case it in
`apps/dashboard/src/features/artifacts/ArtifactViewerPage.tsx:38-44`: when the body's first line is
`APEX phase transcript`, parse the remainder and render it with `JsonViewer`.

---

## 6. Question 5 — are irreversible actions clearly destructive and confirmed?

Inventory of the agent-facing destructive actions. `CONSISTENCY.md` CON-07 already covers the
app-wide confirm-strength and `btn-danger`/`btn-ghost` inconsistency, so this table records what is
agent-specific and the one new argument.

| Action | Where | Label | Variant | Confirms? | Proportionate? |
|---|---|---|---|---|---|
| Abort run at a gate | `hitl/GateActionBar.tsx:30-41` | `Abort` → `Confirm abort` | **`btn-danger`** | **Type `ABORT`** | Strong; arguably over-strong for a recoverable stop |
| Abort a busy run (header) | `RunDetailPage.tsx:234-238` | same component | `btn-danger` | Type `ABORT` | Consistent with the gate — good |
| **Skip phase** | `GateActionBar.tsx:166-175` | `Skip Phase` | **`btn-secondary`** | **None** | **No** — see AGT-13 |
| Prompt rollback | `features/prompts/RollbackConfirm.tsx` | `Set vN active` | `btn-primary` | Modal naming the version and its note | **Yes** — and the copy is excellent |
| Consumer key rotation | `features/admin/ConsumersPage.tsx:357-417` | `Rotate key` | `btn-primary` | Modal stating the 5-minute grace window | **Yes** |
| Archive a prompt | `features/prompts/PromptDetailPage.tsx:326-334` | `Archive` | `btn-ghost` | **None** | No — CON-07 owns this |
| Delete consumer / connection / environment / document | various | `Delete…` | mixed | mixed | CON-07 owns this |

<a id="agt-13"></a>
### AGT-13 — "Skip Phase" is the only gate action with no scope statement, and its blast radius is recorded in the data and shown nowhere

- **Severity** P3 (the one-click/undersized-confirm half is already on record; this is the
  consequence argument)
- **Area / route** `prompt_review` gate, every route it renders on
- **Mode / role** connected, admin
- **Screenshot** `agent-06-prompt-gate-skip-vs-abort.jpg`

The known finding is that `Skip Phase` is a one-click irreversible scope change with secondary
styling sitting beside an `Abort` that demands you type `ABORT`. Two things extend it.

**First, the asymmetry inside the action bar is sharper than styling.** Its neighbour carries an
explicit scope caption — `GateActionBar.tsx:161-164` renders *"Accepts this phase output; it does
not approve the test result or a release."* under "Accept phase output & continue", and
`GateDecisionContext.tsx:14-18` adds a plan-aware consequence sentence above the panel for both
gate kinds. **Skip Phase gets neither.** The component that is most careful about consequences
anywhere in the product says nothing about the only action that changes the plan.

**Second, the consequence is severe and is sitting in the fleet data.** On
`01a14ad6-1f91-7b83-bdb3-6aa2a4a32b90` a single `skip_phase` on `story_analysis` produced:

```
test_planning   failed — "Cannot run phase 'test_planning': prerequisite 'story_analysis' ended with status 'skipped'."
script_scenario failed — "… prerequisite 'test_planning' ended with status 'failed'."
execution       failed — "… prerequisite 'script_scenario' ended with status 'failed'."
reporting       failed — "… prerequisite 'execution' ended with status 'failed'."
postmortem      failed — "… prerequisite 'reporting' ended with status 'failed'."
```

One unconfirmed click ended the run. The header then reports **"7/7 phases finished"**
(`agent-04-runs-grid-7of7.jpg`) — COR-05 owns the counter, but the combination is what the operator
sees: an unlabelled one-click action, a cascade, and a progress counter that reads as complete.

**Recommended change** `apps/dashboard/src/hitl/GateActionBar.tsx:166-175` —
wrap `Skip Phase` the way `approve` is wrapped at `:151-165`, with a `gate-decision-scope` caption:
*"Marks this phase skipped and continues. Phases that depend on it will not run."* Extend
`apps/dashboard/src/hitl/GateDecisionContext.tsx:18` to name the dependent phases from
`state.phases_plan` for the `prompt_review` branch, exactly as the `phase_review` branch already
names the next phase. A confirm dialog is the second-best option; the caption is the one that stops
the mistake.

### Credit where due

`RollbackConfirm` is the model the rest of the app should copy: it names the target version,
quotes its note, and ends with *"No versions are modified or deleted."* — a destructive-looking
action correctly de-escalated by telling the user exactly how reversible it is. The rotate dialog
does the same for the five-minute key overlap. Both are proportionate.

---

## 7. Does the UI keep orchestration, workflow, transport and test result as four separate facts?

**Yes — deliberately and visibly.** This is one of the product's real strengths and it survives
every surface I checked.

| Fact | Source of truth | Where it is rendered | Unknown handled as unknown? |
|---|---|---|---|
| **Orchestration status** | `thread_status` + strip | header `status-badge`, `/runs` STATUS, Home STATUS | **Yes** — `orchestrationStatusLabel()` returns the literal `'Unknown'` (`runDisplay.ts:155`) |
| **Workflow / phase status** | `phase_results` | phase strip, metrics row, timeline, compare | **No** — collapses to "Pending" (**AGT-05**) |
| **Live transport status** | SSE connection | `LiveStatusChip` in the pipeline hero | **Yes** — raw text, muted, plus a `title` per state (`LiveStatusChip.tsx:41-52`) |
| **Test result** | `execution_result` / `test_summary.outcome.assessment` | header `Test result:`, `/runs` TEST RESULT, overview "Test acceptance" | **Yes** — Not available / Not applicable / Inconclusive / Not assessed (`runDisplay.ts:138-146`) |

Three of four handle unknown correctly; the phase vocabulary is the exception, which is AGT-05.

The header of a failed connected run reads **"ORCHESTRATION FAILED · Test result: Not available ·
7/7 phases finished"** and the hero adds a separate transport chip — four facts, four renderings,
no conflation (`agent-01-strip-skipped-failed-7of7.jpg`). The overview reinforces it in prose:
*"No acceptance assessment is available in this snapshot. Completing orchestration does not certify
a release."* That sentence is doing real work and should be protected in any redesign.

One addition rather than a defect: there is a **fifth** fact, the engine's own status, rendered by
`EngineStrip.tsx:93-100` as a raw string in a chip that is **always** `topbar-meta-chip accent`.
Unknown engine statuses survive as raw text (good), but a failing engine is tinted the same as a
healthy one. COR-14 owns the `"completed · 1%"` text defect; the invariant tone is the part worth
fixing alongside it — `apps/dashboard/src/features/runs/EngineStrip.tsx:94` should pick a tone from
the status rather than hard-coding `accent`.

---

## 8. What this product gets right

Stated plainly, because several of these are better than the norm for agentic tooling.

1. **The four-facts separation (§7).** Orchestration status, test result, phase progress and live
   transport are four distinct renderings that never merge, with a prose reminder that finishing
   orchestration is not a release certification. Most dashboards in this space collapse at least
   two of these.
2. **`GateDecisionContext` reads the persisted plan before stating a consequence.** Its comment —
   *"Consequences use the persisted plan; never assume the default phase sequence"* — is the right
   instinct, and the rendered sentence ("Accepting uses this phase output and continues to Test
   Planning, subject to its configured reviews") tells the operator what happens next from this
   run's actual plan.
3. **The `gate-decision-scope` caption.** *"Accepts this phase output; it does not approve the test
   result or a release."* sits under the primary button at every output gate. This is the single
   most valuable sentence in the HITL surface.
4. **`awaiting_agent` is a first-class gate state.** "Agent working on your prompt edits — the gate
   will reopen" (`GateModule.tsx:112-131`), with the verb varying by the action you took. The
   operator is never left wondering whether their revise landed.
5. **The prompt gate is genuinely well-provisioned.** Provenance chip, `read-only` chip, session
   `edited` chip, collapsible "Shared platform instructions (read-only)", a **"Tools in Play"**
   panel listing the tool surface the agent is about to be given, and context packets — all before
   you authorise execution (`agent-06-prompt-gate-skip-vs-abort.jpg`). Showing the tool surface at
   authorisation time is the right design and few products do it.
6. **The Agent Reasoning tab, when the data exists, is excellent** — reasoning digest, durable tool
   calls with duration and timestamp, the resolved prompt with a provenance chip, and the operator
   dialogue, all on one panel (`agent-02-reasoning-tab-clipped-provenance.jpg`). The defects above
   are about reach and labelling, not about the panel's design.
7. **The durable transcript path actually works** (AGT-02): every phase gets a transcript, it is
   indexed, proxied, fetched and rendered with a kind chip and a download. PARITY row 4b's claim
   holds at the data layer.
8. **The timeline records human decisions alongside machine events.** *"Gate prompt_review:
   skip_phase · by unknown · Story Analysis"* appears in order between the phase rows, with its own
   tone, and `skipped` / `failed` / `started` are three distinguishable dot colours
   (`agent-03-timeline-audit-trail.jpg`).
9. **`LiveStatusChip` is the reference implementation for unknown states** — six named states, each
   with an explanatory `title`, and unrecognised values rendered as raw muted text rather than
   guessed at. Every other status mapper in the app should be measured against it.
10. **Cascade errors are surfaced in full on the run overview** — all five `role="alert"`
    paragraphs, naming the prerequisite that failed (`agent-09-overview-cascade-errors.jpg`).
11. **No toasts anywhere, by documented intent**, with a coherent three-tier error model. Agent
    failures stay on screen where the operator can read them.
12. **The new-test wizard's prompt step already solves AGT-03** — `run override` vs `catalog@v{N}`
    chips with distinct tones and a "Revert to catalog" action
    (`features/new-test/steps/PromptsStep.tsx:139-151`). The pattern exists; it just has not reached
    the run surfaces.
13. **`RollbackConfirm` and the key-rotation dialog are proportionate confirmations** that explain
    reversibility instead of merely obstructing.
14. **Nothing in the phase-state vocabulary relies on colour alone.** Every live surface pairs tone
    with a text label.

---

## 9. What I could not verify, and why

1. **The predicted `awaiting_output_review` ≡ `pending` collision in the five non-`apex-light`
   themes (§1.3 item 3).** I set `data-theme` on `documentElement` and read computed styles, but
   the Browser pane was hidden, so the page ran as a background page and style recalculation was
   deferred: `--accent-results` correctly read empty under `data-theme="light"` while the element's
   computed background still returned the `apex-light` value. The claim rests on CSS reading
   (`PhaseStrip.css:62-77, 144-149` plus CON-09's token inventory) and is sound, but it is
   **unmeasured**. Re-run with the pane visible, or add a unit test that asserts the two circles
   resolve differently per theme.
2. **The `aborted` state on any live surface.** The connected fleet contains **zero** `aborted`
   phases across 48 runs (statuses present: succeeded 108, none 107, pending 97,
   awaiting_output_review 7, running 6, failed 5, awaiting_prompt_review 5, skipped 1), and the demo
   fixtures seed none either (`dev-data/store.ts:2287-2295`). Its matrix row is derived from
   `PhaseStrip.tsx:88`, `runDisplay.ts:66-121` and `PhaseStrip.css:158-163`, not from a screenshot.
   Producing one requires aborting a live run, which would have mutated state other reviewers in
   this session are reading.
3. **The rendering of a saved run-scoped prompt override (`origin: "run_override"`, AGT-03).** I
   established the backend behaviour from `pipeline_read.py:1424-1429` and the client behaviour from
   `PromptReviewSection.tsx:100-104`, but did not complete the round trip in the browser: doing it
   in connected mode writes to a shared thread, and the demo attempt was abandoned after the inner
   scroll container made the CodeMirror target unreliable within my budget. The dropped-`editor`
   finding does not depend on it — `grep` for `editor` across `apps/dashboard/src` is conclusive.
4. **Live tool-call cards and engine ticks in `ActivityFeed`.** The feed builds entries **only** from
   the SSE stream (`ActivityFeed.tsx:136-281`); its state starts empty on mount and nothing
   back-fills it from the snapshot. Every finished run therefore shows *"No live activity for this
   phase yet"* on the Pipeline Log tab. I could not observe a card without holding a run open while
   it executed, which the shared environment did not allow. Worth noting for whoever picks this up:
   the durable equivalents (`entry.tool_calls`) are on the Agent Reasoning tab, so the Pipeline Log
   tab is permanently empty for historical runs — a governance reviewer opening it on a finished run
   learns nothing, and the empty-state copy's "the other tabs show the durable snapshot" is the only
   pointer.
5. **Non-admin roles.** Both environments authenticated as admin. `RequireRole role="operator"`
   wraps the gate action bar and the abort control, so a viewer sees a gate panel with no actions;
   whether that state explains itself ("you cannot act on this") was not checked.
6. **Keyboard and screen-reader behaviour of the gate and strip** beyond reading the markup. AGT-16
   is a markup finding, not an AT observation; `FINDINGS-DRAFT.md` already carries the inbox's
   focus-management defects.
