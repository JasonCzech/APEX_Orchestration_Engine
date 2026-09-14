# Review workspace route contract

Approvals and Test History share one gate-specific Review Workspace. The
screen keeps the review queue, run context, phase output, artifacts, dialogue,
timeline, and decision controls together.

The primary sidebar exposes one `Tests` destination with a pending-review
badge. It opens `All runs` by default; operators switch to `Needs review`
inside the Tests view.

| Export | Route | Behavior |
|---|---|---|
| `ApprovalsInboxPage` | `/approvals` | Selects the oldest open gate and replaces the URL with its canonical review URL |
| `ApprovalDetailPage` | `/runs/:threadId/reviews/:interruptId` | Opens the selected gate in the full Review Workspace |
| `ApprovalDetailPage` | `/approvals/:threadId/:interruptId` | Legacy bookmark; redirects to the canonical review URL |

## URL contract

`runReviewPath()` in `features/runs/reviewPath.ts` is the shared URL builder:

`/runs/:threadId/reviews/:interruptId?source=queue|history|home|overview&section=decision|overview|workspace|timeline&phase=<phase>`

- `interruptId` identifies the exact decision. `pending` is the fallback for
  legacy gates without an id.
- `section` defaults to `decision` and changes without leaving the queue or
  Review Workspace.
- `phase` preserves phase context across Decision, Workspace, Timeline,
  artifacts, and execution logs.
- Queue clicks and keyboard navigation update the URL, so the selected review
  is refreshable and shareable.

## Shared workspace and gate contract

`ApprovalsInboxPage` embeds `RunWorkspace` and passes the self-contained
`GateModule` through `renderGate`. The gate module owns resume I/O, CAS
handling, invalidations, and pessimistic action state. The inbox owns queue
selection, terminal outcome receipts, gray resolved rows, and auto-advance.

Decision actions use gate-specific language. Accepting phase output explicitly
states that it does not approve the overall test result or a release. Terminal
actions leave a decision receipt before the queue advances.

## Keyboard map

`j/k` or `↓/↑` navigate the queue; `Enter` focuses the workspace; `o` opens
Overview in the same workspace; `m` focuses prompt changes; `x` arms typed
abort confirmation; `?` toggles shortcut help. Approval and skip decisions
remain explicit buttons. Shortcuts yield while the operator is typing.

## Data

`useApprovalsInbox()` reuses `usePipelines({status:'interrupted', limit:100})`,
with a 15-second visibility-aware poll. The Sidebar badge consumes the same
cache entry. `RunWorkspace` and `GateModule` share the selected thread-state
cache entry for full run and gate detail.

Tests live in `__tests__/ApprovalsInboxPage.test.tsx`, with fixtures and MSW
handlers in the same directory.
