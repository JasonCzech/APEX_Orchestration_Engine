# Approvals inbox — route wiring (D3, inbox agent)

This feature does NOT touch `src/routes/router.tsx`. Both approvals routes in
the D0 route table already lazy-load from `@/features/approvals/pages`, and
`pages.tsx` in this folder now re-exports the real screen (same minimal-diff
pattern the runs grid documented in D1) — **wiring is already done**:

| Export | Route | Behavior |
|---|---|---|
| `ApprovalsInboxPage` | `/approvals` | keyboard-first gate queue + preview |
| `ApprovalDetailPage` (alias of `ApprovalsInboxPage`) | `/approvals/:threadId/:interruptId` | same screen with that gate pre-selected |

## URL contract

- `/approvals` — no params; the oldest open gate auto-selects.
- `/approvals/:threadId/:interruptId` — deep link that pre-selects the queue
  item. `:interruptId` picks the matching interrupt on the thread when it is
  still pending; if a discuss/revise loop has re-interrupted (new id), the
  thread's CURRENT pending interrupt renders instead (a gate instance is
  identified by `interrupt_id`, and stale links must not dead-end). Selection
  changes from clicks/keys are LOCAL state only — they do not rewrite the URL
  (the two routes mount different route objects, so navigating per keystroke
  would remount the page and drop focus).

## Routes this screen links to

- `/runs` (empty-state ghost button) and `/runs/:threadId` (preview title,
  "Open run", the `o` shortcut) — both real since D1.

## Data

`useApprovalsInbox()` reuses `usePipelines({status:'interrupted', limit:100})`
— the same `queryKeys.pipelines.list` cache family and 15s visibility-aware
poll as the runs grid; rows are filtered client-side on `pending_gate != null`
and sorted oldest-`updated_at` first. The Sidebar badge calls the same hook,
so the badge and the inbox share ONE cache entry / ONE poll (no new query
keys were needed — `queryKeys.approvals.*` remains reserved). The hook also
diffs consecutive polls and exposes `removedItems` (gates resumed elsewhere)
for one cycle so the page can gray rows inline instead of yanking them.

The preview pane reads `useThreadState(threadId)` (the same facade snapshot
the run-detail page uses) and mounts the shared gate module on the pending
interrupt.

## GateModule integration contract (consumed from src/hitl — gate agent)

Import path: `import { GateModule } from '@/hitl/GateModule'` — the
SELF-CONTAINED module (mounts its own `useGate` machine over the shared
thread-state cache entry). Types are canonical in `src/hitl/GateModule.tsx`;
`./gateModuleContract.ts` re-exports them and documents the consumer-side
semantics:

- `<GateModule threadId interrupt compact onOutcome handleRef />`, keyed by
  `interrupt.interrupt_id` (a re-interrupt mints a new id → fresh module +
  re-armed `onOutcome`).
- `onOutcome` (once per gate instance):
  `{type:'resumed', action}` for approve/modify/skip_phase/abort → row grays
  as **actioned**, selection auto-advances; discuss/revise → NOT terminal
  (gate reopens in place); `{type:'superseded'}` → row grays as **actioned
  elsewhere**, selection auto-advances.
- `handleRef: Ref<GateModuleHandle>` → `isActionable()` / `invoke(action)` /
  `focus()` for the keyboard layer. All resume I/O (pessimistic 202/409 CAS
  handling + invalidations) stays inside the machine; this page never calls
  the resume endpoint.

## Keyboard map (document-level listener, mounted with the page)

`j/k` or `↓/↑` navigate · `Enter` focus preview · `o` open run ·
`a/m/s/x` → `invoke('approve'|'modify'|'skip_phase'|'abort')` gated on
`isActionable()` (`m` is modify-FOCUS — the handle moves focus into the
prompt editor, no submit) · `?` shortcuts overlay · `Escape` closes it.
Shortcuts yield while typing (input/textarea/select/contenteditable/
`.cm-editor`).

## Shell changes (surgical)

- `src/components/layout/Sidebar.tsx`: local `ApprovalsBadge` component
  (consumes `useApprovalsInbox`) rendered inside the Approvals NavLink —
  `dash-badge` count when > 0, `pulse` class when any gate has waited > 15m
  (`STALE_GATE_MS`).
- `src/components/layout/Sidebar.css`: `.nav-badge` layout glue (hidden when
  the sidebar is collapsed / on narrow viewports).
- `src/test/server.ts`: default `GET */v1/pipelines` → empty list, because the
  badge polls on every authenticated shell mount; per-test `server.use(...)`
  overrides still win.

## Tests

`__tests__/ApprovalsInboxPage.test.tsx` (13) with fixtures/handlers in
`__tests__/approvalsTestHandlers.ts` and a typed module mock in
`__tests__/gateModuleMock.tsx`. The mock is PARTIAL — only the self-contained
`GateModule` export is replaced (typed against the real `GateModuleProps`, so
contract drift fails compile); `GateModuleView` and friends stay real for the
run-detail page mounted by the `o`-shortcut test.
