/**
 * Gate action bar (plan 2.a):
 *   prompt_review: [Approve] [Modify & approve (enabled when dirty)]
 *                  [Skip phase] [Abort — danger, inline type-to-confirm]
 *   phase_review:  [Approve] [Revise… (inline instructions textarea)]
 *                  [Discuss (sends the composer message)] [Abort confirm]
 * Buttons render only for actions the payload advertises (forward-compat:
 * unknown backend actions are simply not rendered here).
 * While submitting, everything is disabled and a 'Resuming…' chip shows.
 */
import { useId, useState } from 'react'

import type { GateAction, GateDraft, GateDraftPatch, GateKind } from './gateMachine'

/**
 * Danger action with an inline type-to-confirm ('ABORT') arm step. Exported
 * for the run-detail header, which offers the same machine-backed abort.
 */
export function AbortConfirm({
  onConfirm,
  disabled = false,
}: {
  onConfirm: () => void
  disabled?: boolean
}) {
  const [arming, setArming] = useState(false)
  const [text, setText] = useState('')
  const inputId = useId()

  if (!arming) {
    return (
      <button
        type="button"
        className="btn btn-danger btn-sm"
        disabled={disabled}
        onClick={() => setArming(true)}
      >
        Abort
      </button>
    )
  }

  const disarm = (): void => {
    setArming(false)
    setText('')
  }

  return (
    <span className="abort-confirm" data-testid="abort-confirm">
      <input
        id={inputId}
        className="field-input abort-confirm-input"
        placeholder="Type ABORT"
        aria-label="Type ABORT to confirm"
        value={text}
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
      />
      <button
        type="button"
        className="btn btn-danger btn-sm"
        disabled={disabled || text !== 'ABORT'}
        onClick={() => {
          disarm()
          onConfirm()
        }}
      >
        Confirm abort
      </button>
      <button type="button" className="btn btn-ghost btn-sm" onClick={disarm}>
        Cancel
      </button>
    </span>
  )
}

export function GateActionBar({
  kind,
  actions,
  draft,
  dirty,
  disabled,
  submitting,
  submittingAction,
  onEdit,
  onSubmit,
}: {
  kind: GateKind
  /** payload.actions (lenient strings — render the known subset). */
  actions: string[]
  draft: GateDraft
  /** Prompt draft differs from the payload original (enables Modify & approve). */
  dirty: boolean
  /** Lock every control (submitting / awaiting / superseded). */
  disabled: boolean
  submitting: boolean
  submittingAction?: GateAction | undefined
  onEdit: (patch: GateDraftPatch) => void
  onSubmit: (action: GateAction) => void
}) {
  const [revising, setRevising] = useState(false)
  const has = (action: string): boolean => actions.includes(action)
  const message = draft.message ?? ''
  const instructions = draft.instructions ?? ''

  return (
    <div className="gate-actions" data-testid="gate-actions">
      {kind === 'phase_review' && revising && (
        <label className="gate-revise" data-testid="gate-revise">
          <span className="gate-field-label">Revision instructions</span>
          <textarea
            className="field-input gate-revise-input"
            placeholder="What should the agent change before re-running this phase?"
            value={instructions}
            disabled={disabled}
            onChange={(event) => onEdit({ instructions: event.target.value })}
          />
        </label>
      )}
      <div className="gate-actions-row">
        {has('approve') && (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={disabled}
            onClick={() => onSubmit('approve')}
          >
            Approve
          </button>
        )}
        {kind === 'prompt_review' && has('modify') && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={disabled || !dirty}
            title={dirty ? 'Resume with your edited prompt' : 'Edit the prompt to enable'}
            onClick={() => onSubmit('modify')}
          >
            Modify &amp; approve
          </button>
        )}
        {kind === 'prompt_review' && has('skip_phase') && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={disabled}
            onClick={() => onSubmit('skip_phase')}
          >
            Skip phase
          </button>
        )}
        {kind === 'phase_review' &&
          has('revise') &&
          (revising ? (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={disabled || instructions.trim().length === 0}
              onClick={() => onSubmit('revise')}
            >
              Send revision
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              data-gate-action="revise"
              disabled={disabled}
              onClick={() => setRevising(true)}
            >
              Revise…
            </button>
          ))}
        {kind === 'phase_review' && has('discuss') && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={disabled || message.trim().length === 0}
            title={
              message.trim().length > 0
                ? 'Send the composer message to the agent'
                : 'Write a message in the composer to enable'
            }
            onClick={() => onSubmit('discuss')}
          >
            Discuss
          </button>
        )}
        <span className="gate-actions-spacer" />
        {submitting && (
          <span className="topbar-meta-chip accent gate-resuming-chip" data-testid="gate-resuming">
            <span className="gate-spinner" aria-hidden="true" />
            Resuming{submittingAction ? ` (${submittingAction.replace('_', ' ')})` : ''}…
          </span>
        )}
        {has('abort') && <AbortConfirm disabled={disabled} onConfirm={() => onSubmit('abort')} />}
      </div>
    </div>
  )
}
