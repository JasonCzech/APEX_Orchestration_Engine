# New-run wizard — route wiring (D4, wizard agent)

No `src/routes/router.tsx` changes: `/runs/new` already lazy-loads
`NewRunWizardPage` from `@/features/runs/pages`; that module now re-exports the
real wizard:

```tsx
// src/features/runs/pages.tsx (the ONE line this feature edited there)
export { NewRunWizardPage } from '../new-test/NewRunWizard'
```

## URL contract for `/runs/new`

`?step=scope|work-items|context|config|prompts|review & draft=<id>`

- `step` defaults to `scope`; unknown values fall back to `scope`. Step changes
  REPLACE history (no back-button spam). Deep links to any step work; earlier
  invalid steps surface on the review step's issue list rather than blocking.
- `draft` appears (replace-history) after the first autosave creates the
  server draft, and is honored on mount: the payload is fetched and restored.

## What ships in this folder

| Piece | File |
|---|---|
| `NewRunWizardPage` (shell: horizontal tabs + content + sticky footer + resume picker) | `NewRunWizard.tsx` |
| Steps 1–6 | `steps/{ScopeStep,WorkItemsStep,ContextStep,ConfigStep,PromptsStep,ReviewStep}.tsx` |
| `WizardDraft` + validation + prereq hints + gates mapping + `buildLaunchPreview` | `wizardState.ts` |
| Debounced autosave (1.5s, create-then-update serialized) | `useDraft.ts` |
| Launch mutation (`POST /v1/pipelines`, including resolved context) | `useWizardLaunch.ts` |

New shared hooks (this agent's files): `src/api/hooks/{useCatalog,useDrafts,
useWorkTracking,useDocuments,useAssistants}.ts`. `queryKeys.ts` got append-only
keys (`prompts.listNamespace/byId`, `catalog.applicationsBy/environmentsBy`,
`workItems.key`, `documents.listBy`, `drafts.*`).

## Launch contract (verify against backend on drift)

`buildLaunchPreview(draft)` drives the review step's launch plan and the domain
request assembled in `useWizardLaunch`:

- `POST /v1/pipelines` receives the selected assistant id, full configurable,
  document ids, and inline work-item context packets. The server resolves full
  document text before creating the thread and run.
- `configurable` mirrors `src/apex/graphs/pipeline/configurable.py`:
  - `gates` is ALWAYS the explicit 7-phase matrix (all_gated -> all `gated`,
    all_auto -> D2's `ALL_AUTO_GATES` import, custom -> the wizard matrix).
  - `phases` omitted when all 7 (backend default), else the canonical-order
    subset. Phase-prerequisite gaps BLOCK launch because this path creates a new
    thread with no prior results to reuse.
  - `prompt_overrides["phase/<p>"] = {content}` — replaces the SYSTEM prompt
    only (src/apex/services/prompts.py resolution order).
  - Work-item keys resolve into input `context_packets`; document ids are
    resolved server-side into packets containing the stored extracted text.
  - Golden configs retain their full assistant bundle (connections, models,
    limits, prompt pins, backend, load settings), with visible wizard edits
    layered on top.

## Drafts

Server drafts via `/v1/drafts` (payload = `WizardDraft` verbatim, parsed back
leniently per field). Title falls back to "Untitled run". Launch deletes the
draft best-effort (failures ignored). The "Resume draft" picker shows only on
a fresh visit (no `?draft`, nothing typed) when `listDrafts` returns rows.
