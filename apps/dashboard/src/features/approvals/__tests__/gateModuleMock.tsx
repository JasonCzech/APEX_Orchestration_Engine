/**
 * Typed GateModule stand-in for the inbox tests (the plan's "stub GateModule
 * via module mock"). Tests register it with:
 *
 *   vi.mock('@/hitl/GateModule', async () => ({
 *     GateModule: (await import('./gateModuleMock')).MockGateModule,
 *   }))
 *
 * Typed against the REAL GateModuleProps, so contract drift in src/hitl
 * breaks these tests at compile time. It records handle invocations, honors
 * isActionable/focus, and exposes buttons that fire each outcome so tests can
 * drive auto-advance without the machine or MSW resume round trips.
 */
import { useImperativeHandle } from 'react'

import type { GateModuleHandle, GateModuleProps } from '@/hitl/GateModule'
import type { GateAction } from '@/hitl/gateMachine'

/** Handle invocations across the whole test — reset in beforeEach. */
export const invokedActions: GateAction[] = []

export function resetGateModuleMock(): void {
  invokedActions.length = 0
}

export function MockGateModule({
  threadId,
  interrupt,
  compact,
  onOutcome,
  handleRef,
}: GateModuleProps) {
  useImperativeHandle(
    handleRef,
    (): GateModuleHandle => ({
      isActionable: () => true,
      invoke: (action: GateAction) => {
        invokedActions.push(action)
        // Submit actions resolve immediately (as if the 202 landed); edit
        // actions (modify/revise/discuss) only move focus in the real module.
        if (action === 'approve' || action === 'skip_phase' || action === 'abort') {
          onOutcome?.({ type: 'resumed', action })
        }
        return true
      },
      focus: () => {},
    }),
    [onOutcome],
  )

  return (
    <div
      data-testid="gate-module-mock"
      data-thread={threadId}
      data-interrupt={interrupt.interrupt_id ?? ''}
      data-compact={String(Boolean(compact))}
    >
      <button type="button" onClick={() => onOutcome?.({ type: 'resumed', action: 'approve' })}>
        mock-approve
      </button>
      <button type="button" onClick={() => onOutcome?.({ type: 'superseded' })}>
        mock-supersede
      </button>
      <textarea aria-label="mock-note" />
    </div>
  )
}
