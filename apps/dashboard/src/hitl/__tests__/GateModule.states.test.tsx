/**
 * GateModuleView state renderings driven directly by constructed machine
 * states: submitting lock + 'Resuming…' chip, awaiting-agent banner,
 * superseded variants + [View current state], failed card with [Retry]
 * resubmitting the same action over an intact draft.
 */
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { GateModuleView } from '@/hitl/GateModule'
import { initialDraftFor, type GateMachineState } from '@/hitl/gateMachine'

import { gateInstanceOf, phaseInterrupt, promptInterrupt } from './gateFixtures'

vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({
      value,
      readOnly,
      editable,
      'aria-label': ariaLabel,
    }: {
      value: string
      readOnly?: boolean
      editable?: boolean
      'aria-label'?: string
    }) =>
      createElement('textarea', {
        'data-testid': 'codemirror',
        'aria-label': ariaLabel,
        value,
        readOnly: readOnly === true || editable === false,
        onChange: () => undefined,
      }),
  }
})

const promptGate = gateInstanceOf(promptInterrupt('int-1'))
const phaseGate = gateInstanceOf(phaseInterrupt('int-2'))

function renderState(state: GateMachineState, overrides: Partial<Parameters<typeof GateModuleView>[0]> = {}) {
  const onEdit = vi.fn()
  const onSubmit = vi.fn()
  const onViewCurrent = vi.fn()
  const gate = state.tag === 'no_gate' ? promptGate : state.gate
  render(
    <MemoryRouter>
      <GateModuleView
        threadId="th-1"
        gate={gate}
        machineState={state}
        onEdit={onEdit}
        onSubmit={onSubmit}
        onViewCurrent={onViewCurrent}
        {...overrides}
      />
    </MemoryRouter>,
  )
  return { onEdit, onSubmit, onViewCurrent }
}

describe('GateModuleView machine states', () => {
  it('renders nothing in no_gate', () => {
    renderState({ tag: 'no_gate' })
    expect(screen.queryByTestId('gate-module')).not.toBeInTheDocument()
  })

  it('submitting locks the action bar and shows the Resuming chip', () => {
    renderState({
      tag: 'submitting',
      gate: promptGate,
      action: 'approve',
      draft: initialDraftFor(promptGate),
    })
    expect(screen.getByTestId('gate-resuming')).toHaveTextContent('Resuming (approve)…')
    const bar = screen.getByTestId('gate-actions')
    for (const button of within(bar).getAllByRole('button')) {
      expect(button).toBeDisabled()
    }
    // The prompt editors lock too (submitting disables editing).
    for (const editor of screen.getAllByTestId('codemirror')) {
      expect(editor).toHaveAttribute('readonly')
    }
  })

  it('awaiting_agent shows the gate-will-reopen banner (revise wording)', () => {
    renderState({ tag: 'awaiting_agent', gate: phaseGate, action: 'revise' })
    expect(screen.getByTestId('gate-awaiting')).toHaveTextContent(
      'Agent working on your revision instructions — the gate will reopen.',
    )
    expect(screen.queryByTestId('gate-actions')).not.toBeInTheDocument()
  })

  it('awaiting_agent explains that prompt edits will be re-reviewed', () => {
    renderState({ tag: 'awaiting_agent', gate: promptGate, action: 'modify' })
    expect(screen.getByTestId('gate-awaiting')).toHaveTextContent(
      'Agent working on your prompt edits — the gate will reopen.',
    )
  })

  it('superseded(conflict) says another operator resumed; View current resets', async () => {
    const user = userEvent.setup()
    const { onViewCurrent } = renderState({ tag: 'superseded', gate: promptGate, by: 'conflict' })
    const banner = screen.getByTestId('gate-superseded')
    expect(banner).toHaveAttribute('data-by', 'conflict')
    expect(banner).toHaveTextContent('Another operator resumed this gate')
    await user.click(within(banner).getByRole('button', { name: 'View current state' }))
    expect(onViewCurrent).toHaveBeenCalledTimes(1)
  })

  it('superseded(cleared) says actioned elsewhere or replaced', () => {
    renderState({ tag: 'superseded', gate: promptGate, by: 'cleared' })
    expect(screen.getByTestId('gate-superseded')).toHaveTextContent(
      'Gate actioned elsewhere or replaced',
    )
  })

  it('failed shows the error, keeps the draft visible, and Retry resubmits the same action', async () => {
    const user = userEvent.setup()
    const { onSubmit } = renderState({
      tag: 'failed',
      gate: promptGate,
      action: 'modify',
      draft: { prompt: { system: 'EDITED SYSTEM', user: 'EDITED USER' } },
      error: new Error('resume exploded'),
    })
    const card = screen.getByTestId('gate-failed')
    expect(card).toHaveTextContent('Resume failed: resume exploded')
    // Draft intact proof: the editors render the preserved draft text.
    const panel = screen.getByTestId('prompt-review-panel')
    const system = within(panel).getByLabelText('System Prompt')
    expect(system).toHaveValue('EDITED SYSTEM')
    // And the dirty chip reflects the preserved diff.
    expect(screen.getByTestId('gate-dirty-chip')).toBeInTheDocument()

    await user.click(within(card).getByRole('button', { name: 'Retry modify' }))
    expect(onSubmit).toHaveBeenCalledWith('modify')
  })
})
