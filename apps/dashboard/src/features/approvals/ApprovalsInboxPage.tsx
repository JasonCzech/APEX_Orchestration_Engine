/**
 * /approvals — keyboard-first cross-run gate queue (plan UX 2.b).
 * Also mounted on /approvals/:threadId/:interruptId (deep link pre-selects).
 *
 * Two panes: queue (360px) | gate preview (flex), stacking under 1320px.
 * The preview renders the SHARED self-contained GateModule (src/hitl, plan
 * 2.a) per the contract in ./gateModuleContract.ts — this page never touches
 * the resume endpoint itself (pessimistic semantics, invalidations and CAS
 * handling live in the gate machine).
 *
 * Keyboard map (disabled while typing in inputs/textareas/CodeMirror):
 *   j/k or ↓/↑  navigate queue        Enter  focus preview
 *   o           open run              ?      shortcuts overlay
 *   a/s/x       approve / skip phase / abort — delegated to the module's
 *               imperative handle, only while the preview gate is open
 *   m           modify-focus: the handle moves focus into the prompt editor
 *
 * Terminal outcomes gray the row inline — 'actioned' (resumed here) vs
 * 'actioned elsewhere' (superseded) — and auto-advance the selection to the
 * next open gate. Rows that vanish between polls (resumed from another
 * surface) stay grayed for one poll cycle via useApprovalsInbox.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import { useThreadState, type GateInterrupt } from '@/api/hooks/useThreadState'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'
import { GateModule } from '@/hitl/GateModule'

import { ApprovalsQueue, gateKindTone, type QueueRow, type RowRemovalReason } from './ApprovalsQueue'
import type { GateAction, GateModuleHandle, GateOutcome } from './gateModuleContract'
import { useApprovalsInbox, type ApprovalItem } from './useApprovalsInbox'
import './approvals.css'

interface Selection {
  threadId: string
  interruptId: string | null
}

/** A row actioned while on screen: grayed while the list still echoes these gate ids. */
interface ActionedEntry {
  reason: RowRemovalReason
  /** Gate instance ids consumed by the action — a NEW id on the row revives it. */
  interruptIds: ReadonlySet<string | null>
}

function toSelection(item: ApprovalItem): Selection {
  return { threadId: item.thread_id, interruptId: item.pending_gate.interrupt_id ?? null }
}

/** True when a keystroke originated in a typing context (shortcuts must yield). */
function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  // CodeMirror (gate prompt editors) renders a contenteditable inside .cm-editor.
  return Boolean(target.closest('.cm-editor'))
}

const SHORTCUTS: Array<[string, string]> = [
  ['j / ↓', 'Next gate'],
  ['k / ↑', 'Previous gate'],
  ['Enter', 'Focus the gate preview'],
  ['o', 'Open the run'],
  ['a', 'Approve (open gate)'],
  ['m', 'Modify — focus the prompt editor'],
  ['s', 'Skip phase (prompt gates)'],
  ['x', 'Arm abort confirmation'],
  ['?', 'Toggle this overlay'],
]

export function ApprovalsInboxPage() {
  const params = useParams<{ threadId?: string; interruptId?: string }>()
  const navigate = useNavigate()
  const inbox = useApprovalsInbox()

  const [selection, setSelection] = useState<Selection | null>(() =>
    params.threadId
      ? { threadId: params.threadId, interruptId: params.interruptId ?? null }
      : null,
  )
  const [actioned, setActioned] = useState<ReadonlyMap<string, ActionedEntry>>(new Map())
  const [overlayOpen, setOverlayOpen] = useState(false)

  const gateHandleRef = useRef<GateModuleHandle | null>(null)
  const previewRef = useRef<HTMLElement | null>(null)

  // ── Queue rows: live items (grayed when actioned from this pane) + rows that
  // vanished from the latest poll (grayed 'actioned elsewhere' for one cycle).
  const rows: QueueRow[] = [
    ...inbox.items.map((item): QueueRow => {
      const entry = actioned.get(item.thread_id)
      // A different interrupt_id is a NEW gate instance — the row goes live
      // again. Same-id re-reviews never become actioned in the first place.
      return entry && entry.interruptIds.has(item.pending_gate.interrupt_id ?? null)
        ? { ...item, removed: entry.reason }
        : item
    }),
    ...inbox.removedItems
      .filter((gone) => !inbox.items.some((item) => item.thread_id === gone.thread_id))
      .map(
        (gone): QueueRow => ({
          ...gone,
          removed: actioned.get(gone.thread_id)?.reason ?? 'elsewhere',
        }),
      ),
  ].sort((a, b) => {
    if (a.updated_at === b.updated_at) return a.thread_id.localeCompare(b.thread_id)
    if (a.updated_at === null) return 1
    if (b.updated_at === null) return -1
    return a.updated_at.localeCompare(b.updated_at)
  })
  const actionable = rows.filter((row) => !row.removed)

  // Latest render state for the document-level keyboard listener + callbacks.
  const stateRef = useRef({ actionable, selection, overlayOpen, items: inbox.items })
  stateRef.current = { actionable, selection, overlayOpen, items: inbox.items }

  // First load (or queue refill after empty): select the oldest open gate.
  useEffect(() => {
    if (selection === null && actionable.length > 0) {
      setSelection(toSelection(actionable[0] as ApprovalItem))
    }
  }, [selection, actionable])

  // ── Terminal gate outcome (preview's onOutcome): gray the row, advance to
  // the next open gate.
  const handleResolved = useCallback((reason: RowRemovalReason, gateId: string | null) => {
    const { selection: current, actionable: list, items } = stateRef.current
    if (!current) return
    const listRow = items.find((item) => item.thread_id === current.threadId)
    setActioned((prev) => {
      const next = new Map(prev)
      next.set(current.threadId, {
        reason,
        interruptIds: new Set([
          gateId,
          listRow?.pending_gate.interrupt_id ?? null,
          current.interruptId,
        ]),
      })
      return next
    })
    const remaining = list.filter((row) => row.thread_id !== current.threadId)
    if (remaining.length === 0) {
      setSelection(null)
      return
    }
    const index = list.findIndex((row) => row.thread_id === current.threadId)
    const next =
      list.slice(index + 1).find((row) => row.thread_id !== current.threadId) ??
      remaining[remaining.length - 1]
    setSelection(next ? toSelection(next) : null)
  }, [])

  // ── Keyboard layer (one document listener; latest state via stateRef).
  useEffect(() => {
    const moveSelection = (delta: 1 | -1) => {
      const { actionable: list, selection: current } = stateRef.current
      if (list.length === 0) return
      const index = current ? list.findIndex((row) => row.thread_id === current.threadId) : -1
      const next =
        index === -1
          ? list[delta === 1 ? 0 : list.length - 1]
          : list[Math.min(Math.max(index + delta, 0), list.length - 1)]
      if (next) setSelection(toSelection(next))
    }
    const invokeGate = (action: GateAction) => {
      const handle = gateHandleRef.current
      if (handle?.isActionable()) handle.invoke(action)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      if (isEditableTarget(event.target)) return
      if (event.key === '?') {
        event.preventDefault()
        setOverlayOpen((open) => !open)
        return
      }
      if (event.key === 'Escape') {
        setOverlayOpen(false)
        return
      }
      if (stateRef.current.overlayOpen) return
      switch (event.key) {
        case 'j':
        case 'ArrowDown':
          event.preventDefault()
          moveSelection(1)
          break
        case 'k':
        case 'ArrowUp':
          event.preventDefault()
          moveSelection(-1)
          break
        case 'Enter':
          if (gateHandleRef.current) gateHandleRef.current.focus()
          else previewRef.current?.focus()
          break
        case 'o': {
          const { selection: current } = stateRef.current
          if (current) void navigate(`/runs/${current.threadId}`)
          break
        }
        case 'a':
          invokeGate('approve')
          break
        case 'm':
          invokeGate('modify')
          break
        case 's':
          invokeGate('skip_phase')
          break
        case 'x':
          invokeGate('abort')
          break
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [navigate])

  const allClear = !inbox.isLoading && !inbox.error && rows.length === 0

  return (
    <div className="approvals-page animate-enter">
      <header className="approvals-header">
        <h2 className="approvals-title">Approvals</h2>
        {inbox.count > 0 && (
          <span className="topbar-meta-chip accent" data-testid="approvals-count-chip">
            {inbox.count} pending
          </span>
        )}
        <span className="spacer" />
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          aria-label="Keyboard shortcuts"
          title="Keyboard shortcuts (?)"
          onClick={() => setOverlayOpen((open) => !open)}
        >
          ?
        </button>
      </header>

      {inbox.error ? (
        <ProblemCard
          title="Approvals failed to load"
          message={inbox.error.message}
          onRetry={inbox.refetch}
        />
      ) : allClear ? (
        <div className="dash-empty approvals-empty" data-testid="approvals-empty">
          <h3>All clear. No gates awaiting review.</h3>
          <p className="dash-empty-hint">New gates appear here the moment a run interrupts.</p>
          <Link className="btn btn-ghost btn-sm" to="/runs">
            Go to runs
          </Link>
        </div>
      ) : inbox.isLoading ? (
        <div
          className="approvals-skeleton"
          role="status"
          aria-busy="true"
          aria-label="Loading approvals"
        >
          <div className="glass-panel approvals-skeleton-block" />
          <div className="glass-panel approvals-skeleton-block" />
        </div>
      ) : (
        <div className="approvals-layout">
          <section className="approvals-queue-pane glass-panel" aria-label="Pending gates">
            <ApprovalsQueue
              rows={rows}
              selectedThreadId={selection?.threadId ?? null}
              onSelect={(row) => setSelection(toSelection(row))}
            />
          </section>
          <section
            className="approvals-preview-pane"
            aria-label="Gate preview"
            ref={previewRef}
            tabIndex={-1}
          >
            {selection ? (
              <GatePreview
                key={selection.threadId}
                selection={selection}
                handleRef={gateHandleRef}
                onResolved={handleResolved}
              />
            ) : (
              <div className="dash-empty small">Select a gate from the queue (j / k).</div>
            )}
          </section>
        </div>
      )}

      {overlayOpen && (
        <Dialog
          overlayClassName="approvals-overlay-backdrop"
          className="glass-panel approvals-shortcuts"
          ariaLabel="Keyboard shortcuts"
          onClose={() => setOverlayOpen(false)}
        >
          <h3>Keyboard shortcuts</h3>
          <dl>
            {SHORTCUTS.map(([keys, what]) => (
              <div className="approvals-shortcut-row" key={keys}>
                <dt>
                  <kbd>{keys}</kbd>
                </dt>
                <dd>{what}</dd>
              </div>
            ))}
          </dl>
        </Dialog>
      )}
    </div>
  )
}

/**
 * Right pane: snapshot the selected thread and mount the shared GateModule on
 * its pending interrupt. Keyed by threadId from the parent so nothing leaks
 * across runs; changed interrupt ids remount, while refreshed same-id
 * re-reviews reopen inside the shared machine.
 *
 * Outcome mapping for the queue (see gateModuleContract.ts):
 *   resumed approve/skip_phase/abort        -> 'local' (gray 'actioned', advance)
 *   resumed modify/discuss/revise           -> not terminal (gate reopens) — no-op
 *   superseded                              -> 'elsewhere' (gray, advance)
 */
function GatePreview({
  selection,
  handleRef,
  onResolved,
}: {
  selection: Selection
  handleRef: React.MutableRefObject<GateModuleHandle | null>
  onResolved: (reason: RowRemovalReason, gateId: string | null) => void
}) {
  const query = useThreadState(selection.threadId)

  if (query.isPending) {
    return (
      <div
        className="glass-panel approvals-skeleton-block"
        role="status"
        aria-busy="true"
        aria-label="Loading gate"
      />
    )
  }
  if (query.isError) {
    return (
      <ProblemCard
        title="Gate failed to load"
        message={query.error instanceof Error ? query.error.message : 'Unknown error'}
        onRetry={() => void query.refetch()}
      />
    )
  }

  const { detail, interrupts } = query.data
  // Prefer the deep-linked/queued instance; otherwise the thread's first
  // pending interrupt (a re-interrupt replaces the id we navigated with).
  const interrupt: GateInterrupt | undefined =
    interrupts.find((candidate) => candidate.interrupt_id === selection.interruptId) ??
    interrupts[0]
  const gateId = interrupt?.interrupt_id ?? null

  const handleOutcome = (outcome: GateOutcome) => {
    if (outcome.type === 'superseded') {
      onResolved('elsewhere', gateId)
    } else if (
      outcome.action !== 'modify' &&
      outcome.action !== 'discuss' &&
      outcome.action !== 'revise'
    ) {
      onResolved('local', gateId)
    }
    // modify/discuss/revise: the gate reopens — keep the selection.
  }

  return (
    <div className="approvals-preview">
      <header className="approvals-preview-header">
        <Link to={`/runs/${detail.thread_id}`} className="approvals-preview-title">
          {detail.title ?? detail.thread_id}
        </Link>
        {interrupt && (
          <span className={`topbar-meta-chip ${gateKindTone(interrupt.kind)}`}>
            {interrupt.kind ?? 'review'} · {interrupt.phase ?? '?'}
          </span>
        )}
        <span className="spacer" />
        <Link className="btn btn-ghost btn-sm" to={`/runs/${detail.thread_id}`}>
          Open run
        </Link>
      </header>
      {interrupt ? (
        <GateModule
          key={interrupt.interrupt_id ?? 'gate'}
          threadId={detail.thread_id}
          interrupt={interrupt}
          compact
          onOutcome={handleOutcome}
          handleRef={handleRef}
        />
      ) : (
        <div className="dash-empty small" data-testid="approvals-gate-gone">
          No open gate on this run — it was actioned elsewhere. The queue refreshes on the next
          poll.
        </div>
      )}
    </div>
  )
}
