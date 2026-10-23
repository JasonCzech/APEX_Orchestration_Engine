/**
 * Review step: a launch summary in wizard order beside a readiness checklist,
 * with each section, phase and prompt leading back to the step that owns it.
 */
import { screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { fillScope, flushAndUnmountWizard, installWizardHandlers, renderWizard } from './wizardTestUtils'

describe('ReviewStep', () => {
  it('summarizes the test and turns the checklist into fix actions', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await user.click(screen.getByRole('tab', { name: 'Review' }))

    const checklist = screen.getByRole('complementary', { name: 'Launch readiness' })
    expect(checklist).toHaveTextContent(/items? needs? attention/)
    expect(within(screen.getByRole('list', { name: 'Launch checks' })).getByText('Complete the outstanding planning items before launch.')).toBeInTheDocument()
    // Sections read in wizard order, each with its own edit action and status.
    const review = screen.getByRole('region', { name: /^Review$/ })
    expect(within(review).getAllByRole('heading', { level: 3 }).map(heading => heading.textContent)).toEqual([
      'Launch readiness', 'Untitled test', 'Scope', 'Config', 'Workload and acceptance', 'Tickets', 'Knowledge', 'Prompts',
    ])
    expect(within(review).getByRole('region', { name: 'Tickets' })).toHaveTextContent('Optional')
    // The checklist comes first in reading and tab order.
    expect(checklist.compareDocumentPosition(within(review).getByRole('region', { name: 'Scope' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(review).getByRole('region', { name: 'Scope' })).toHaveTextContent(/\d+ to fix/)

    await user.click(within(checklist).getByRole('button', { name: 'Scope: Title is required' }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveFocus())

    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await waitFor(() => expect(screen.getByRole('complementary', { name: 'Launch readiness' })).toHaveTextContent('Ready to launch'))
    expect(screen.queryByTestId('review-issues')).not.toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })

  it('shows the phase plan and prompts, and opens Prompts from either', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    await user.click(within(screen.getByRole('group', { name: 'Phase execution' })).getByRole('button', { name: 'Execution' }))
    await user.click(screen.getByRole('tab', { name: 'Prompts' }))
    await waitFor(() => expect(screen.getByRole('tab', { name: /Story Analysis/ })).toHaveTextContent('story_analysis/system'))
    await user.click(screen.getByRole('tab', { name: 'Review' }))

    const plan = screen.getByRole('list', { name: 'Phase plan' })
    expect(within(plan).getAllByRole('button')).toHaveLength(7)
    expect(within(plan).getByRole('button', { name: 'Story Analysis: Agent, Review: prompt + output. Opens it in Prompts' })).toBeInTheDocument()
    expect(within(plan).getByRole('button', { name: 'Execution: Person, bypassed for agentic execution, Waits for a person. Opens it in Prompts' })).toBeInTheDocument()

    const prompts = screen.getByRole('table')
    expect(within(prompts).getByRole('row', { name: /Phase context — Execution/ })).toHaveTextContent('Bypassed: a person completes this phase')
    expect(within(prompts).getAllByRole('rowheader').map(cell => cell.textContent).slice(0, 3)).toEqual(['System Prompt', 'Application context', 'Environment Context'])

    // Each phase and prompt opens its own slot on the Prompts step.
    await user.click(within(plan).getByRole('button', { name: /^Test Planning: Agent/ }))
    expect(screen.getByRole('tab', { name: 'Prompts' })).toHaveAttribute('aria-selected', 'true')
    await waitFor(() => expect(screen.getByRole('tab', { name: /Test Planning/ })).toHaveAttribute('aria-selected', 'true'))
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(within(screen.getByRole('table')).getByRole('button', { name: /^default · v1/ }))
    await waitFor(() => expect(screen.getByRole('tab', { name: /System prompt/ })).toHaveAttribute('aria-selected', 'true'))
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(within(screen.getByRole('list', { name: 'Phase plan' })).getByRole('button', { name: /^Execution: Person/ }))
    await waitFor(() => expect(screen.getByRole('tab', { name: /Execution/ })).toHaveAttribute('aria-selected', 'true'))
    expect(screen.getByRole('tabpanel', { name: /Execution/ })).toHaveTextContent('Bypassed for agentic execution')
    await flushAndUnmountWizard(rendered)
  })

  it('collapses a long request behind a toggle', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await user.type(screen.getByLabelText('Request'), 'Hold checkout under peak load. '.repeat(10))
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    const toggle = screen.getByRole('button', { name: 'Show full request' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await user.click(toggle)
    expect(screen.getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true')
    await flushAndUnmountWizard(rendered)
  })
})
