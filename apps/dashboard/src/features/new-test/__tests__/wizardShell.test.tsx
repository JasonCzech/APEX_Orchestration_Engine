/**
 * Wizard shell: single-scroll layout with stacked sections, footer launch
 * gating, and draft resume entry points.
 */
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { draftRead, fillScope, installWizardHandlers, renderWizard } from './wizardTestUtils'

describe('NewRunWizard shell', () => {
  it('renders the stacked sections and keeps Launch disabled until scope is valid', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    renderWizard()

    expect(screen.getByRole('region', { name: 'Run Scope' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Work Items' })).toBeInTheDocument()
    expect(screen.getByTestId('document-dropzone')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Execution Configuration' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Prompt Selection' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Launch Preview' })).toBeInTheDocument()

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
