# Four untouched areas — holistic UX review, 2026-10-17

Reviewer scope: **New Test wizard** (`/runs/new`), **Test Presets / Golden Configs**
(`/golden-configs*`), **Test Cycles** (`/cycles*`, never reviewed before, absent from
`apps/dashboard/PARITY.md`), and **Resources** (`/work-items*`, `/environments*`,
`/context`, `/evidence`). `/prompts*` is in scope only for walkthrough i.

Axes: consistency · thoroughness (state coverage) · correctness · ease of use · accessibility.

Environments exercised: **connected** `http://127.0.0.1:4175` (production build, same-origin
proxy, real API-key gate, admin consumer `apex-admin`, `scopes: []`), **demo**
`http://127.0.0.1:4173` (offline fixture, consumer *Demo Presenter*, role `admin` with one
project scope), API `http://127.0.0.1:2024`. Theme `apex-light` throughout unless stated.
Viewports 1440×900 and 390×844. No servers were stopped or rebuilt.

---

## Headline

**Test Cycles is not production-ready.** The data model, the preflight-blocker engine and the
deliverable-versioning mechanics are genuinely good — better than most of the app. But the
feature ships three admissions that it does not work (`Cycle dispatch is not enabled`,
`Retrieval connectors are not available in this editor yet`, `Publication is not configured`),
its review actions (`submit` / `approve` / `request_revision` / `archive`) fire on a single
unconfirmed click including on frozen approved versions, its one real data-loss guard is
bypassed by the Cancel button sitting next to Save, its concurrency failure is a bare
`Cycle request failed (409)` in unstyled body text with a manual copy-paste recovery, its
role gate disagrees with the server's scope gate so operators are shown a New-cycle button
that always 403s, and permission-denied is rendered as "not found". Details in ARE-10…ARE-21.

Second headline: the **New Test wizard footer asserts readiness it has not checked**
(ARE-01), and a draft whose attached document has been deleted still reports "Test plan is
complete" with Launch enabled (ARE-02).

Third: **Test Presets is unreachable in connected mode** — `POST /assistants/search` returns
HTTP 500 at the backend, unchanged since `docs/qa/resources-audit-2026-09-14` (ARE-07).

---

## Findings

Severity: **P0** blocks use / causes silent data or correctness loss with no recovery ·
**P1** fix before this area is dependable for routine operator use · **P2** meaningful
friction or a wrong statement to the user · **P3** polish.

---

### ARE-01 · P1 · correctness, consistency
**The wizard footer says "the execution target is set" when nothing is targeted**
Area: New Test wizard · Route `/runs/new?step=*` · connected · admin · apex-light · 1440×900
Screenshot: `area-01-wizard-review-ready-vs-sim.jpg`

Repro:
1. `/runs/new`, enter a Title and a Request. Leave Application and Environment at `— none —`.
2. Read the footer caption.

Expected: a readiness statement that matches what was actually checked — the engine is the
built-in simulator and no target was chosen.
Actual: the footer reads **"Ready to launch: the plan and execution target are set."** with no
application and no environment selected. On the Review step the same screen contradicts itself:
the Readiness card says *"Execution readiness: Simulated execution does not require an external
application or environment"* while the footer eight lines below asserts the target **is** set.

Root cause. `assessWizardReadiness` (`wizardState.ts:443`) correctly separates `planningIssues`
from `executionIssues`, and `executionIssues` is empty whenever `engine === 'sim'` — which is
the hard-coded default (`emptyDraft()`, `wizardState.ts:107`). The footer
(`NewRunWizard.tsx:613-618`) then prints a single sentence that claims both facts. Its second
branch ("The test plan is ready; execution setup still needs attention") is also unreachable
for the default engine.

This is a regression against the implementation note in
`docs/qa/new-test-pipeline-audit-2026-09-14/audit.md` ("external execution cannot launch
without an application and target environment") — the *gate* was implemented, the *wording*
still over-claims.

Recommendation: in `apps/dashboard/src/features/new-test/NewRunWizard.tsx:613`, print the two
facts separately, e.g. *"Plan is valid. Engine: Simulated — no external target required."* /
*"Plan is valid. Execution setup incomplete: 2 items."* Never emit "the execution target is
set" unless `draft.scope.environment_id` is non-null.

Same root cause, same file: `wizardState.ts:107` hard-codes `project_id: 'demo'` into
`emptyDraft()`. On any deployment whose project is not literally `demo`, every new draft starts
pre-filled with an invalid project.

---

### ARE-02 · P1 · correctness, thoroughness
**A draft whose document was deleted still reports "complete" and stays launchable**
Area: New Test wizard · Route `/runs/new?draft=…&step=review` · connected · admin · 1440×900
Screenshot: `area-02-wizard-deleted-doc-launchable.jpg`

Repro:
1. In the wizard Context step, upload a document; confirm the Review step lists it by name.
2. Go to `/context?tab=documents` and delete that document (the confirm dialog is excellent —
   see ARE-22).
3. Return to `/runs/new?draft=<id>&step=review`.

Expected: the stale reference is flagged as an issue, and Launch is blocked or the user is
asked to remove it.
Actual: Review still says **"1 document attached"**, the row reads **"Name unavailable ·
30a7026f… · — Inclusion not verified"**, Readiness says **"Test plan is complete."**, the
footer says **"Ready to launch…"**, and **Launch Test is enabled**. The only signal is the
phrase "Name unavailable" inside a card the user has no reason to re-open.

`stepIssues(draft,'context')` (`wizardState.ts:376`) only checks the packet count; nothing
validates that `document_ids` still resolve.

Recommendation: in `apps/dashboard/src/features/new-test/wizardState.ts:376`, add an issue when
a `document_ids` entry does not resolve in the loaded document list, with a one-click "Remove
missing document" from `ReviewStep.tsx`. The delete dialog in
`features/context/DocumentsTab.tsx` should also say that unlaunched drafts referencing the file
will need editing.

---

### ARE-03 · P2 · correctness, ease of use
**Touching the Application select silently discards attached documents, the environment and app prompt overrides**
Area: New Test wizard · Route `/runs/new?step=scope` · connected · admin · 1440×900

Repro:
1. Build a draft with Application = ACC, Environment = PERFHYPER2, one attached document.
2. On Scope, set Application to `— none —` and back to `ACC`.
3. Open Context and Scope again.

Expected: a warning before discarding, or the selections survive re-selecting the same app.
Actual: `ATTACHED (1)` is gone, Environment is back to `— none —`, app-scoped prompt overrides
are dropped. No dialog, no status message, no undo. The loss happens on the Scope step; the
damage is on steps the user cannot see.

`ScopeStep.tsx:175-200` unconditionally sets `document_ids: []` and `environment_id: null` in
the Application `onChange`. `ConfigStep.tsx:122-129` does the same when a preset changes the
app. The 2026-09-14 audit raised this for presets ("preview exactly what applying it will
replace"); it is still true, and it is also true for the plain select.

Recommendation: in `apps/dashboard/src/features/new-test/steps/ScopeStep.tsx:175`, keep the
selections and surface them as a reviewable list ("2 items no longer match the new
application — keep / remove"), or gate the change behind a confirm that names what will be
cleared.

---

### ARE-04 · P2 · correctness, ease of use
**De-selecting a phase permanently destroys the run prompt override authored for it**
Area: New Test wizard · Route `/runs/new?step=config` · connected · admin · 1440×900

Repro (verified twice, clean):
1. Prompts step → Story Analysis → **Override for this run** → confirm the chip reads
   `run override`.
2. Config step → click **Story Analysis** to exclude it, then click it again to include it.
3. Prompts step.

Expected: the override is retained, or at minimum the user is told it will be discarded.
Actual: the chip is back to `catalog@v1`; the authored text is gone. Re-selecting the phase
does not restore it. `togglePhase` (`ConfigStep.tsx:155-162`) filters `prompt_overrides` down
to the currently selected phases on every toggle.

Recommendation: in `apps/dashboard/src/features/new-test/steps/ConfigStep.tsx:155`, keep
overrides for de-selected phases in the draft and exclude them only in `buildConfigurable`;
show "1 override for an excluded phase" on the Review step.

---

### ARE-05 · P2 · accessibility
**The prompt-override editor is an unnamed `role="textbox"`**
Area: New Test wizard (and `/prompts`) · Route `/runs/new?step=prompts` · connected · 1440×900

Repro: Prompts step → *Override for this run* → inspect the `.cm-content` element.
Expected: an accessible name such as "Story Analysis system prompt override".
Actual: `role="textbox" aria-multiline="true" contenteditable="true"` with **`aria-label`
null and `aria-labelledby` null**. The code intends to name it —
`PromptsStep.tsx:164` passes `aria-label={editorLabel}` — but `@uiw/react-codemirror` does not
forward that prop to the contenteditable, so it is dropped. `features/prompts/PromptEditor.tsx:24`
has the identical bug. The project already knows the correct technique: `CodeViewer.tsx:37`
uses `EditorView.contentAttributes.of({'aria-label': …})`.

Recommendation: in `apps/dashboard/src/features/new-test/steps/PromptsStep.tsx:164` and
`apps/dashboard/src/features/prompts/PromptEditor.tsx:24`, pass the label through
`EditorView.contentAttributes.of(...)` as `CodeViewer` does. The read-only `<CodeMirror>` at
`PromptsStep.tsx:180` has no label at all and should get one too.

---

### ARE-06 · P3 · thoroughness, correctness
**Wizard copy promises capabilities the step does not have, and a dead link**
Area: New Test wizard · Routes `?step=context`, `?step=work-items` · connected · 1440×900

- The Context step's description reads *"Add documents **and context summaries** so the phases
  have the evidence they need"*, but there is no control for context summaries anywhere in the
  app. `context_summary_ids` is declared (`wizardState.ts:92`), cleared
  (`ScopeStep.tsx:79`), counted toward the packet limit (`wizardState.ts:348`) and sent at
  launch (`useWizardLaunch.ts:151`) — and can never be non-empty.
- The Work Items step always prints *"Saved query needs a connection update? **Open saved
  queries**."* (`WorkItemsStep.tsx:449`) even when the consumer has zero saved queries, linking
  to an empty page.
- Only the **system** prompt of a phase is shown and overridable. The catalog ships
  `<phase>/system` **and** `<phase>/user` (14 prompts, 7 pairs); the user prompt cannot be
  reviewed or overridden from the wizard.
- `"1 item still need attention."` — the noun is pluralised, the verb is not
  (`NewRunWizard.tsx:618`). With zero phases selected the policy line reads
  `"0 planned review pauses… Revisions may require additional reviews."`

Recommendation: drop "and context summaries" from `WIZARD_SECTION_COPY.context` or build the
picker; make `WorkItemsStep.tsx:449` conditional on `savedQueries.data?.length`; fix the verb
agreement; expose the `/user` prompt beside the `/system` prompt.

---

### ARE-07 · P1 · correctness (backend), thoroughness
**Test Presets is unreachable in connected mode — `POST /assistants/search` returns 500**
Area: Test Presets · Routes `/golden-configs`, `/runs/new?step=config` · connected · admin
Screenshot: `area-07-presets-500-connected.jpg`

Verified directly, independent of the dashboard:

```
POST http://127.0.0.1:2024/assistants/search   {"graph_id":"pipeline","limit":100,"offset":0}
→ 500 {"type":"about:blank","title":"Internal server error","status":500}
```

Also 500 for `{}` and `{"limit":10}`, and identically through the `:4175` same-origin proxy —
so it is the backend, not the proxy. The dashboard retries three times, showing a three-card
skeleton for ~11 s before settling.

Consequences: `/golden-configs` shows "Test presets unavailable"; `/golden-configs/:id` cannot
be reached at all in connected mode; the wizard Config step shows "Test presets failed to
load…". This is the same failure recorded in `docs/qa/resources-audit-2026-09-14/REPORT.md`
§08 ("Test Presets returned HTTP 500") — **unfixed after a month**.

Credit where due: the two UI-level recommendations from that audit *were* implemented — the
sidebar, topbar and heading all now say "Test Presets", and the error is plain language with no
raw JSON.

Recommendation: fix the `assistants.search` handler. In the dashboard, shorten the failure path
— `features/golden-configs/GoldenConfigsPage.tsx` should surface the failure after the first
attempt with "retrying…" rather than an 11-second skeleton that implies progress.

---

### ARE-08 · P1 · correctness
**Applying a preset silently downgrades an unrecognised engine to the simulator**
Area: Test Presets → wizard · Route `/golden-configs/:id` → `/runs/new?step=config&golden=…`
demo · Demo Presenter (admin, scoped) · apex-light · 1440×900
Screenshot: `area-10-preset-detail-raw-ids-apexload.jpg`

Repro:
1. `/golden-configs/asst-release` — the preset "ACC add-a-line Release Certification" shows
   **Engine: `apexload`**.
2. Click **Start run with this config**.
3. Wizard Config step: `config inherited` chip is shown. Read the Engine radiogroup, then open
   Review → Technical Details.

Expected: either the pinned engine is applied, or the user is told it could not be.
Actual: the Engine radiogroup has **Simulated** checked and the launch payload contains
`"engine": "sim"`. A release-certification preset that pins a distributed load engine would
launch as a simulation while the UI displays "config inherited".

`applyGoldenConfig` (`ConfigStep.tsx:77-84`) sets `engine: 'sim'` as the baseline and only
overrides it when the bundle's value is one of `['sim','apex_load','loadrunner']`; `apexload`
is not, so it falls through silently. `engineLabel` (`golden-configs/configView.ts:184`) has
the mirror-image bug — it falls back to the raw string, which is why the detail page prints
`apexload`.

The demo fixture value is itself wrong, but the *behaviour* is the defect: the app has three
spellings of one engine in play — `apex_load` (wizard), `apex-load` (Cycles script editor),
`apexload` (preset fixture) — and silently resolves the unknown ones to the simulator.

Recommendation: in `apps/dashboard/src/features/new-test/steps/ConfigStep.tsx:77`, when the
bundle pins an engine the wizard does not recognise, refuse to apply the preset silently —
show "This preset pins engine `apexload`, which this deployment does not support" and leave the
engine unset. In `configView.ts:184`, render unknown engines as "Unknown engine (`<id>`)".

---

### ARE-09 · P2 · ease of use, consistency
**The preset editor is raw-JSON-only, global-admin-only, and its absence is unexplained**
Area: Test Presets · Route `/golden-configs/:assistantId` · demo · Demo Presenter (shown as
**Admin** in the sidebar) · 1440×900 · screenshot `area-10-preset-detail-raw-ids-apexload.jpg`

Observed on the detail page: exactly one button, **Start run with this config**. No Edit, no
Save new version, no back link to the list, no form controls at all
(`buttons: ["Start run with this config"]`, `links: []`, `inputs: 0`).

The editor exists but is gated on `isGlobalAdmin(consumer)` —
`consumer.role === 'admin' && consumer.scopes.length === 0`
(`GoldenConfigDetailPage.tsx:240`, `auth/RequireRole.tsx:18`). Demo Presenter is `role: 'admin'`
with one project scope, so the gate is false. The sidebar badge says **Admin** and the page
offers nothing and explains nothing.

When it *is* available it is a **raw JSON textarea** ("Edit JSON" → parse-validated
`<textarea class="gc-json-editor">` → "Save new version", `GoldenConfigDetailPage.tsx:172-233`),
even though the same page already renders the gate matrix and prompt pins as structured tables.
PARITY.md row 16 describes this as a "structured config viewer/editor"; it is a structured
viewer plus a JSON blob editor.

Also on this page: **Scope defaults are raw ids** — Project `telecom-modernization`,
Application `app-digital-device`, Environment `env-perf` — with no resolution against the
catalog, while the wizard's Review step resolves the identical ids to "ACC" / "PERFHYPER2" and
offers Copy ID chips. And there is no breadcrumb back to `/golden-configs`, unlike every other
detail page in the app (`/cycles/:id` has "All cycles", `/environments/:id` has a full
breadcrumb).

Recommendation: in `apps/dashboard/src/features/golden-configs/GoldenConfigDetailPage.tsx`,
(a) render a disabled Edit control with "Editing presets requires an unscoped admin key" rather
than nothing; (b) make the gate matrix and prompt pins directly editable and keep JSON behind
"Advanced"; (c) resolve project/app/environment ids through `useCatalog` as `ReviewStep.tsx`
does; (d) add the "Test Presets" breadcrumb link.

---

### ARE-10 · P1 · correctness, ease of use — **Cycles**
**Review actions fire on one unconfirmed click, including archiving an approved version**
Area: Test Cycles · Route `/cycles/:id?tab=<step>&version=<id>` · connected · admin · 1440×900

Exercised end to end on a real cycle (`a71b83a5…`), all four actions:

| Action | Confirmation | Reversible | What the UI says afterwards |
|---|---|---|---|
| `submit` | none | yes (`request_revision`) | "Review action recorded for version 8ae5d150." |
| `approve` | none | **no** (only `archive` remains) | same generic sentence |
| `request_revision` | none | yes (`submit` again) | same generic sentence; the note is buried |
| `archive` | none | yes (`restore`) | same generic sentence |
| `restore` | none | yes | same generic sentence |

Specific problems:

1. **No confirmation anywhere.** `pages.tsx:435-454` calls `review.mutate` straight out of
   `onClick`. On a brand-new draft the buttons render in the order **"Archive in APEX"**,
   **"Submit for review"** — the destructive one first — and both carry `btn btn-secondary`,
   so they are visually identical. Only `approve` is ever `btn-primary` (`pages.tsx:438`).
2. **On an approved version the only available action is an unconfirmed "Archive in APEX".**
   Approval is this feature's compliance artifact; one stray click archives it.
3. **The success message never says what happened.** "Review action recorded for version
   8ae5d150." is printed for all five actions.
4. **`request_revision` leaves no visible trace.** The status reverts to `draft`; the reviewer's
   note is only inside the collapsed `<details>` "Version identity and review history". The
   author sees a plain draft with no "revision requested" banner and no note.
5. **The review note is not cleared after a successful action** (`note` state,
   `pages.tsx:277`), so the next action silently reuses the previous note.
6. **Stale status text accumulates.** "Saved draft version 8ae5d150." (`saved` state,
   `pages.tsx:486`) is never reset and was still on screen many actions later.
7. **Review history is raw.** `pages.tsx:416-421` prints
   `request_revision · 3ebe0a0f08cb4268a05cf563e6ed477b · 2026-10-17T18:27:36.230334+00:00` —
   raw action key, raw consumer UUID, raw ISO timestamp with microseconds, while the line
   directly above it uses `toLocaleString()`.

Recommendation: in `apps/dashboard/src/features/cycles/pages.tsx:424-468`, route every action
through the existing `components/Dialog` the way
`features/context/DocumentsTab.tsx` does for delete — naming the version, the action and its
consequence; style `archive` as `btn-danger` and put it last; make the success message name the
action; add a "Revision requested — <note>" banner to the version header; clear `note` and
`saved` on success; format `r.at` with `toLocaleString()` and resolve `r.actor`.

---

### ARE-11 · P2 · correctness, consistency — **Cycles**
**The one navigation guard in the app is bypassed by the Cancel button next to Save**
Area: Test Cycles · Route `/cycles/:id?tab=scope` · connected · admin · 1440×900
Screenshot: `area-04-cycles-nav-guard-works.jpg`

The guard itself works, and works well. With unsaved deliverable text, clicking a sidebar link
raises **"Leave unsaved changes? / Your current edits have not been saved as a version."** with
*Keep editing* (primary) and *Discard and leave* (secondary); Escape and backdrop click both
resolve to *Keep editing*. Focus is trapped, the rest of the document is `inert`, focus is
restored on close (`components/Dialog.tsx`). This is the best unsaved-changes handling in the
product.

But:

1. **"Cancel editing" discards everything with no prompt.** Verified: title
   `ARE scope v1 — add-a-line peak` plus a filled Requirements field, click *Cancel editing*
   → editor closes, no dialog, text gone. `onCancel` (`pages.tsx:716`) just sets
   `editing = null`; the blocker only covers router navigation. The button sits immediately
   beside *Save draft version*.
2. **The other two dialogs are visually broken.** `ProviderScriptImport.tsx:338-339` and
   `CycleHandoff.tsx:281-282` render bare `<button>Keep editing</button><button>Discard and
   leave</button>` with **no class and no wrapper** — unstyled native buttons jammed together
   with no gap, and no explanatory body text, inside an otherwise styled dialog
   (`area-06-cycles-handoff-dialog-unstyled.jpg`).
3. **`ProviderScriptImport`'s dirty check is wrong.** `dirty = Boolean(parameters || coverage)`
   (`ProviderScriptImport.tsx:37`) ignores the `title` and `plan_version_id` the same form
   requires, so a partly filled import leaves unguarded.
4. **Opening the editor disables the entire workspace.** All eight step buttons *and* Refresh
   are `disabled` from the moment `CycleEditor` mounts — before anything is typed
   (`pages.tsx:551`, `:566`) — with no explanation of why they are greyed out.

Correction to the review brief: Cycles is **not** the only place registering a guard —
`features/admin/ConsumerKeyLifecycle.tsx:109` does too (priority 100).

Recommendation: in `apps/dashboard/src/features/cycles/pages.tsx:716`, make `onCancel` consult
the same dirty state and reuse the leave dialog; give the two dialogs in
`ProviderScriptImport.tsx:330` and `CycleHandoff.tsx:273` the same markup as
`CycleEditor.tsx:341` (`.cycle-actions` + `btn btn-primary` / `btn btn-secondary` + a body
sentence); widen `ProviderScriptImport`'s `dirty` to cover `title` and `plan`; drop the
blanket `disabled` and let the guard do its job.

---

### ARE-12 · P2 · correctness, ease of use — **Cycles**
**Concurrent edits surface as a bare "Cycle request failed (409)" with a copy-paste recovery**
Area: Test Cycles · Route `/cycles/:id?tab=scope` (editor open) · connected · admin · 1440×900
Screenshot: `area-05-cycles-409-unstyled-raw.jpg`

Repro (two principals, both real):
1. Open the scope deliverable editor and fill five fields.
2. From another client, write any version to the same cycle (`POST /v1/cycles/{id}/versions`),
   bumping `revision` 1 → 2. The editor pins its revision at mount by design
   (`CycleEditor.tsx:100`).
3. Click **Save draft version**.

Expected: a plain-language explanation of what changed, by whom, and a way to keep the typed
text while rebasing onto the new revision.
Actual: **"Cycle request failed (409)"** followed by *"Your text is retained. If the cycle
changed, copy your changes before cancelling and reopening the latest version."* The user must
hand-copy five long textareas, click *Cancel editing* — which discards without a prompt
(ARE-11) — reopen, and paste. There is no reload-and-keep, no merge, no indication of what the
conflicting change was.

The message is also **unstyled**: `CycleEditor.tsx:332` uses `className="tonal-card warning"`,
and `.tonal-card` is defined only in `features/runs/run-detail.css`. Measured live on
`/cycles`: `background: rgba(0,0,0,0)`, `padding: 0px`, `border: none`, **0 matching CSS rules
loaded**. The error renders as ordinary black body text indistinguishable from help copy.
*(This belongs to the existing `.tonal-card` finding — the two new occurrences in my areas are
`features/cycles/CycleEditor.tsx:332` and `features/cycles/pages.tsx:386`.)*

Recommendation: in `apps/dashboard/src/features/cycles/api.ts:12-28`, map 409 to a dedicated
message ("Someone else changed this cycle while you were editing — revision 1 → 2"), and in
`CycleEditor.tsx:331` add a **"Keep my text and rebase onto revision N"** button that re-pins
`revision` from a fresh fetch without clearing `content`.

---

### ARE-13 · P1 · correctness, consistency — **Cycles**
**The UI gates on role, the server gates on scope — operators are shown actions that always 403, and denial is rendered as "not found"**
Area: Test Cycles · Routes `/cycles`, `/cycles/:id` · connected · operator and viewer keys

Verified against the API (role probes; no browser session was switched — see "Could not
reach"):

| Call | admin | operator | viewer |
|---|---|---|---|
| `GET /v1/cycles` | 200, 1 cycle | 200, **`[]`** | 200, **`[]`** |
| `GET /v1/cycles/{existing id}` | 200 | **404 "Cycle not found"** | **404 "Cycle not found"** |
| `POST /v1/cycles` | 201 | **403 "project is outside this consumer's scopes"** | 403 |
| `POST /v1/cycles/{id}/versions` | 201 | 404 (scope) | 403 "Requires role 'operator' or higher" |
| `POST /v1/cycles/{id}/actions` | 200 | reaches handler (404 on fake id) | 403 |

Consequences in the UI:

1. `pages.tsx:193` shows **New cycle** to anyone `roleAtLeast('operator')`. An operator fills
   the whole form (name, project, application, environment, mode) and gets a 403 rendered as a
   bare `<p role="alert">` at the bottom (`pages.tsx:166-170`).
2. The Project field is **free text** (`pages.tsx:104-113`) defaulted from
   `consumer?.scopes[0]?.project_id ?? ''`. It is scope-unaware. The wizard solved exactly this
   problem: `ScopeStep.tsx:141` renders a **select of authorized projects** when the consumer is
   scoped, plus a `<datalist>` of known project ids when it is not.
3. A user without access sees **"No cycles on this page. Create a cycle to assemble your first
   deliverables."** — advice they cannot act on — and a direct link gives
   "Could not load cycle / Cycle request failed (404) / Retry"
   (`area-08-cycle-404.jpg`), indistinguishable from a deleted cycle. Retry is useless for a 404
   and there is no "Back to all cycles".

The app already has a scope-aware helper: `canMutateAudience(consumer, projectId, appId)`
(`auth/RequireRole.tsx:33`), used by `WorkItemsPage.tsx:29` and `DocumentsTab.tsx:22`, and
`NewRunWizard.tsx:304` uses `canPersistWizardDraft` and even prints *"Draft saving requires
project-wide access."* Cycles (`pages.tsx:193`, `:516`) and `EnvironmentsPage.tsx:431` use
role-only `roleAtLeast`.

Recommendation: in `apps/dashboard/src/features/cycles/pages.tsx:193` and `:516`, switch to
`canMutateAudience(consumer, project, app)`; replace the free-text Project input at
`pages.tsx:104` with the authorized-project select pattern from `ScopeStep.tsx:141`; in
`pages.tsx:496`, special-case 404/403 with "You don't have access to this cycle" and a link to
`/cycles`. Apply the same to `features/environments/EnvironmentsPage.tsx:431`.

---

### ARE-14 · P2 · correctness, ease of use — **Cycles**
**"Next action" tells a brand-new cycle to approve something that does not exist, and barely changes**
Area: Test Cycles · Route `/cycles/:id?tab=overview` · connected · admin · 1440×900
Screenshot: `area-03-cycles-overview-next-action.jpg`

On a cycle created seconds earlier, with every deliverable showing "Not started":

```
Next action: Approve an active scope version
• Approve an active scope version
• Approve an active test plan version
• Approve an active readiness version
• Approve an active script version
• Provider access, script binding, capacity and readiness freshness require execution preflight
```

The first actual action is *create a scope draft*; approving is three steps away. The banner is
`cycle.blockers[0] ?? 'Review deliverables'` (`pages.tsx:558`) and duplicates the first bullet
of the list immediately below it. It did not change across archive / restore / submit /
request_revision — only approval moved it on.

Also on Overview: **Executions has no card**, because `pages.tsx:595` filters `overview` and
`executions` out of the grid, while the nav above lists all eight steps.

Recommendation: in `apps/dashboard/src/features/cycles/pages.tsx:557`, derive the next action
from the actual state of the first incomplete deliverable ("Create a scope draft" → "Submit the
scope for review" → "Approve the scope"), and stop repeating `blockers[0]` in both the banner
and the list. Give Executions a card with its preparation count.

---

### ARE-15 · P2 · thoroughness, ease of use — **Cycles**
**Dead ends with no empty-state guidance, inconsistently handled inside one feature**
Area: Test Cycles · Routes `?tab=script`, `?tab=executions` · connected · admin · 1440×900

In this deployment (`GET /v1/cycles/{id}/provider-scripts` → `{"connections":[],"scripts":[],
"total":0,"blockers":[]}`):

- **Import from the APEX Load library** opens to a connection `<select>` containing only
  "Select a provider connection" — no message, no explanation, no link to
  `/admin/connections`. A dead end with no words (`ProviderScriptImport.tsx:84-91`).
- **Execution connection** on the Executions step is likewise a one-option select reading
  "Not selected — preflight will be blocked", with no reason given.
- **Launch owner** is a `<select>` with a single option ("APEX pipeline") until a script is
  chosen — a control with no choice.
- By contrast **ScriptStorage** handles the same situation well: *"Ask an administrator to
  configure an enabled S3-compatible artifact connection for this project."*

Recommendation: copy the `ScriptStorage.tsx` empty-state pattern into
`features/cycles/ProviderScriptImport.tsx:84` and `features/cycles/ExecutionWorkspace.tsx`
(connection select), and hide "Launch owner" until more than one authority is available.

---

### ARE-16 · P2 · correctness, ease of use — **Cycles**
**Executions is a well-built preflight bolted to a disabled dispatcher, and reveals plan-level constraints only at the end**
Area: Test Cycles · Route `/cycles/:id?tab=executions` · connected · admin · 1440×900
Screenshot: `area-11-cycles-preflight-blockers-flat.jpg`

The step's own words: *"**Cycle dispatch is not enabled**; preparing an execution does not
launch a test."* After building four approved deliverables, preparing an execution produced
twelve checks — seven **Blocked**, one **Not checked**, five **Ready**. The content is
genuinely good: every blocker names its remedy.

Problems:

1. One blocker is *"**Arrival-rate scheduling: Blocked** — The pipeline execution contract does
   not support arrival-rate scheduling. Revise the workload; arrival rate will not be silently
   ignored."* The Test Plan editor offers **"Arrival rate (requests per second) (optional)"**
   with no hint that filling it blocks execution. The constraint surfaces four deliverables and
   one approval cycle later.
2. The twelve checks render as a flat bulleted list in which **Blocked**, **Not checked** and
   **Ready** differ only by a bold word — no colour, no icon, no grouping, no count. Seven
   blockers are visually identical to five passes.
3. The workload summary prints **"Target: `326c7d3e5a264051ba19571d316fb6cd`"** — the raw
   environment id — on a page whose own header four lines above resolves it to **PERFHYPER2**.
4. A prepared execution has no delete/discard action.

Recommendation: in `apps/dashboard/src/features/cycles/ExecutionWorkspace.tsx`, group the
checks into "Blocked (7)" / "Not checked (1)" / "Ready (5)" with the existing `status-badge`
tones; resolve the environment id through `useEnvironments` as the page header already does;
surface the arrival-rate constraint as inline help on the `arrival_rate` field in
`CycleEditor.tsx`.

---

### ARE-17 · P2 · consistency, ease of use — **Cycles**
**Linked runs and related runs are identified by engine name and UUID, never by test title**
Area: Test Cycles · Routes `?tab=executions`, `?tab=report` · connected · admin · 1440×900

- The **"Recent run in this project and application"** select lists
  `sim · attempt 1 · completed · 10/17/2026, 2:12:27 PM · 01a14b10-a977-7102-95be-56ec6884b32f`
  for every option. The runs have titles — Home and `/runs` show them — but the picker shows a
  UUID. "Link existing run" is therefore a guess.
- After linking, the row reads **"sim · attempt 1 · engine status completed · bindings
  unverified"**. There *is* a link (to `/runs/<id>/phases/execution`), but its text is
  `sim · attempt 1`.
- The Results & Reports baseline selector repeats the same UUID labels.

Recommendation: in `apps/dashboard/src/features/cycles/ExecutionWorkspace.tsx` and
`features/cycles/CycleResults.tsx`, label runs `"<title> · <engine> attempt <n> · <date>"` and
keep the id in a secondary chip, matching `/runs` and the wizard Review step.

---

### ARE-18 · P3 · correctness, consistency — **Cycles**
**Deliverable editor defects: a textarea for an ID field, fractional virtual users, raw enum labels, blank "required" values**
Area: Test Cycles · Route `/cycles/:id?tab=*` (editor) · connected · admin · 1440×900

All verified live against the backend's own field schema:

| Problem | Evidence | Source |
|---|---|---|
| `inventory_snapshot_id` is declared `control: 'text'` but renders as a **4-row textarea** | DOM `tagName: TEXTAREA` for `#cycle-field-inventory_snapshot_id` | `CycleEditor.tsx:62` has no `'text'` branch; everything that is not `select`/`number` becomes a textarea |
| "Concurrent users" accepts **1200.75** | `checkValidity() === true` for `1200.75` | `CycleEditor.tsx:60` hard-codes `step="any"` for every number field |
| Enum values leak as labels, inconsistently | Execution platform options `apex-load`, `loadrunner`; Script format options `apex native json`, `web http html`, `other import` | `CycleEditor.tsx:50` does `option.replaceAll('_',' ')` with no label map |
| A required field saved as `""` renders as a **blank row**, while a missing key renders "Not provided" | "Execution platform" row empty; validation issue said "Execution platform is required" | `pages.tsx:380` `String(content[key] ?? 'Not provided')` — `??` does not catch `''` |
| `*` / `(optional)` are purely visual | every starred field has `required === false` and no `aria-required` | `CycleEditor.tsx:24-38` never sets `required`/`aria-required` |
| Readiness fields carry **no help text** at all, while scope fields do | `aria-describedby` null on all seven | backend `help` not supplied for `readiness` |
| No picker or link from `inventory_snapshot_id` to the environment's inventory | the app has `/environments/:id` with snapshots and a Rescan button | — |

Recommendation: in `apps/dashboard/src/features/cycles/CycleEditor.tsx` add a `control: 'text'`
→ `<input type="text">` branch; use `step={field.integer ? 1 : 'any'}`; add a label map for the
engine/format enums (reusing `ENGINE_CAPTIONS` from `new-test/steps/ConfigStep.tsx`); mirror
`*` with `aria-required="true"`; in `pages.tsx:380` treat `''` as "Not provided"; add an
inventory-snapshot picker sourced from `useInventory` on the cycle's environment.

---

### ARE-19 · P3 · ease of use, consistency — **Cycles**
**The "diff" prints both versions in full instead of diffing them**
Area: Test Cycles · Route `/cycles/:id?tab=report&version=…` · connected · admin · 1440×900

Selecting a comparison version renders, for **every** field, a "Comparison version" paragraph
and a "Selected version" paragraph, with " · Changed" appended to the heading when they differ
(`pages.tsx:349-384`). For two near-identical report versions this doubled a long page with no
highlighting at all; for identical fields it prints the same text twice.

The same product ships a real diff for prompts — `features/prompts/PromptDiff.tsx` uses
`@codemirror/merge`'s `unifiedMergeView` with inserted/deleted chunks and a change gutter.

Recommendation: in `apps/dashboard/src/features/cycles/pages.tsx:349`, render only changed
fields by default (with "Show unchanged (N)"), and reuse `PromptDiff` for the multiline fields.

---

### ARE-20 · P3 · ease of use — **Cycles**
**Capturing report evidence leaves you on the stale version; two versions share one title**
Area: Test Cycles · Route `/cycles/:id?tab=report` · connected · admin · 1440×900

Clicking **"Capture evidence as new report draft"** creates a new version but leaves the page
on the previous one, which still reads *"This report has no frozen execution summaries."* The
only route forward is a small "Open captured draft" link inside a status line. Both versions
then appear in the Selected-version picker with the **identical title**
`ARE report v1 — 26.3.1 · draft`, distinguishable only by an 8-character hex.

Recommendation: in `apps/dashboard/src/features/cycles/ReportEvidence.tsx`, navigate to the new
version on success (as `CycleEditor`'s `onSaved` does) and give the captured draft a derived
title such as "… (evidence captured <date>)".

---

### ARE-21 · P3 · consistency, accessibility — **Cycles**
**Owner and author are raw consumer UUIDs; current step and keyboard focus look the same**
Area: Test Cycles · Routes `/cycles`, `/cycles/:id` · connected · admin · 1440×900 and 390×844

- Every cycle card and the workspace header print **`Owner: 3ebe0a0f08cb4268a05cf563e6ed477b`**,
  and every version prints **`Created by 3ebe0a0f08cb4268a05cf563e6ed477b`**, although
  `GET /v1/system/info` returns `consumer.name = "apex-admin"`. `/prompts` gets this right —
  its version list shows **`apex-admin · just now`** and `seed_prompts · 2h ago`. At 390 px the
  UUID wraps across two lines in the header. *(Distinct from the known
  `actor: "unknown"` finding — here an opaque id is recorded and displayed.)*
- The cycle list card shows `project_id` and mode but not the application, environment, or any
  progress/status — only "revision N".
- The step nav marks the current step with `outline: 2px solid currentColor` + `offset: 2px`
  (`cycles.css:74`), i.e. a teal ring; the app's keyboard focus ring is a 3 px ring in the same
  teal (`rgb(0,118,157)`). During keyboard navigation "current step" and "focused step" are the
  same visual treatment, and there is no other current-step styling (no fill, no underline).
- Narrow viewport is otherwise handled well: at 390×844 the 8-button nav reflows to a 2-column
  grid, no horizontal overflow (`document.scrollWidth === innerWidth === 390`).

Recommendation: resolve the consumer id to its name (the value is already in `AuthProvider`
state) in `apps/dashboard/src/features/cycles/pages.tsx:234`, `:309`, `:418`; in
`apps/dashboard/src/features/cycles/cycles.css:74`, give `aria-current="page"` a filled/inverted
treatment instead of an outline so it cannot be confused with focus.

---

### ARE-22 · P2 · consistency — **Resources**
**One product, three different behaviours for the same "Project" field; the one good pattern is not reused**
Area: Resources · Routes `/context`, `/evidence`, `/cycles`, `/runs/new?step=scope` · connected

| Screen | Project control | Behaviour |
|---|---|---|
| `/evidence` | `<select>` of **names** ("All projects", "Demo") | best |
| `/runs/new` Scope | `<input list="known-projects">` with a datalist of real ids, or a select of authorized projects when scoped | good |
| `/context` (both tabs) | free text, placeholder **`proj-alpha`**, **no datalist** | a non-existent example id as the only hint |
| `/cycles` New cycle | free text, no datalist, scope-unaware (see ARE-13) | worst |

`/context` also still has the two unfixed items from
`docs/qa/resources-audit-2026-09-14/REPORT.md` §06: the **filename is not a link** (no preview
or detail view) and the **only row action is "Delete…"** (no "Use in test", not in an overflow).
What *was* fixed: Documents is now the landing tab, visible field labels are present, Evidence
moved to its own route, the sidebar/heading now read "Knowledge & Documents".

Credit: the document delete dialog is the best confirmation in the product —
*"This permanently removes are-review-context.md (234 B). Runs that already consumed it keep
their copies."* with a `btn-danger` action. It is the model ARE-10 should copy.

Recommendation: extract the `known-projects` datalist pattern from
`apps/dashboard/src/features/new-test/steps/ScopeStep.tsx:155` into a shared `ProjectField`
component and use it in `features/context/DocumentsTab.tsx`,
`features/context/SummariesTab.tsx` and `features/cycles/pages.tsx:104`. Make the filename in
`DocumentsTab.tsx` open a details view and move Delete into an overflow menu.

---

### ARE-23 · P2 · thoroughness, ease of use — **Resources**
**The Evidence "test" filter is an unsearchable 51-option list with duplicates, UUIDs and a 372-character option**
Area: Resources · Route `/evidence` · connected · admin · 1440×900

Measured on the live select (`aria-label="Filter by test"`): **51 options**, longest option
**372 characters**, and these exact duplicate labels with nothing to tell them apart —
`ACC add-a-line peak certification — candidate 26.3.1` ×2, `… candidate 26.3.0` ×2,
`… baseline 26.2.1` ×2, `CAS demo` ×3, `M2 demo` ×4, `events` ×3, `M1 demo` ×3 — plus untitled
runs listed as bare thread UUIDs. No date, no status, no search, no grouping.

The rest of the workspace is strong: in demo mode the scope rail, Linked/Unlinked chips, source
types and per-test associations all read well.

Recommendation: in `apps/dashboard/src/features/context/EvidencePage.tsx` /
`EvidenceTab.tsx`, replace the `<select>` with a searchable combobox, append the run date and
status to each label, and truncate to a sane length with the full title in `title=`.

---

### ARE-24 · P2 · accessibility, correctness — **walkthrough i (`/prompts`)**
**The prompt version diff renders changed lines at ~1.1:1 contrast — the one thing you came to read is invisible**
Area: Prompts (walkthrough i only) · Route `/prompts/:ns/:name/versions/:v?diff=…`
connected · admin · **apex-light** · 1440×900 · screenshot `area-09-prompt-diff-unreadable.jpg`

Measured in the live DOM:

| Line type | text colour | background | contrast |
|---|---|---|---|
| deleted chunk | `rgb(226,232,240)` | `rgb(254,240,241)` | **1.11 : 1** |
| inserted line | `rgb(226,232,240)` | `rgb(237,251,244)` | **1.16 : 1** |
| unchanged line | `rgb(226,232,240)` | `rgb(26,26,46)` | ~13 : 1 |

WCAG AA requires 4.5:1. In the screenshot the three diff rows are blank pink and green bars.

Cause: `features/prompts/prompts.css:552-566` paints diff lines with the **page-surface** tokens
`--success-bg` / `--danger-bg` (light in `apex-light`) inside a CodeMirror surface that keeps
the **code** tokens `--code-bg` / `--code-text` (dark in every theme, by design — see
`components/viewers/viewers.css:1`). The gutter bars (3 px, `--success` / `--danger`) are the
only usable signal.

Note on ownership: `/prompts` consistency belongs to another reviewer; this is filed here
because it was found during walkthrough i and is a correctness/accessibility defect rather than
a style preference.

Recommendation: in `apps/dashboard/src/features/prompts/prompts.css:552`, define code-surface
diff tokens (e.g. `--code-diff-add-bg` / `--code-diff-del-bg` derived from `--code-bg`) and
set an explicit `color` on `.cm-changedLine` / `.cm-insertedLine` / `.cm-deletedChunk`.

---

### ARE-25 · P2 · consistency, correctness — **walkthrough i (`/prompts`)**
**Creating a version silently activates it; rolling back to one requires a confirmation**
Area: Prompts (walkthrough i only) · Route `/prompts/:ns/:name` · connected · admin · 1440×900

Repro and verification:
1. `story_analysis/system` → **New version** → edit → **Save as v2**. The header immediately
   reads `active v2`. No confirmation, no warning, no "this becomes the active prompt" copy on
   the editor panel. Confirmed server-side:
   `GET /v1/prompts` → `active_version: {version: 2}`. Every subsequent pipeline run in this
   deployment now uses the new text.
2. On the v1 page, **Set this version active** opens a proper dialog: *"Set v1 active? The
   active pointer moves to v1 — APEX canonical catalog 2026-09-08.1. No versions are modified
   or deleted."* with Cancel / `btn-primary` Set v1 active.

The same consequence — moving the active pointer — is confirmed in one direction and silent in
the other.

Recommendation: in `apps/dashboard/src/features/prompts/PromptEditor.tsx` / `PromptDetailPage.tsx`,
either add a "Make this the active version" checkbox (default on) with the consequence stated
beside the Save button, or route Save through the same confirm dialog used for rollback.

Also observed in walkthrough i, lower severity:
- The playground's 202 card shows run id `01a14b2b-3d24-…` while **"Open run" links to thread
  id `01a14b2b-3d22-…`** — the prominent identifier is not the one you can look up. The same
  mismatch occurs in `features/context/SummariesTab.tsx` for generate-now
  (`01a14b2f-02cf-…` displayed, `/runs/01a14b2f-02cc-…` linked). Merge: show the thread id, or
  label both.
- The detail page title is still the generic **"Prompt"** and **Archive still sits beside New
  version** — both carried over unfixed from
  `docs/qa/resources-audit-2026-09-14/REPORT.md` §10.
- Fixed since that audit and worth keeping: the playground is now titled **Template Preview**
  with a **Check template** action and an honest "deterministic stub" caption.

---

### ARE-26 · P3 · consistency — **walkthrough j (`/work-items`)**
**The translation confidence chip survives a hand-edited query; Save hides the query that Edit shows**
Area: Resources · Routes `/work-items`, `/work-items/saved` · connected · admin · 1440×900

1. Natural language → **Translate** produced `text ~ "<the whole sentence>"` with a
   **confidence 90%** chip. I then replaced the provider query by hand with
   `project = ACC AND type = defect AND status = "Open"` and executed it — **the chip still
   reads "confidence 90%"**, now describing a translation that no longer exists.
2. The **Save query** dialog shows only NAME and DESCRIPTION — the query being saved is not
   displayed. The **Edit saved query** dialog shows Name, Provider, Query and Description. Same
   object, two different dialogs.
3. Save is `disabled` with an empty name, with no "required" marker and no explanation.
4. Saving and editing a saved query both complete with **no success message** (no `role=status`
   anywhere); the table silently updates.
5. The Provider field has an `aria-label` but **no visible label** — sighted users see a text
   box containing `stub`.

Everything else in walkthrough j worked cleanly: translate → edit → execute → save → modify, and
the row overflow menu is properly built (`role="menu"`, `aria-expanded`,
`aria-label="Saved query actions: <name>"`, Run / Edit… / Delete…).

Recommendation: in `apps/dashboard/src/features/work-items/WorkItemsPage.tsx`, clear or relabel
the confidence chip (`"edited — translation confidence no longer applies"`) whenever the
provider query diverges from the translated value; show the query (read-only) in the Save
dialog; mark Name required; emit a `role="status"` confirmation after save/edit; add a visible
Provider label.

---

### ARE-27 · P3 · thoroughness — **Resources (environments)**
**Rescan works but the inventory has no provenance, and the Applications page is unchanged from the 2026-09-14 recommendations**
Area: Resources · Routes `/environments`, `/environments/:id` · connected · admin · 1440×900

- **Rescan** (`202`) works: "Never scanned" → "Scanned just now" with a services table. Two
  **Rescan** buttons exist on one page (header and inventory empty state).
- The returned inventory is `checkout-api`, `payment-svc`, `cart-svc` — services unrelated to
  the environment's own hosts (`acc.`, `mulesoft.`, `bsse.perfhyper2.example.test`), because the
  stub connector returns a fixed list. The panel shows **no source/connection attribution**, so
  nothing tells the reader where the data came from or that it is a stub.
- The `stale` chip (`EnvironmentDetailPage.tsx:279`) is backend-driven and was not reachable —
  see "Could not reach".
- `/environments` still offers **New environment** globally alongside **Add environment** per
  application, application names are not links to a workspace, and there is no search or project
  filter — all three unfixed from `docs/qa/resources-audit-2026-09-14/REPORT.md` §01. The
  §02 recommendations for the detail page **were** implemented: breadcrumb
  `Applications / demo / ACC / PERFHYPER2`, "Reference" renamed **Configuration**, JSON options
  collapsed under **Advanced options**, Rescan beside Inventory.

Recommendation: in `apps/dashboard/src/features/environments/EnvironmentDetailPage.tsx:258`, add
the connection name and scan source beside "Scanned <time>"; drop the duplicate header Rescan.
Resolve the breadcrumb's raw `demo` project id to a project name.

---

## Corrections to the review brief

1. **The wizard's step tabs are not among the deficient `role="tablist"` blocks.**
   `NewRunWizard.tsx:496-523` implements `role="tab"` + `aria-controls="wizard-panel-<step>"` +
   roving `tabIndex` + ArrowLeft/ArrowRight/Home/End (`moveTab`, `:449`), and the panels are
   `role="tabpanel"` with `hidden={!active}` and `aria-labelledby`
   (`WizardSection`, `:66-96`). The engine chooser is a correct `role="radiogroup"` with roving
   tabIndex and arrow keys (`ConfigStep.tsx:232-249`). Whatever the count of deficient tablists
   is, the wizard should be excluded from it.
2. **Cycles is not the only place that registers a navigation guard.**
   `features/admin/ConsumerKeyLifecycle.tsx:109` registers one at priority 100.
3. The 2026-09-14 P1 "phase selection also changes prompt focus" is **fixed** —
   `togglePhase` (`ConfigStep.tsx:142`) only moves focus when the focused phase is excluded, and
   the Prompts step now has its own phase selector.

---

## Per-area scorecard (1–5)

### New Test wizard

| Axis | Score | Justification |
|---|---|---|
| Consistency | 4 | Labels, scope bar, Copy-ID chips and step chrome are the most coherent in the app; only the preset/engine coupling and the dead "context summaries" copy break it. |
| Thoroughness | 3 | Empty, loading, validation, permission, resume and large-text states are all handled; stale references (deleted document) and unsaved-on-reload are not. |
| Correctness | 2 | The footer asserts an execution target that is not set (ARE-01), a draft with a deleted document is "complete" and launchable (ARE-02), and two silent data losses (ARE-03, ARE-04). |
| Ease of use | 4 | Six steps, free navigation, autosave, faithful resume, an excellent Review step with a launch-JSON disclosure; the silent clears are the main friction. |
| Accessibility | 4 | Exemplary tabs/radiogroup/labels; one unnamed `role="textbox"` (ARE-05) keeps it off 5. |

### Test Presets / Golden Configs

| Axis | Score | Justification |
|---|---|---|
| Consistency | 2 | Naming is finally uniform, but raw ids where the wizard resolves names, a raw engine id, and no breadcrumb back to the list. |
| Thoroughness | 2 | Loading, error and "no presets" states are handled; populated detail and the editor are unreachable in connected mode, and there is no empty/denied messaging for the admin-only editor. |
| Correctness | 2 | Silent engine downgrade to the simulator under a "config inherited" badge (ARE-08); the list endpoint 500s (ARE-07). |
| Ease of use | 2 | "Start run with this config" is excellent; everything else is read-only with no explanation, and editing is a JSON blob for unscoped admins only. |
| Accessibility | 4 | Structured tables, real headings, labelled chips; nothing specific found against it. |

### Test Cycles

| Axis | Score | Justification |
|---|---|---|
| Consistency | 2 | Role-only gates where the rest of the app is scope-aware, raw UUID authors where `/prompts` resolves names, two of three leave-dialogs unstyled, raw enum labels, three spellings of one engine. |
| Thoroughness | 3 | Empty / loading / 404 / 409 / permission / narrow-viewport are all reached and all render *something*; the states that exist are frequently bare, and three sub-features ship disabled. |
| Correctness | 2 | Unconfirmed review actions on frozen approvals, a "next action" that is impossible on a new cycle, a text field rendered as a textarea, fractional virtual users, `''` shown as blank for a required field. |
| Ease of use | 2 | Preflight blockers and the frozen-dependency model are excellent; getting there means eight steps, four approvals, a guard you can walk around, and a dispatcher that is switched off. |
| Accessibility | 3 | Dialog focus management is the best in the app; `aria-current` on nav is right; but `*` has no `aria-required`, the current-step ring is confusable with focus, and `role="alert"` errors land unstyled. |

### Resources (`/work-items`, `/environments`, `/context`, `/evidence`)

| Axis | Score | Justification |
|---|---|---|
| Consistency | 2 | Four screens, four different Project controls (ARE-22); `/evidence` resolves names, `/context` offers a fictitious placeholder id. |
| Thoroughness | 4 | Empty, populated, filtered, confirm-before-delete, 202-accepted and large-list states are all present and mostly well written. |
| Correctness | 4 | Everything I executed behaved correctly; the defects are labelling (stale confidence chip, displayed-vs-linked ids), not wrong data. |
| Ease of use | 3 | Walkthrough j completed without a dead end; `/evidence` is strong; `/context` documents still cannot be previewed or used, and the evidence test filter is unusable at scale. |
| Accessibility | 4 | Correct `aria-label`s on the query console, a well-built overflow menu, real table semantics; visible labels are the gap, not programmatic ones. |

---

## State × view coverage actually exercised

`✓` exercised · `~` partial · `✗` not reached (reason in the next section) · `–` not applicable

| View / route | empty | loading | populated | very large | validation error | backend 5xx / offline | permission (viewer / operator / admin) | stale / superseded | leaving unsaved |
|---|---|---|---|---|---|---|---|---|---|
| `/runs/new` Scope | ✓ | ✓ | ✓ | ✓ (long title + 100-char request) | ✓ | ✗ | ~ admin UI / API-probed | ✓ (deleted document) | ✓ (reload inside the 1.5 s debounce — edit lost silently, no `beforeunload`) |
| `/runs/new` Config | ✓ (no presets) | ✓ | ✓ | – | ✓ ("Select at least one phase") | ✓ **real 500** on presets | ~ | – | ✓ |
| `/runs/new` Work Items | ✓ | ✓ | ✗ (no saved queries seeded) | – | – | ✗ | ~ | – | ✓ |
| `/runs/new` Context | ✓ | ✓ | ✓ (upload + parse + attach) | ~ | ✓ (type/size paths read in source) | ✗ | ~ | ✓ | ✓ |
| `/runs/new` Prompts | ✓ (no app prompt) | ✓ | ✓ (override authored + edited) | ✓ | ✓ (empty-override issue, source) | ✗ | ~ | – | ✓ |
| `/runs/new` Review | – | ✓ | ✓ | ✓ | ✓ | ✗ | ~ | ✓ | ✓ |
| `/runs/new?draft=` resume | ✓ | ✓ | ✓ (faithful) | – | – | ✗ | ~ | – | ✓ |
| `/golden-configs` | – | ✓ | ✓ (demo, 3) | – | – | ✓ **real 500** (connected) | ~ | – | – |
| `/golden-configs/:id` | – | ✓ | ✓ (demo) | – | – | ✗ (unreachable) | ✓ scoped-admin → no editor, no message | – | – |
| `/cycles` list | ✓ | ✓ | ✓ (1 cycle) | ✗ | ✓ (native required) | ✗ | ✓ API-probed: operator/viewer see `[]` | – | – |
| `/cycles/:id` Overview | ✓ (new cycle) | ✓ | ✓ | – | – | ✗ | ✓ | ✓ (approval-stale copy read) | – |
| `?tab=scope` | ✓ | ✓ | ✓ (draft→submit→revision→approve) | ✓ | ✓ | ✗ | ✓ | ✓ **real 409** | ✓ **guard fires**; ✓ Cancel bypass |
| `?tab=test_plan` | ✓ | ✓ | ✓ (14 fields, approved) | – | ✓ (range + fractional vusers) | ✗ | ✓ | – | ✓ |
| `?tab=readiness` | ✓ | ✓ | ✓ (approved) | – | ✓ | ✗ | ✓ | – | ✓ |
| `?tab=script` | ✓ | ✓ | ✓ (2 versions, approved) | ✓ (150 kB / 50 k / NUL / bad-UTF-8 all tested) | ✓ | ✗ | ✓ | – | ✓ |
| `?tab=script` provider import | ✓ (no connections) | ✓ | ✗ | – | – | ✗ | ✓ | – | ✗ **guard unreachable** |
| `?tab=executions` | ✓ | ✓ | ✓ (prepared + linked run + 12 checks) | – | ✓ (disabled Prepare) | ✗ | ✓ | ✓ ("connection unavailable or changed") | – |
| `?tab=report` | ✓ | ✓ | ✓ (2 versions, evidence captured, compared) | – | ✓ | ✗ | ✓ | ✓ (unfrozen evidence notice) | – |
| `?tab=postmortem` | ✓ | ✓ | ✓ | – | ✓ | ✗ | ✓ | – | ✓ **guard fires** |
| `/cycles/<bad id>` | – | ✓ | – | – | – | ✓ **real 404** | ✓ denial renders as 404 | – | – |
| `/work-items` | ✓ | ✓ | ✓ (translate→edit→execute→save) | – | ✓ (disabled Save) | ✗ | ✓ API-probed | ✓ (stale confidence chip) | ~ (Escape discards, no warning) |
| `/work-items/saved` | – | ✓ | ✓ (edit + persist) | – | – | ✗ | ✓ | – | ~ |
| `/environments` | – | ✓ | ✓ | ✗ | – | ✗ | ~ | – | – |
| `/environments/:id` | ✓ ("Never scanned") | ✓ | ✓ (rescan 202 → services) | – | – | ✗ | ~ | ✗ stale chip | – |
| `/context?tab=documents` | ✓ | ✓ | ✓ (upload + delete w/ confirm) | – | – | ✗ | ~ | – | – |
| `/context?tab=summaries` | ✓ | ✓ | ✓ (generate-now 202 → run link) | – | – | ✗ | ~ | – | – |
| `/evidence` | ✓ (connected) | ✓ | ✓ (demo, 12 items, scope rail) | ✓ (51-option filter, 372-char option) | – | ✗ | ~ | – | – |
| `/prompts*` (walkthrough i only) | – | ✓ | ✓ (edit→v2→diff→rollback→playground) | – | ✓ (disabled Save) | ✗ | ~ | – | ~ |

Themes: `apex-light` only. Viewports: 1440×900 everywhere; 390×844 on `/cycles/:id`.

---

## Could not reach, and why

1. **Injected backend 5xx / offline for arbitrary routes.** The documented technique — patching
   `window.fetch` from the page — **does not work against this build**.
   `api/apexClient.ts:80` captures `globalThis.fetch` once, into a lazily created module-level
   singleton (`const transport = getDevApexFetch() ?? globalThis.fetch`), and the only caller of
   `resetApexClient()` is a dev-data-mode subscription. Verified empirically: with
   `window.fetch` replaced and a fault registered for `/v1/catalog/environments`, the interceptor
   recorded **0 hits** and the real request completed. I did not stop the shared backend. I
   substituted genuine server failures instead: a real **500** (`/assistants/search`, ARE-07), a
   real **409** (CAS conflict forced from a second client, ARE-12), a real **404**
   (`/cycles/<bad id>`, ARE-13), and real **403s** across roles. Fix for future reviewers: export
   a test hook that calls `resetApexClient()`, or have `fetchWithoutRedirects` resolve
   `globalThis.fetch` at call time.
2. **Browser role switching (viewer / operator in the UI).** The API key lives in
   **`localStorage`** under `apex.apiKey` (`auth/keyStorage.ts:3`), which is shared by every tab
   on `127.0.0.1:4175`. Signing out to switch roles would have signed out the other reviewers
   using that origin. I verified authorization against the API with each key and read the UI
   gates in source instead; the UI-vs-server mismatch in ARE-13 is established from both sides,
   but no screenshot of an operator's `/cycles` exists.
3. **`ProviderScriptImport`'s unsaved-changes dialog.** Its `dirty` flag only becomes true once
   the `parameters` or `coverage` fields exist, and those render only after a provider
   connection **and** a remote script are selected. This deployment has zero APEX Load
   connections (`/v1/cycles/{id}/provider-scripts` → `connections: []`), so the fields are
   unreachable. The guard was reviewed in source (ARE-11 item 3); the other two dialogs were
   exercised live.
4. **Test Presets detail and editor in connected mode** — blocked by the 500 (ARE-07). The
   detail page was reviewed in demo, where the editor is gated off by `isGlobalAdmin` (ARE-09),
   so the **Edit JSON → Save new version** path was not executed in either environment.
5. **Cycles with `features.cycles` disabled.** The `Disabled()` component (`pages.tsx:52`) was
   read in source; turning the flag off would have required restarting the shared backend.
   Demo mode does correctly hide the sidebar entry.
6. **The environment inventory `stale` chip.** `snapshot.stale` is set by the backend
   (`EnvironmentDetailPage.tsx:279`); the seeded environment had `last_snapshot: null` and a
   fresh rescan is never stale. Only "Never scanned" and "Scanned just now" were observed.
7. **A real APEX Load or LoadRunner engine.** Only `sim-engine` is registered and enabled; no
   test was launched in any environment.
8. **Work-items populated state in the wizard**, and **`/environments` at scale** — one
   application, one environment, zero saved queries seeded in connected mode.

---

## Changes this review made to the running environments

Connected (`:4175` / `:2024`):

- Cycle **`a71b83a5763f45f588b4de1b11f063e6`** — "ARE review cycle — ACC add-a-line 26.3.1",
  with approved scope / test plan / readiness / script versions, two report drafts, one
  execution preparation, one linked run, and one throwaway postmortem version titled
  "ARE concurrency probe (ignore)" created via the API to force the 409 in ARE-12.
- Wizard draft **`324626b72fb24c0a8984fe36eb0585d2`** — "ARE review — add-a-line peak soak
  (do not launch)". **Never launched.**
- Saved query **"ARE review — ACC open defects"** (provider `stub`).
- Prompt `phase/story_analysis/system`: a **v2** was created and then **rolled back — v1 is
  active again**, matching the seeded state. v2 remains in version history.
- Two accepted runs from the Template Preview and generate-now probes.
- Document `are-review-context.md` was uploaded and then **deleted** (deliberately, for ARE-02).
  `/v1/documents` is empty again.

Demo (`:4173`): one wizard draft `draft-102` created by "Start run with this config". Clearable
with "Reset demo" in the sidebar account panel.

No application, backend or `package.json` code was modified.
