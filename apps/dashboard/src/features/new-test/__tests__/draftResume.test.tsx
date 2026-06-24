/**
 * Draft round-trip: ?draft=<id> restores the stored WizardDraft on mount, and
 * the first-visit "Resume draft" picker loads a stored draft + sets the URL.
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { WizardDraft } from '../wizardState'
import { draftRead, installWizardHandlers, renderWizard } from './wizardTestUtils'

const STORED: Partial<WizardDraft> = {
  title: 'Resumed soak',
  request: 'Pick up where we left off',
  scope: { project_id: 'demo', app_id: 'app-checkout', environment_id: 'env-staging' },
  work_item_keys: ['PHX-241'],
  config: {
    engine: 'apex_load',
    phases: null,
    prompt_focus_phase: 'story_analysis',
    gates_mode: 'all_auto',
    golden_config_id: null,
  },
}

describe('wizard draft resume', () => {
  it('restores state from the ?draft= URL on mount', async () => {
    installWizardHandlers([
      draftRead({ id: 'draft-7', title: 'Resumed soak', payload: STORED as Record<string, unknown> }),
    ])
    renderWizard('/runs/new?draft=draft-7')

    expect(await screen.findByLabelText('Title')).toHaveValue('Resumed soak')
    expect(screen.getByLabelText('Request')).toHaveValue('Pick up where we left off')
    expect(screen.getByLabelText('Project')).toHaveValue('demo')
    // The catalog selects rehydrate from the stored scope ids.
    await waitFor(() => expect(screen.getByLabelText('Application')).toHaveValue('app-checkout'))
    await waitFor(() => expect(screen.getByLabelText('Environment')).toHaveValue('env-staging'))
    // No resume picker when the URL already names a draft.
    expect(screen.queryByTestId('resume-draft-panel')).not.toBeInTheDocument()
  })

  it('offers Resume draft on first visit and loads the picked draft + URL', async () => {
    installWizardHandlers([
      draftRead({ id: 'draft-9', title: 'Black Friday soak', payload: STORED as Record<string, unknown> }),
    ])
    const user = userEvent.setup()
    const { router } = renderWizard()

    const select = await screen.findByLabelText('Resume draft')
    await user.selectOptions(select, 'draft-9')

    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Resumed soak'))
    await waitFor(() => expect(router.state.location.search).toContain('draft=draft-9'))
    // Picker disappears once a draft is active.
    expect(screen.queryByTestId('resume-draft-panel')).not.toBeInTheDocument()
  })
})
