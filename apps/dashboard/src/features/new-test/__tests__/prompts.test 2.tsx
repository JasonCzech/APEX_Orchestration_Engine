/**
 * Prompts step: catalog preview with provenance chip catalog@vN; [Override
 * for this run] seeds prompt_overrides["phase/<p>"] from the catalog system
 * content ("run override" chip), edits store {content}, revert removes the
 * key. The override payload is asserted through the review step's exact
 * launch JSON.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { server } from '@/test/server'

import { installWizardHandlers, renderWizard } from './wizardTestUtils'

// CodeMirror needs real DOM measurement; mock it as a controlled textarea
// (same boundary mock the hitl/artifacts suites use).
vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({
      value,
      onChange,
      editable,
      readOnly,
      'aria-label': ariaLabel,
    }: {
      value: string
      onChange?: (value: string) => void
      editable?: boolean
      readOnly?: boolean
      'aria-label'?: string
    }) =>
      createElement('textarea', {
        'data-testid': 'codemirror',
        'aria-label': ariaLabel,
        value,
        readOnly: readOnly === true || editable === false,
        onChange: (event: { target: { value: string } }) => onChange?.(event.target.value),
      }),
  }
})

const PHASE_PROMPTS = [
  {
    id: 'p-exec-sys',
    namespace: 'phase',
    key: 'execution/system',
    description: null,
    active_version: { id: 'v-3', version: 3 },
  },
  {
    id: 'p-exec-usr',
    namespace: 'phase',
    key: 'execution/user',
    description: null,
    active_version: { id: 'v-1', version: 1 },
  },
]

const PROMPT_CONTENT: Record<string, string> = {
  'p-exec-sys': 'You are the execution phase operator.',
  'p-exec-usr': 'Run the plan: {{request}}',
}

function promptHandlers() {
  return [
    http.get('*/v1/prompts', () => HttpResponse.json(PHASE_PROMPTS)),
    http.get('*/v1/prompts/:id', ({ params }) => {
      const id = params['id'] as string
      const summary = PHASE_PROMPTS.find((entry) => entry.id === id)
      if (!summary) return HttpResponse.json({ detail: 'not found' }, { status: 404 })
      return HttpResponse.json({ ...summary, content: PROMPT_CONTENT[id] })
    }),
  ]
}

describe('PromptsStep', () => {
  it('previews catalog content with provenance, and override -> edit -> revert round-trips', async () => {
    installWizardHandlers()
    server.use(...promptHandlers())
    const user = userEvent.setup()
    renderWizard('/runs/new?step=prompts')

    // 7 phase accordions (all phases included by default).
    const promptSection = screen.getByRole('tabpanel', { name: 'Prompts' })
    const accordions = within(promptSection).getAllByRole('group')
    expect(accordions.length).toBeGreaterThanOrEqual(7)

    const execution = within(promptSection).getByText('execution').closest('details') as HTMLElement
    await user.click(within(execution).getByText('execution'))

    // Catalog provenance chips + read-only content.
    await waitFor(() => expect(within(execution).getByText('catalog@v3')).toBeInTheDocument())
    expect(within(execution).getByText('catalog@v1')).toBeInTheDocument()
    const viewers = within(execution).getAllByTestId('codemirror')
    expect(viewers[0]).toHaveValue('You are the execution phase operator.')
    expect(viewers[0]).toHaveAttribute('readonly')

    // Override: seeded from the catalog system prompt, editable, chip flips.
    await user.click(within(execution).getByRole('button', { name: 'Override for this run' }))
    const editor = within(execution).getByLabelText('execution system prompt override')
    expect(editor).toHaveValue('You are the execution phase operator.')
    expect(within(execution).getByTestId('override-chip-execution')).toHaveTextContent(
      'run override',
    )

    await user.clear(editor)
    await user.type(editor, 'Custom system prompt for this run')

    // The override lands in the EXACT launch payload under phase/execution.
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    const json = await screen.findByTestId('launch-payload-json')
    const payload = JSON.parse(json.textContent ?? '{}') as {
      configurable: { prompt_overrides?: Record<string, { content: string }> }
    }
    expect(payload.configurable.prompt_overrides).toEqual({
      'phase/execution': { content: 'Custom system prompt for this run' },
    })

    // Revert removes the override key entirely.
    await user.click(screen.getByRole('tab', { name: 'Prompts' }))
    const promptSectionAgain = screen.getByRole('tabpanel', { name: 'Prompts' })
    const executionAgain = within(promptSectionAgain)
      .getByText('execution')
      .closest('details') as HTMLDetailsElement
    if (!executionAgain.open) await user.click(within(executionAgain).getByText('execution'))
    await user.click(
      within(executionAgain).getByRole('button', { name: 'Revert to catalog' }),
    )
    expect(
      within(executionAgain).queryByTestId('override-chip-execution'),
    ).not.toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    const jsonAfter = await screen.findByTestId('launch-payload-json')
    expect(jsonAfter.textContent).not.toContain('prompt_overrides')
  })
})
