import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
/**
 * Keyboard-first cross-run gate queue and full Review Workspace. /approvals
 * selects the oldest gate; /runs/:threadId/reviews/:interruptId is canonical.
 * Legacy /approvals/:threadId/:interruptId bookmarks redirect there.
 *
 * Two panes: queue (360px) | review workspace (flex), stacking under 1320px.
 * The workspace renders the SHARED self-contained GateModule (src/hitl, plan
 * 2.a) per the contract in ./gateModuleContract.ts — this page never touches
 * the resume endpoint itself (pessimistic semantics, invalidations and CAS
 * handling live in the gate machine).
 *
 * Keyboard map (disabled while typing in inputs/textareas/CodeMirror):
 *   j/k or ↓/↑  navigate queue        Enter  focus workspace
 *   o           open overview          ?      shortcuts overlay
 *   m           modify-focus: the handle moves focus into the prompt editor
 *   x           arm the module's typed abort confirmation
 * Approval and skip decisions remain on the explicit gate controls; ordinary
 * document-level letter keys must never submit them.
 *
 * Terminal outcomes gray the row inline — 'actioned' (resumed here) vs
 * 'actioned elsewhere' (superseded) — and auto-advance the selection to the
 * next open gate. Rows that vanish between polls (resumed from another
 * surface) stay grayed for one poll cycle via useApprovalsInbox.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router'
import { useQueryClient } from '@tanstack/react-query'
import { queryKeys } from '@/api/queryKeys'

import { useThreadState, type GateInterrupt, type ThreadStateSnapshot } from '@/api/hooks/useThreadState'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { Dialog } from '@/components/Dialog'
import { useCompactLayout } from '@/components/hooks/useCompactLayout'
import { ProblemCard } from '@/components/ProblemCard'
import { RunWorkspace, type RunWorkspaceSection } from '@/features/runs/RunDetailPage'
import { runReviewPath, reviewInterruptId } from '@/features/runs/reviewPath'
import { formatTimestamp, isPhaseName } from '@/features/runs/runDisplay'
import { GateModule } from '@/hitl/GateModule'

import { ApprovalsQueue, type QueueRow, type RowRemovalReason } from './ApprovalsQueue'
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

interface ReopeningBaseline {
  threadSnapshotAt: number
  inboxSnapshotAt: number
  serverUpdatedAt: string | null | undefined
  gateId: string | null | undefined
  originalGateId: string | null | undefined
}

function isReopenedGate(updatedAt: string | null | undefined, gateId: string | null | undefined, baseline: ReopeningBaseline, originalGateId: string | null | undefined): boolean {
  const current = Date.parse(updatedAt ?? '')
  const previous = Date.parse(baseline.serverUpdatedAt ?? '')
  const newIdentity = Boolean(gateId && gateId !== baseline.gateId && gateId !== originalGateId)
  if (Number.isFinite(current) && Number.isFinite(previous)) return current > previous || (current === previous && newIdentity)
  return newIdentity
}

interface DecisionReceipt {
  title: string
  action: string
  at: Date
}

function outcomeLabel(action: GateAction | 'superseded', kind?: GateInterrupt['kind']): string {
  switch (action) {
    case 'approve': return kind === 'prompt_review'
      ? 'Approved prompts and started the phase'
      : 'Accepted phase output and continued the run'
    case 'skip_phase': return 'Skipped the phase and continued the run'
    case 'abort': return 'Aborted the run'
    case 'superseded': return 'Decision was completed elsewhere'
    default: return 'Decision recorded'
  }
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
  ['Enter', 'Focus the review workspace'],
  ['o', 'Open the run overview'],
  ['m', 'Modify — focus the prompt editor'],
  ['x', 'Arm abort confirmation'],
  ['?', 'Toggle this overlay'],
]

export function ApprovalsInboxPage() {
  useTimestampPreference()
  const compact = useCompactLayout()
  const [queueExpanded, setQueueExpanded] = useState(false)
  const queueVisible = !compact || queueExpanded
  const queryClient = useQueryClient()
  const params = useParams<{ threadId?: string; interruptId?: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const inbox = useApprovalsInbox()

  const selection = useMemo<Selection | null>(
    () => params.threadId
      ? { threadId: params.threadId, interruptId: reviewInterruptId(params.interruptId) }
      : null,
    [params.interruptId, params.threadId],
  )
  const requestedSelectionRef = useRef<Selection | null>(selection)
  requestedSelectionRef.current = selection
  const setSelection = useCallback(
    (value: Selection | null, replace = false) => {
      requestedSelectionRef.current = value
      void navigate(
        value
          ? runReviewPath(value.threadId, value.interruptId, { source: 'queue' })
          : '/approvals',
        { replace },
      )
    },
    [navigate],
  )
  const [actioned, setActioned] = useState<ReadonlyMap<string, ActionedEntry>>(new Map())
  const [decisionReceipt, setDecisionReceipt] = useState<DecisionReceipt | null>(null)
  const [reopening, setReopening] = useState<ReadonlySet<string>>(new Set())
  const reopeningRef = useRef(new Set<string>())
  const reopeningRowsRef = useRef(new Map<string, QueueRow>())
  const reopeningFinishedRef = useRef(new Map<string, number>())
  const reopeningBaselinesRef = useRef(new Map<string, ReopeningBaseline>())
  const inboxUpdatedAtRef = useRef(inbox.updatedAt)
  inboxUpdatedAtRef.current = inbox.updatedAt
  const { refetch: refetchInbox } = inbox
  const markReopening = useCallback((threadId: string, pending: boolean) => {
    if (pending) {
      const row = stateRef.current.items.find(item => item.thread_id === threadId)
      reopeningRef.current.add(threadId)
      reopeningFinishedRef.current.delete(threadId)
      if (row) reopeningRowsRef.current.set(threadId, row)
      const cached = queryClient.getQueryState<ThreadStateSnapshot>(queryKeys.threads.state(threadId))
      const gateId = cached?.data?.detail.pending_gate?.interrupt_id ?? cached?.data?.interrupts[0]?.interrupt_id ?? row?.pending_gate.interrupt_id
      reopeningBaselinesRef.current.set(threadId, {
        threadSnapshotAt: cached?.dataUpdatedAt ?? 0,
        inboxSnapshotAt: inboxUpdatedAtRef.current,
        serverUpdatedAt: cached?.data?.detail.updated_at ?? row?.updated_at,
        gateId,
        originalGateId: row?.pending_gate.interrupt_id ?? gateId,
      })
      setReopening(new Set(reopeningRef.current))
    } else if (reopeningRef.current.has(threadId)) {
      // The thread snapshot commonly reopens before the independently polled
      // inbox catches up. Keep the local working row until that list is healed.
      const snapshot = queryClient.getQueryData<ThreadStateSnapshot>(queryKeys.threads.state(threadId))
      const currentGateId = snapshot?.interrupts[0]?.interrupt_id ?? snapshot?.detail.pending_gate?.interrupt_id
      if (currentGateId && stateRef.current.selection?.threadId === threadId && stateRef.current.selection.interruptId !== currentGateId) {
        setSelection({ threadId, interruptId: currentGateId }, true)
      }
      reopeningFinishedRef.current.set(threadId, inboxUpdatedAtRef.current)
      void refetchInbox()
    }
  }, [refetchInbox, queryClient, setSelection])
  useEffect(() => {
    let changed = false
    for (const threadId of reopeningRef.current) {
      const original = reopeningRowsRef.current.get(threadId)
      const fresh = inbox.items.find(item => item.thread_id === threadId)
      const baseline = reopeningBaselinesRef.current.get(threadId)
      const refreshedGate = fresh && baseline && inbox.updatedAt > baseline.inboxSnapshotAt && isReopenedGate(fresh.updated_at, fresh.pending_gate.interrupt_id, baseline, baseline.originalGateId)
      const beforeRefresh = reopeningFinishedRef.current.get(threadId)
      const cached = queryClient.getQueryState<ThreadStateSnapshot>(queryKeys.threads.state(threadId))
      const activeGateId = cached?.data?.interrupts[0]?.interrupt_id ?? cached?.data?.detail.pending_gate?.interrupt_id
      const terminal = baseline && cached && cached.dataUpdatedAt > baseline.threadSnapshotAt && !activeGateId &&
        ['idle', 'error'].includes(cached.data?.detail.thread_status ?? '')
      const settledAndReconciled = beforeRefresh !== undefined && inbox.updatedAt > beforeRefresh &&
        (terminal || (activeGateId && fresh?.pending_gate.interrupt_id === activeGateId))
      // A fresh gate also reconciles work completed after its preview unmounted.
      // Queue absence cannot override an active review in the run snapshot.
      if (!refreshedGate && !settledAndReconciled) continue
      if (!fresh && original) setActioned(previous => new Map(previous).set(threadId, {
        reason: 'local', interruptIds: new Set([original.pending_gate.interrupt_id ?? null]),
      }))
      reopeningFinishedRef.current.delete(threadId)
      reopeningBaselinesRef.current.delete(threadId)
      reopeningRef.current.delete(threadId)
      reopeningRowsRef.current.delete(threadId)
      changed = true
    }
    if (changed) setReopening(new Set(reopeningRef.current))
  }, [inbox.updatedAt, inbox.items, queryClient])
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
          ...(reopening.has(gone.thread_id) ? { reopening: true } : { removed: actioned.get(gone.thread_id)?.reason ?? 'elsewhere' }),
        }),
      ),
  ].sort((a, b) => {
    if (a.updated_at === b.updated_at) return a.thread_id.localeCompare(b.thread_id)
    if (a.updated_at === null) return 1
    if (b.updated_at === null) return -1
    return a.updated_at.localeCompare(b.updated_at)
  })
  for (const [threadId, row] of reopeningRowsRef.current) {
    if (!rows.some(candidate => candidate.thread_id === threadId)) rows.push({ ...row, reopening: true })
  }
  const actionable = rows.filter((row) => !row.removed)

  // Latest render state for the document-level keyboard listener + callbacks.
  const stateRef = useRef({ actionable, selection, overlayOpen, items: inbox.items })
  stateRef.current = { actionable, selection, overlayOpen, items: inbox.items }

  // First load (or queue refill after empty): select the oldest open gate.
  useEffect(() => {
    if (selection === null && actionable.length > 0) {
      setSelection(toSelection(actionable[0] as ApprovalItem), true)
    }
  }, [selection, actionable, setSelection])

  // Preserve old bookmarks while making the gate-specific review URL canonical.
  useEffect(() => {
    if (!selection || !location.pathname.startsWith('/approvals/')) return
    setSelection(selection, true)
  }, [location.pathname, selection, setSelection])

  // Retain the last queue ordering that actually contained this selected
  // interrupt. Reopening can settle one render after its replacement row
  // arrives; a single previous-render snapshot would lose that evidence.
  const observedSelection = useRef<{ selection: Selection; rows: QueueRow[] } | null>(null)
  const selectedStillOpen = selection !== null && actionable.some(row =>
    row.thread_id === selection.threadId &&
    (selection.interruptId === null || row.pending_gate.interrupt_id === selection.interruptId),
  )
  if (selection && selectedStillOpen) observedSelection.current = { selection, rows }

  // A gate can disappear between polls without GateModule getting a terminal
  // callback. Advance the parent selection as soon as the selected instance is
  // no longer actionable; prefer a re-gate on the same thread.
  useEffect(() => {
    if (inbox.isLoading || selection === null || reopening.has(selection.threadId)) return
    if (selectedStillOpen) return
    const precedingRows = observedSelection.current?.selection.threadId === selection.threadId &&
      observedSelection.current?.selection.interruptId === selection.interruptId
      ? observedSelection.current.rows : []
    const priorIndex = precedingRows.findIndex(row =>
      row.thread_id === selection.threadId &&
      (selection.interruptId === null || row.pending_gate.interrupt_id === selection.interruptId),
    )
    // A canonical link can arrive before the queue observes a newly opened
    // review. Absence from that separate cache is not evidence that the
    // requested interrupt completed. Let its own run snapshot resolve it.
    if (priorIndex === -1) return
    const sameThread = actionable.find((row) => row.thread_id === selection.threadId)
    const next = sameThread ?? precedingRows.slice(priorIndex + 1).map(row => actionable.find(open => open.thread_id === row.thread_id)).find(Boolean) ?? actionable.at(-1)
    setSelection(next ? toSelection(next as ApprovalItem) : null, true)
  }, [actionable, inbox.isLoading, selection, selectedStillOpen, setSelection, reopening])

  // ── Terminal gate outcome (preview's onOutcome): gray the row, advance to
  // the next open gate.
  const handleResolved = useCallback((reason: RowRemovalReason, gateId: string | null, action?: GateAction) => {
    const { selection: current, actionable: list, items } = stateRef.current
    if (!current) return
    reopeningRef.current.delete(current.threadId)
    reopeningRowsRef.current.delete(current.threadId)
    reopeningFinishedRef.current.delete(current.threadId)
    reopeningBaselinesRef.current.delete(current.threadId)
    setReopening(new Set(reopeningRef.current))
    const listRow = items.find((item) => item.thread_id === current.threadId)
    setDecisionReceipt({
      title: listRow?.title ?? current.threadId,
      action: outcomeLabel(
        reason === 'elsewhere' ? 'superseded' : action ?? 'approve',
        listRow?.pending_gate.kind,
      ),
      at: new Date(),
    })
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
      setSelection(null, true)
      return
    }
    const index = list.findIndex((row) => row.thread_id === current.threadId)
    const next =
      list.slice(index + 1).find((row) => row.thread_id !== current.threadId) ??
      remaining[remaining.length - 1]
    setSelection(next ? toSelection(next) : null, true)
  }, [setSelection])

  // ── Keyboard layer (one document listener; latest state via stateRef).
  useEffect(() => {
    const moveSelection = (delta: 1 | -1) => {
      const { actionable: list } = stateRef.current
      const current = requestedSelectionRef.current
      if (list.length === 0) return
      const index = current ? list.findIndex((row) => row.thread_id === current.threadId) : -1
      const next =
        index === -1
          ? list[delta === 1 ? 0 : list.length - 1]
          : list[Math.min(Math.max(index + delta, 0), list.length - 1)]
      if (next) {
        setQueueExpanded(true)
        setSelection(toSelection(next))
      }
    }
    const invokeGate = (action: GateAction) => {
      const handle = gateHandleRef.current
      if (handle?.isActionable()) handle.invoke(action)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      if (event.target instanceof Element && event.target.closest('[role="dialog"]')) return
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
          if (current) {
            void navigate(
              runReviewPath(current.threadId, current.interruptId, {
                source: 'queue',
                section: 'overview',
              }),
            )
          }
          break
        }
        case 'm':
          invokeGate('modify')
          break
        case 'x':
          invokeGate('abort')
          break
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [navigate, setSelection])

  const allClear = !inbox.isLoading && !inbox.error && rows.length === 0 && selection === null
  const selectionInQueue = selection === null || rows.some(row => row.thread_id === selection.threadId &&
    (selection.interruptId === null || row.pending_gate.interrupt_id === selection.interruptId))
  const reviewSearch = new URLSearchParams(location.search)
  const requestedSource = reviewSearch.get('source')
  const reviewSource = requestedSource === 'history' || requestedSource === 'home' || requestedSource === 'overview'
    ? requestedSource
    : 'queue'
  const requestedSection = reviewSearch.get('section')
  const reviewSection: RunWorkspaceSection =
    requestedSection === 'overview' || requestedSection === 'workspace' || requestedSection === 'timeline'
      ? requestedSection
      : 'decision'
  const reviewPhase = isPhaseName(reviewSearch.get('phase')) ? reviewSearch.get('phase') : null
  const setReviewSection = (section: RunWorkspaceSection, phase?: string) => {
    if (!selection) return
    void navigate(
      runReviewPath(selection.threadId, selection.interruptId, {
        source: reviewSource,
        section,
        phase: phase ?? reviewPhase,
      }),
      { replace: true },
    )
  }

  return (
    <div className="approvals-page animate-enter">
      {[...reopeningBaselinesRef.current.entries()].map(([threadId, baseline]) => (
        <ReopeningGateObserver key={threadId} threadId={threadId} baseline={baseline} onSettled={markReopening} />
      ))}
      <header className="approvals-toolbar glass-panel">
        <nav className="ux-actions approvals-view-switch" aria-label="Test operations views">
          <Link className="btn btn-secondary" to="/runs">All runs</Link>
          <Link className="btn btn-secondary" aria-current="page" to="/approvals">Needs review</Link>
        </nav>
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

      {decisionReceipt && (
        <div className="approvals-decision-receipt tonal-card success" role="status">
          <div>
            <strong>Decision receipt</strong>
            <span>{decisionReceipt.action} for {decisionReceipt.title} · {formatTimestamp(decisionReceipt.at.toISOString())}</span>
          </div>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setDecisionReceipt(null)}>Dismiss</button>
        </div>
      )}

      {inbox.refreshError && (
        <CachedDataWarning error={inbox.refreshError} onRetry={inbox.refetch} />
      )}

      {inbox.error && selection === null ? (
        <ProblemCard
          title="Reviews failed to load"
          message={inbox.error.message}
          onRetry={inbox.refetch}
        />
      ) : allClear ? (
        <StateCard kind="empty" title="All clear. No reviews pending." data-testid="approvals-empty" action={<Link className="btn btn-secondary btn-sm" to="/runs">Go to runs</Link>}>
          New reviews appear here when a run needs your decision.
        </StateCard>
      ) : inbox.isLoading && selection === null ? (
        <Skeleton label="Loading reviews" rows={2} height={120} />
      ) : (
        <div className="approvals-layout">
          <section className="approvals-queue-pane glass-panel" aria-label="Pending reviews">
            {compact && <button type="button" className="approvals-queue-summary btn btn-secondary" aria-expanded={queueVisible} aria-controls="pending-review-queue" onClick={() => setQueueExpanded(value => !value)}>
              {queueVisible ? 'Hide' : 'Show'} pending reviews ({inbox.isLoading ? 'loading' : inbox.error ? 'unavailable' : actionable.length})
              {selection && <span>{rows.find(row => row.thread_id === selection.threadId)?.title ?? 'Selected review'}</span>}
            </button>}
            <div id="pending-review-queue" hidden={!queueVisible}>
            {inbox.isLoading ? <Skeleton label="Loading reviews" rows={2} /> : inbox.error ? <ProblemCard title="Reviews failed to load" message={inbox.error.message} onRetry={inbox.refetch} /> : <>
            {!selectionInQueue && <StateCard kind="stale" compact title="Selected review is outside the current queue snapshot" action={<button className="btn btn-secondary" onClick={() => void inbox.refetch()}>Refresh queue</button>}>
              The selected run is shown from its own latest snapshot.
            </StateCard>}
            <ApprovalsQueue
              rows={rows}
              focusSelection={queueVisible}
              selectedThreadId={selection?.threadId ?? null}
              onSelect={(row) => setSelection(toSelection(row))}
            />
            </>}
            </div>
          </section>
          <section
            className="approvals-preview-pane"
            aria-label="Review workspace"
            ref={previewRef}
            tabIndex={-1}
          >
            {selection ? (
              <GatePreview
                key={selection.threadId}
                selection={selection}
                handleRef={gateHandleRef}
                onResolved={handleResolved}
                onReopening={markReopening}
                waitAge={rows.find((row) => row.thread_id === selection.threadId)?.age}
                queuePosition={selectionInQueue ? { position: rows.findIndex(row => row.thread_id === selection.threadId) + 1, total: rows.length } : undefined}
                section={reviewSection}
                phase={reviewPhase}
                onSectionChange={setReviewSection}
              />
            ) : (
              <StateCard kind="empty" compact title="Select a review" action={null}>Choose a review from the queue, or use j / k.</StateCard>
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

/** Keep an in-flight local decision observable when its workspace is unmounted. */
function ReopeningGateObserver({ threadId, baseline, onSettled }: {
  threadId: string
  baseline: ReopeningBaseline | undefined
  onSettled: (threadId: string, pending: boolean) => void
}) {
  const query = useThreadState(threadId)
  const detail = query.data?.detail
  const fresh = Boolean(baseline && query.dataUpdatedAt > baseline.threadSnapshotAt)
  const terminal = fresh && (detail?.thread_status === 'idle' || detail?.thread_status === 'error')
  const newGate = Boolean(fresh && detail?.pending_gate && baseline && isReopenedGate(detail.updated_at, detail.pending_gate.interrupt_id, baseline, baseline.originalGateId))
  useEffect(() => {
    if (terminal || newGate) onSettled(threadId, false)
  }, [terminal, newGate, onSettled, threadId])
  return null
}

/**
 * Right pane: mount the shared RunWorkspace and GateModule for the selected
 * pending interrupt. Keyed by threadId from the parent so nothing leaks
 * across runs; refreshed gates reopen inside the shared machine, including
 * changes to the interrupt id.
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
  onReopening,
  section,
  phase,
  onSectionChange,
  waitAge,
  queuePosition,
}: {
  selection: Selection
  handleRef: React.MutableRefObject<GateModuleHandle | null>
  onResolved: (reason: RowRemovalReason, gateId: string | null, action?: GateAction) => void
  onReopening: (threadId: string, pending: boolean) => void
  section: RunWorkspaceSection
  phase: string | null
  onSectionChange: (section: RunWorkspaceSection, phase?: string) => void
  waitAge?: string
  queuePosition?: { position: number; total: number }
}) {
  const query = useThreadState(selection.threadId)
  const reportReopening = useCallback((pending: boolean) => onReopening(selection.threadId, pending), [onReopening, selection.threadId])

  if (query.isPending) {
    return (
      <Skeleton label="Loading review" rows={2} height={120} />
    )
  }
  if (query.isError && !query.data) {
    return (
      <ProblemCard
        title="Review failed to load"
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
      onResolved('local', gateId, outcome.action)
    }
    // modify/discuss/revise: the gate reopens — keep the selection.
  }

  return (
    <div className="approvals-preview">
      {query.isError && (
        <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />
      )}
      <RunWorkspace
          key={detail.thread_id}
          threadId={detail.thread_id}
          embedded
          showEmbeddedControls={false}
          waitAge={waitAge}
          reviewTriage
          queuePosition={queuePosition}
          section={section}
          phase={phase ?? (isPhaseName(interrupt?.phase) ? interrupt.phase : null)}
          onSectionChange={(nextSection, nextPhase) => onSectionChange(nextSection, nextPhase)}
          onPhaseChange={(nextPhase) => onSectionChange('workspace', nextPhase)}
          renderGate={(workspaceInterrupt, returnTo) => (
            <GateModule
              key={detail.thread_id}
              threadId={detail.thread_id}
              interrupt={workspaceInterrupt}
              returnTo={returnTo}
              onOutcome={handleOutcome}
              onReopening={reportReopening}
              handleRef={handleRef}
            />
          )}
        />
    </div>
  )
}
