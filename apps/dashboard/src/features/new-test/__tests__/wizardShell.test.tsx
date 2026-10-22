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
  it('confirms participant changes, preserves authored workload, and retains documents for revalidation', async () => {
    installWizardHandlers([draftRead({ id: 'draft-scope', payload: {
      title: 'Existing test', request: 'Existing request',
      scope: { project_id: 'demo', app_id: 'app-checkout', environment_id: 'env-staging', test_scope: {
        schema_version: 1,
        participants: [{ id: 'backend', layer: 'backend', project_id: 'demo', application_id: 'app-checkout', environment_id: 'env-staging' }],
        primary_participant_id: 'backend', traffic_entry_participant_id: 'backend',
      } },
      document_ids: ['doc-existing'], context_summary_ids: ['summary-existing'],
      config: { golden_configurable: { load_test: { vusers: 75, ramp_s: 10, duration_s: 60, slas: { p95_ms: 200 } } } },
      prompt_overrides: { 'phase/story_analysis/system': { content: 'Edited prompt' } },
    } })])
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?draft=draft-scope')
    const project = await screen.findByLabelText('Project')
    await waitFor(() => expect(project).toHaveValue('demo'))
    await user.selectOptions(project, '')
    const dialog = await screen.findByRole('dialog', { name: 'Change test scope' })
    expect(dialog).toHaveTextContent('Context tied to removed systems must be revalidated')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(project).toHaveValue('demo')
    await user.selectOptions(project, '')
    await user.click(within(await screen.findByRole('dialog', { name: 'Change test scope' })).getByRole('button', { name: 'Change scope' }))
    expect(project).toHaveValue('')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    expect(screen.getByTestId('review-issues')).toHaveTextContent('Attached document doc-existing is missing or unavailable')
    expect(screen.getByRole('button', { name: 'Remove missing document' })).toBeInTheDocument()
    const preview = JSON.parse(screen.getByTestId('launch-payload-json').textContent!)
    expect(preview.document_ids).toEqual(['doc-existing'])
    expect(preview.configurable.project_id).toBe('')
    expect(preview.configurable.load_test).toEqual({ vusers: 75, ramp_s: 10, duration_s: 60, slas: { p95_ms: 200 } })
    await flushAndUnmountWizard(rendered)
  })


  it('focuses the exact missing field after returning from Review', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: 'Scope: Title is required' }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveFocus())
    await flushAndUnmountWizard(rendered)
  })

  it('keeps the footer to one row and discloses outstanding items on demand', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    const footer = document.querySelector('.wizard-footer') as HTMLElement
    const disclosure = footer.querySelector('details.wizard-footer-issues') as HTMLDetailsElement
    expect(disclosure).not.toBeNull()
    expect(disclosure.open).toBe(false)
    const summary = disclosure.querySelector('summary')!
    expect(summary).toHaveTextContent(/items? needs? attention$/)
    expect(within(footer).getByRole('status')).toHaveTextContent(/items? needs? attention$/)
    expect(footer).not.toHaveTextContent('Plan is valid')
    expect(screen.getByRole('button', { name: 'Save draft' })).toHaveAccessibleDescription('Saving does not launch a test.')
    await user.click(summary)
    expect(disclosure.open).toBe(true)
    // jsdom does not focus a clicked <summary>; browsers do.
    summary.focus()
    await user.keyboard('{Escape}')
    expect(disclosure.open).toBe(false)
    expect(summary).toHaveFocus()
    await user.click(summary)
    await user.click(within(footer).getByRole('button', { name: 'Scope: Title is required' }))
    expect(disclosure.open).toBe(false)
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveFocus())
    await flushAndUnmountWizard(rendered)
  })

  it('closes the issue list when the step changes so it never covers the next form', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    const opened = document.querySelector('details.wizard-footer-issues') as HTMLDetailsElement
    await user.click(opened.querySelector('summary')!)
    expect(opened.open).toBe(true)
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    const current = document.querySelector('details.wizard-footer-issues') as HTMLDetailsElement
    expect(current.open).toBe(false)
    await flushAndUnmountWizard(rendered)
  })

  it('lays out each system as a card and points readiness links at its missing parts', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: /Scope: Complete the system topology/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add backend' })).toHaveFocus())
    await user.click(screen.getByRole('button', { name: 'Add backend' }))
    expect(screen.queryByRole('button', { name: 'Add backend' })).not.toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Backend' })).toHaveClass('wizard-participant')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: 'Scope: Choose a project, application, and environment for every participant.' }))
    await waitFor(() => expect(screen.getByLabelText('Project')).toHaveFocus())
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: 'Scope: Select the primary system under test.' }))
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Primary system under test' })).toHaveFocus())
    expect(screen.queryByText(/Project is required/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry participant verification' })).not.toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })

  it('marks each step tab with its own outstanding items', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    expect(screen.getByRole('tab', { name: 'Scope' })).toHaveAccessibleDescription(/^\d+ items need attention$/)
    expect(screen.getByRole('tab', { name: 'Tickets' })).not.toHaveAccessibleDescription()
    await fillScope(user, screen)
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Scope' })).toHaveAccessibleDescription('Complete'))
    await flushAndUnmountWizard(rendered)
  })

  it('renders each group as a horizontal tab and routes through review before launching', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()

    const tablist = screen.getByRole('tablist', { name: 'New test groups' })
    expect(within(tablist).getAllByRole('tab').map((tab) => tab.getAttribute('aria-label'))).toEqual([
      'Scope',
      'Config',
      'Tickets',
      'Knowledge',
      'Prompts',
      'Review',
    ])
    expect(screen.getByRole('tab', { name: 'Scope' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel', { name: 'Scope' })).toHaveTextContent('Test scope')
    expect(screen.queryByRole('tabpanel', { name: 'Tickets' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Tickets' }))
    expect(screen.getByRole('tab', { name: 'Tickets' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.getByRole('tabpanel', { name: 'Tickets' })).toHaveTextContent('Tickets')
    expect(screen.queryByRole('tabpanel', { name: 'Scope' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Scope' }))

    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Scope: Title is required' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByRole('tab', { name: 'Config' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByRole('tab', { name: 'Scope' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    expect(screen.getByRole('button', { name: 'Launch test' })).toBeDisabled()
    await user.click(screen.getByRole('tab', { name: 'Scope' }))
    await fillScope(user, screen)
    for (let step = 0; step < 4; step++) await user.click(screen.getByRole('button', { name: 'Next' }))
    await user.click(screen.getByRole('button', { name: 'Review test' }))
    expect(screen.getByRole('button', { name: 'Launch test' })).toBeEnabled()
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
