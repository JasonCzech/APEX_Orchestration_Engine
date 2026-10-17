import './gate-dialog.css'
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
import { Dialog } from '@/components/Dialog'
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
        data-gate-action="abort"
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
  const confirmed = text.trim().toUpperCase() === 'ABORT'

  return (
    <span className="abort-confirm" data-testid="abort-confirm">
      <input
        id={inputId}
        className="field-input abort-confirm-input"
        autoFocus
        placeholder="Type ABORT"
        aria-label="Type ABORT to confirm"
        value={text}
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
      />
      <button
        type="button"
        className="btn btn-danger btn-sm"
        disabled={disabled || !confirmed}
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
  const [skipConfirm, setSkipConfirm] = useState(false)
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
            autoFocus
            placeholder="What should the agent change before re-running this phase?"
            value={instructions}
            disabled={disabled}
            onChange={(event) => onEdit({ instructions: event.target.value })}
          />
        </label>
      )}
      {skipConfirm && <Dialog className="gate-dialog glass-panel" overlayClassName="gate-dialog-overlay" ariaLabel="Skip this phase?" onClose={() => setSkipConfirm(false)}>
        <h2>Skip this phase?</h2>
        <p>This decision is irreversible. The phase will not execute, and later phases that require its output cannot run.</p>
        {dirty && <p>Your unsaved prompt edits will not be applied.</p>}
        <div className="ux-actions">
          <button type="button" className="btn btn-secondary" onClick={() => setSkipConfirm(false)}>Cancel</button>
          <button type="button" className="btn btn-danger" disabled={disabled} onClick={() => { setSkipConfirm(false); onSubmit('skip_phase') }}>Confirm skip</button>
        </div>
      </Dialog>}
      <div className="gate-actions-row">
        {kind.startsWith('engine_') && has('retry') && (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            data-gate-action="retry"
            disabled={disabled}
            onClick={() => onSubmit('retry')}
          >
            {submitting ? 'Retrying…' : 'Retry provider operation'}
          </button>
        )}
        {kind === 'prompt_review' && has('approve') && (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={disabled}
            onClick={() => onSubmit(dirty && has('modify') ? 'modify' : 'approve')}
          >
            {submitting
              ? dirty && has('modify')
                ? 'Saving…'
                : 'Executing…'
              : dirty && has('modify')
                ? 'Save edits and re-review'
                : 'Execute phase'}
          </button>
        )}
        {kind === 'phase_review' && has('approve') && (
          <span className="gate-primary-decision">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={disabled}
              onClick={() => onSubmit('approve')}
            >
              Accept phase output &amp; continue
            </button>
            <span className="gate-decision-scope">
              Accepts this phase output; it does not approve the test result or a release.
            </span>
          </span>
        )}
        {kind === 'prompt_review' && has('skip_phase') && (
          <span className="gate-primary-decision">
            <button type="button" className="btn btn-danger btn-sm" data-gate-action="skip_phase"
              disabled={disabled} onClick={() => setSkipConfirm(true)}>Skip phase</button>
            <span className="gate-decision-scope">Marks this phase skipped. Later phases that require its output cannot run.</span>
          </span>
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
              Re-run with changes
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              data-gate-action="revise"
              disabled={disabled}
              onClick={() => setRevising(true)}
            >
              Request changes
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
            Ask agent
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
