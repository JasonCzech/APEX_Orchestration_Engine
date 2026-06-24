/**
 * Wizard shell: horizontal tab layout, footer launch gating, and draft resume
 * entry points.
 */
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { draftRead, fillScope, installWizardHandlers, renderWizard } from './wizardTestUtils'

describe('NewRunWizard shell', () => {
  it('renders each group as a horizontal tab and keeps Launch disabled until scope is valid', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    renderWizard()

    const tablist = screen.getByRole('tablist', { name: 'New test groups' })
    expect(within(tablist).getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
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

    const launch = screen.getByRole('button', { name: 'Launch Pipeline' })
    expect(launch).toBeDisabled()
    expect(screen.getAllByText('Scope: Title is required')).toHaveLength(2)
    expect(screen.getAllByText('Scope: Request is required')).toHaveLength(2)

    await fillScope(user, screen)
    expect(launch).toBeEnabled()
  })

  it('shows the resume panel on first visit and hides it once a draft is chosen', async () => {
    installWizardHandlers([
      draftRead({ id: 'draft-9', title: 'Black Friday soak', payload: { title: 'Resumed soak' } }),
    ])
    const user = userEvent.setup()
    const { router } = renderWizard()

    const select = await screen.findByLabelText('Resume draft')
    expect(screen.getByTestId('resume-draft-panel')).toBeInTheDocument()

    await user.selectOptions(select, 'draft-9')

    expect(await screen.findByLabelText('Title')).toHaveValue('Resumed soak')
    expect(router.state.location.search).toContain('draft=draft-9')
    expect(screen.queryByTestId('resume-draft-panel')).not.toBeInTheDocument()
  })
})
