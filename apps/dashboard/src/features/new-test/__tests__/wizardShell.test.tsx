/**
 * Wizard shell: horizontal tab layout, footer launch gating, and draft resume
 * entry points.
 */
import { screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import {
  draftRead,
  fillScope,
  flushAndUnmountWizard,
  installWizardHandlers,
  renderWizard,
} from './wizardTestUtils'

describe('NewRunWizard shell', () => {
  it('focuses the exact missing field after returning from Review', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: 'Scope: Title is required' }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveFocus())
    await flushAndUnmountWizard(rendered)
  })

  it('renders each group as a horizontal tab and routes through review before launching', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()

    const tablist = screen.getByRole('tablist', { name: 'New test groups' })
    expect(within(tablist).getAllByRole('tab').map((tab) => tab.getAttribute('aria-label'))).toEqual([
      'Scope',
      'Work Items',
      'Context',
      'Config',
      'Prompts',
      'Review',
    ])
    expect(screen.getByRole('tab', { name: 'Scope' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel', { name: 'Scope' })).toHaveTextContent('Run Scope')
    expect(screen.queryByRole('tabpanel', { name: 'Work Items' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Work Items' }))
    expect(screen.getByRole('tab', { name: 'Work Items' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.getByRole('tabpanel', { name: 'Work Items' })).toHaveTextContent('Work Items')
    expect(screen.queryByRole('tabpanel', { name: 'Scope' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Scope' }))

    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled()
    expect(screen.queryByText('Scope: Title is required')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByRole('tab', { name: 'Work Items' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByRole('tab', { name: 'Scope' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    expect(screen.getByRole('button', { name: 'Launch Test' })).toBeDisabled()
    await user.click(screen.getByRole('tab', { name: 'Scope' }))
    await fillScope(user, screen)
    for (let step = 0; step < 4; step++) await user.click(screen.getByRole('button', { name: 'Next' }))
    await user.click(screen.getByRole('button', { name: 'Review Test' }))
    expect(screen.getByRole('button', { name: 'Launch Test' })).toBeEnabled()
    await flushAndUnmountWizard(rendered)
  })

  it('shows the resume panel on first visit and hides it once a draft is chosen', async () => {
    installWizardHandlers([
      draftRead({ id: 'draft-9', title: 'Black Friday soak', payload: { title: 'Resumed soak' } }),
    ])
    const user = userEvent.setup()
    const rendered = renderWizard()
    const { router } = rendered

    const select = await screen.findByLabelText('Resume draft')
    expect(screen.getByTestId('resume-draft-panel')).toBeInTheDocument()

    await user.selectOptions(select, 'draft-9')

    expect(await screen.findByLabelText('Title')).toHaveValue('Resumed soak')
    expect(router.state.location.search).toContain('draft=draft-9')
    expect(screen.queryByTestId('resume-draft-panel')).not.toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })
})
