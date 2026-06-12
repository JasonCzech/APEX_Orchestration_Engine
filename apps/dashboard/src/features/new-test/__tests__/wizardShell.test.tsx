/**
 * Wizard shell: step rail, validation gating Next, and the ?step= URL
 * round-trip (replace-history navigation through the rail and footer).
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { fillScope, installWizardHandlers, renderWizard } from './wizardTestUtils'

describe('NewRunWizard shell', () => {
  it('renders the 6-step rail and disables Next until the scope step is valid', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    renderWizard()

    const rail = screen.getByRole('navigation', { name: 'Wizard steps' })
    expect(rail).toHaveTextContent('Scope')
    expect(rail).toHaveTextContent('Work items')
    expect(rail).toHaveTextContent('Context')
    expect(rail).toHaveTextContent('Config')
    expect(rail).toHaveTextContent('Prompts')
    expect(rail).toHaveTextContent('Review')

    const next = screen.getByRole('button', { name: 'Next' })
    expect(next).toBeDisabled() // title + request still empty

    await fillScope(user, screen)
    expect(next).toBeEnabled()
  })

  it('round-trips the step through the URL: Next/Back/rail clicks update ?step=', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const { router } = renderWizard()

    await fillScope(user, screen)
    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(router.state.location.search).toContain('step=work-items')
    expect(screen.getByLabelText('Add by key')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(router.state.location.search).toContain('step=scope')

    // Visited steps are clickable from the rail; unvisited ones are disabled.
    await user.click(screen.getByRole('button', { name: /Work items/ }))
    await waitFor(() => expect(router.state.location.search).toContain('step=work-items'))
    expect(screen.getByRole('button', { name: /Review/ })).toBeDisabled()
  })

  it('honors a ?step= deep link and falls back to scope for unknown steps', async () => {
    installWizardHandlers()
    renderWizard('/runs/new?step=context')
    expect(await screen.findByTestId('document-dropzone')).toBeInTheDocument()

    installWizardHandlers()
    renderWizard('/runs/new?step=bogus')
    expect(await screen.findByLabelText('Title')).toBeInTheDocument()
  })
})
