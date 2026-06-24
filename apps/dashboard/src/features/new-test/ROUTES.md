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
| Launch mutation (extends D2 `launchRun` semantics) | `useWizardLaunch.ts` |

New shared hooks (this agent's files): `src/api/hooks/{useCatalog,useDrafts,
useWorkTracking,useDocuments,useAssistants}.ts`. `queryKeys.ts` got append-only
keys (`prompts.listNamespace/byId`, `catalog.applicationsBy/environmentsBy`,
`workItems.key`, `documents.listBy`, `drafts.*`).

## Launch contract (verify against backend on drift)

`buildLaunchPreview(draft)` is the single source for BOTH the review step's
"Launch payload (exact)" JSON and the SDK calls in `useWizardLaunch`:

- `threads.create({metadata: {project_id, app_id?, title}})`
- `runs.create(threadId, 'pipeline', {input: {title, request},
  config: {configurable}, ...D2 stream options})` (durability sync, resumable,
  multitask reject, updates+messages-tuple+custom with subgraphs)
- `configurable` mirrors `src/apex/graphs/pipeline/configurable.py`:
  - `gates` is ALWAYS the explicit 7-phase matrix (all_gated -> all `gated`,
    all_auto -> D2's `ALL_AUTO_GATES` import, custom -> the wizard matrix).
  - `phases` omitted when all 7 (backend default), else the canonical-order
    subset. Phase-prereq gaps WARN (plan 4: "earlier in plan or succeeded on
    thread") — the backend plan resolver is authoritative.
  - `prompt_overrides["phase/<p>"] = {content}` — replaces the SYSTEM prompt
    only (src/apex/services/prompts.py resolution order).
  - Wizard context refs map to `pre_execution_context` as prefixed strings:
    `workitem:<key>`, `document:<id>`, `context:<id>`. The backend field is
    declared but not yet consumed by any phase node — revisit when it is.

## Drafts

Server drafts via `/v1/drafts` (payload = `WizardDraft` verbatim, parsed back
leniently per field). Title falls back to "Untitled run". Launch deletes the
draft best-effort (failures ignored). The "Resume draft" picker shows only on
a fresh visit (no `?draft`, nothing typed) when `listDrafts` returns rows.
