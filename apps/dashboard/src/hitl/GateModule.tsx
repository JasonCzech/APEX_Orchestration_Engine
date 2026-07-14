/**
 * GateModule — THE shared gate renderer (plan 2.a). Two layers:
 *
 * 1. GateModuleView — the controlled renderer (props {threadId, gate,
 *    machineState, onEdit, onSubmit, onViewCurrent?, compact?}). Run detail
 *    mounts it bound to the PAGE-LEVEL useGate machine (the header abort and
 *    the slim cross-phase banner share that one machine).
 *
 * 2. GateModule — the self-contained module honoring the approvals-inbox
 *    integration contract (features/approvals/gateModuleContract.ts):
 *    <GateModule threadId interrupt compact onOutcome handleRef />. It mounts
 *    its OWN useGate over the same thread-state cache entry, notifies terminal
 *    terminal outcomes exactly once per gate instance ('resumed' for terminal
 *    202 decisions, 'superseded' on 409-conflict/actioned-elsewhere), and exposes the imperative handle
 *    for the inbox keyboard layer (a/m/s/x + Enter).
 *
 * Render by machine state:
 *   open       -> kind-specific panel + action bar
 *   submitting -> same panel, controls locked + 'Resuming…' spinner chip
 *   awaiting_agent -> 'Agent working — gate will reopen' banner
 *   superseded -> SupersededBanner (+ [View current state] -> refetch + RESET)
 *   failed     -> error card + [Retry] (same action + preserved draft) above
 *                 the still-editable panel — the draft survives visibly
 *   no_gate    -> nothing
 */
import { useEffect, useImperativeHandle, useRef, type Ref } from 'react'

import { Link } from 'react-router'

import type { GateInterrupt } from '@/api/hooks/useThreadState'
import { roleAtLeast, RequireRole } from '@/auth/RequireRole'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { isPhaseName, PHASE_LABELS } from '@/features/runs/runDisplay'

import { GateActionBar } from './GateActionBar'
import {
  isPromptDirty,
  type GateAction,
  type GateDraftPatch,
  type GateInstance,
  type GateMachineState,
} from './gateMachine'
import { PhaseReviewPanel } from './PhaseReviewPanel'
import { PromptReviewPanel } from './PromptReviewPanel'
import { SupersededBanner } from './SupersededBanner'
import { useGate } from './useGate'
import './hitl.css'

function phaseLabel(phase: string): string {
  return isPhaseName(phase) ? PHASE_LABELS[phase] : phase
}

/* ── Controlled renderer (run detail) ────────────────────────────────────── */

export interface GateModuleViewProps {
  threadId: string
  gate: GateInstance
  machineState: GateMachineState
  onEdit: (patch: GateDraftPatch) => void
  onSubmit: (action: GateAction) => void
  /** SupersededBanner's [View current state] (refetch + RESET). */
  onViewCurrent?: (() => void) | undefined
  /** Inbox preview density: tighter spacing, collapsed secondary sections. */
  compact?: boolean
}

export function GateModuleView({
  threadId,
  gate,
  machineState,
  onEdit,
  onSubmit,
  onViewCurrent,
  compact = false,
}: GateModuleViewProps) {
  if (machineState.tag === 'no_gate') return null

  const kindLabel = gate.kind === 'prompt_review' ? 'Prompt review' : 'Phase review'
  const header = (
    <header className="gate-module-header">
      <span className="kind-chip">{gate.kind}</span>
      <h3 className="gate-module-title">
        {kindLabel} — {phaseLabel(gate.phase)}
      </h3>
      <span className="gate-module-id mono" title={`interrupt ${gate.interrupt_id}`}>
        {gate.interrupt_id}
      </span>
    </header>
  )

  const frameClass = `gate-module${compact ? ' compact' : ''}`

  if (machineState.tag === 'awaiting_agent') {
    return (
      <section className={frameClass} data-testid="gate-module" aria-label={`${kindLabel} gate`}>
        {header}
        <div className="gate-awaiting" data-testid="gate-awaiting">
          <span className="gate-spinner" aria-hidden="true" />
          <span>
            Agent working on your{' '}
            {machineState.action === 'discuss'
              ? 'message'
              : machineState.action === 'modify'
                ? 'prompt edits'
                : 'revision instructions'} — the gate
            will reopen.
          </span>
        </div>
      </section>
    )
  }

  if (machineState.tag === 'superseded') {
    return (
      <section className={frameClass} data-testid="gate-module" aria-label={`${kindLabel} gate`}>
        {header}
        <SupersededBanner by={machineState.by} onViewCurrent={onViewCurrent} />
      </section>
    )
  }

  // open / submitting / failed: panel + action bar.
  const draft = machineState.draft
  const dirty =
    machineState.tag === 'open' ? machineState.dirty : isPromptDirty(gate, machineState.draft)
  const submitting = machineState.tag === 'submitting'
  const disabled = submitting
  const payload = gate.payload

  return (
    <section className={frameClass} data-testid="gate-module" aria-label={`${kindLabel} gate`}>
      {header}

      {machineState.tag === 'failed' && (
        <div className="tonal-card danger gate-failed" data-testid="gate-failed">
          <span>Resume failed: {machineState.error.message} — your draft is intact below.</span>
          <RequireRole role="operator">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => onSubmit(machineState.action)}
            >
              Retry {machineState.action.replace('_', ' ')}
            </button>
          </RequireRole>
        </div>
      )}

      {typeof payload?.error === 'string' && (
        <div className="tonal-card warning" data-testid="gate-reinterrupt-error">
          Previous decision was rejected: {payload.error}
        </div>
      )}

      {payload === null && (
        <div className="tonal-card warning" data-testid="gate-payload-drift">
          This gate's payload did not match the dashboard's contract (schema drift) — review it
          from the backend, or approve/abort blind below.
        </div>
      )}

      {payload?.kind === 'prompt_review' && (
        <PromptReviewPanel
          gate={gate}
          payload={payload}
          prompt={draft.prompt}
          note={draft.note}
          dirty={dirty}
          disabled={disabled}
          compact={compact}
          onEdit={onEdit}
        />
      )}
      {payload?.kind === 'phase_review' && (
        <PhaseReviewPanel
          threadId={threadId}
          payload={payload}
          draft={draft}
          disabled={disabled}
          compact={compact}
          onEdit={onEdit}
        />
      )}

      <RequireRole role="operator">
        <GateActionBar
          key={`${gate.interrupt_id}:${JSON.stringify(gate.payload)}`}
          kind={gate.kind}
          actions={payload?.actions ?? ['approve', 'abort']}
          draft={draft}
          dirty={dirty}
          disabled={disabled}
          submitting={submitting}
          submittingAction={submitting ? machineState.action : undefined}
          onEdit={onEdit}
          onSubmit={onSubmit}
        />
      </RequireRole>
    </section>
  )
}

/* ── Self-contained module (approvals-inbox contract) ────────────────────── */

/** Terminal outcomes of one gate instance (one interrupt_id). */
export type GateOutcome =
  | { type: 'resumed'; action: GateAction; runId?: string }
  | { type: 'superseded' }

/** Imperative surface for the inbox keyboard layer (a/m/s/x + Enter). */
export interface GateModuleHandle {
  isActionable(): boolean
  invoke(action: GateAction): boolean
  focus(): void
}

export interface GateModuleProps {
  threadId: string
  /** The pending interrupt from useThreadState — one GateModule per gate instance. */
  interrupt: GateInterrupt
  compact?: boolean
  /** Fires exactly once per gate instance on a terminal state. */
  onOutcome?: (outcome: GateOutcome) => void
  handleRef?: Ref<GateModuleHandle | null>
}

/** Actions valid per gate kind (the payload's `actions` further narrows). */
const KIND_ACTIONS: Record<GateInstance['kind'], readonly GateAction[]> = {
  prompt_review: ['approve', 'modify', 'skip_phase', 'abort'],
  phase_review: ['approve', 'revise', 'discuss', 'abort'],
}

export function GateModule({
  threadId,
  interrupt,
  compact = false,
  onOutcome,
  handleRef,
}: GateModuleProps) {
  // Own machine over the SAME thread-state cache entry the inbox already
  // holds; `interrupt` identifies the instance (the machine discovers it from
  // the snapshot — the facade exposes one pending gate per thread).
  const hitl = useGate(threadId)
  const rootRef = useRef<HTMLDivElement>(null)
  const consumer = useOptionalConsumer()
  const canAct = consumer === undefined || Boolean(consumer && roleAtLeast(consumer.role, 'operator'))

  const stateRef = useRef(hitl.state)
  stateRef.current = hitl.state

  // Terminal-outcome notification: exactly once per mounted gate instance
  // (the inbox keys this module by interrupt_id) and only for THE interrupt
  // this instance represents — a freshly discovered re-gate never misfires.
  // 'resumed' reads useGate.lastAccepted (an explicit 202 record) instead of
  // sniffing the transient 'submitting' commit, which React can batch away.
  const expectedId = interrupt.interrupt_id ?? null
  const firedRef = useRef(false)
  const accepted = hitl.lastAccepted
  const supersededGateId = hitl.state.tag === 'superseded' ? hitl.state.gate.interrupt_id : null
  useEffect(() => {
    if (firedRef.current) return
    if (
      accepted &&
      accepted.action !== 'modify' &&
      accepted.action !== 'discuss' &&
      accepted.action !== 'revise' &&
      (!expectedId || accepted.interruptId === expectedId)
    ) {
      firedRef.current = true
      onOutcome?.({ type: 'resumed', action: accepted.action, runId: accepted.runId })
      return
    }
    if (supersededGateId && (!expectedId || supersededGateId === expectedId)) {
      firedRef.current = true
      onOutcome?.({ type: 'superseded' })
    }
  }, [accepted, supersededGateId, onOutcome, expectedId])

  const focusIn = (selector: string): void => {
    rootRef.current?.querySelector<HTMLElement>(selector)?.focus()
  }

  useImperativeHandle(
    handleRef,
    (): GateModuleHandle => ({
      isActionable: () => canAct && stateRef.current.tag === 'open',
      invoke: (action: GateAction): boolean => {
        if (!canAct) return false
        const state = stateRef.current
        if (state.tag !== 'open') return false
        const { gate } = state
        if (!KIND_ACTIONS[gate.kind].includes(action)) return false
        const advertised = gate.payload?.actions ?? ['approve', 'abort']
        if (!advertised.includes(action)) return false
        switch (action) {
          case 'approve':
          case 'skip_phase':
            hitl.submit(action)
            return true
          case 'abort':
            // Keyboard access must use the same typed confirmation as the
            // visible danger button. `x` arms and focuses it; it never sends
            // the destructive resume directly.
            rootRef.current
              ?.querySelector<HTMLButtonElement>('[data-gate-action="abort"]')
              ?.click()
            requestAnimationFrame(() => focusIn('.abort-confirm-input'))
            return true
          case 'modify':
            // Edit state: focus the system prompt editor ("modify-focus").
            focusIn('[data-testid="gate-editor-system"] .cm-content') // real CodeMirror
            focusIn('[data-testid="gate-editor-system"] textarea') // mocked editor in tests
            return true
          case 'discuss':
            focusIn('.gate-composer-input')
            return true
          case 'revise': {
            // Open the inline instructions textarea, then focus it.
            rootRef.current
              ?.querySelector<HTMLButtonElement>('[data-gate-action="revise"]')
              ?.click()
            requestAnimationFrame(() => focusIn('.gate-revise-input'))
            return true
          }
        }
      },
      focus: () => {
        rootRef.current?.focus()
      },
    }),
    [canAct, hitl],
  )

  if (!hitl.gate) return null

  return (
    <div ref={rootRef} tabIndex={-1} className="gate-module-root">
      <GateModuleView
        threadId={threadId}
        gate={hitl.gate}
        machineState={hitl.state}
        onEdit={hitl.edit}
        onSubmit={hitl.submit}
        onViewCurrent={hitl.viewCurrent}
        compact={compact}
      />
    </div>
  )
}

/* ── Slim cross-phase banner (run detail) ────────────────────────────────── */

/**
 * For run-detail routes whose selected phase is NOT the gate's phase: one
 * line + a link to the phase where the full GateModuleView is pinned.
 */
export function GateSlimBanner({ threadId, gate }: { threadId: string; gate: GateInstance }) {
  return (
    <div className="gate-slim-banner" data-testid="gate-slim-banner">
      <span>
        {gate.kind === 'prompt_review' ? 'Prompt review' : 'Phase review'} gate open on{' '}
        <strong>{phaseLabel(gate.phase)}</strong>
      </span>
      <Link
        className="btn btn-secondary btn-sm"
        to={
          isPhaseName(gate.phase) ? `/runs/${threadId}/phases/${gate.phase}` : `/runs/${threadId}`
        }
      >
        Review
      </Link>
    </div>
  )
}
