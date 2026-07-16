/**
 * Draft round-trip: ?draft=<id> restores the stored WizardDraft on mount, and
 * the first-visit "Resume draft" picker loads a stored draft + sets the URL.
 */
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { queryKeys } from '@/api/queryKeys'
import { server } from '@/test/server'

import {
  draftRead,
  flushAndUnmountWizard,
  installWizardHandlers,
  renderWizard,
} from './wizardTestUtils'

const STORED: Record<string, unknown> = {
  title: 'Resumed soak',
  request: 'Pick up where we left off',
  scope: { project_id: 'demo', app_id: 'app-checkout', environment_id: 'env-staging' },
  // Legacy drafts are intentionally accepted as unbound selections. The
  // work-items step will require an explicit revalidation before launch.
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
      draftRead({ id: 'draft-7', title: 'Resumed soak', payload: STORED }),
    ])
    const rendered = renderWizard('/runs/new?draft=draft-7')

    expect(await screen.findByLabelText('Title')).toHaveValue('Resumed soak')
    expect(screen.getByLabelText('Request')).toHaveValue('Pick up where we left off')
    expect(screen.getByLabelText('Project')).toHaveValue('demo')
    // The catalog selects rehydrate from the stored scope ids.
    await waitFor(() => expect(screen.getByLabelText('Application')).toHaveValue('app-checkout'))
    await waitFor(() => expect(screen.getByLabelText('Environment')).toHaveValue('env-staging'))
    // No resume picker when the URL already names a draft.
    expect(screen.queryByTestId('resume-draft-panel')).not.toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })

  it('offers Resume draft on first visit and loads the picked draft + URL', async () => {
    installWizardHandlers([
      draftRead({ id: 'draft-9', title: 'Black Friday soak', payload: STORED }),
    ])
    const user = userEvent.setup()
    const rendered = renderWizard()
    const { router } = rendered

    const select = await screen.findByLabelText('Resume draft')
    await user.selectOptions(select, 'draft-9')

    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Resumed soak'))
    await waitFor(() => expect(router.state.location.search).toContain('draft=draft-9'))
    // Picker disappears once a draft is active.
    expect(screen.queryByTestId('resume-draft-panel')).not.toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })

  it('keeps saved-draft access visible when the initial list fails and retries it', async () => {
    const stored = draftRead({
      id: 'draft-retry',
      title: 'Retryable draft',
      payload: STORED,
    })
    installWizardHandlers([stored])
    let draftsFail = true
    server.use(
      http.get('*/v1/drafts', () =>
        draftsFail
          ? HttpResponse.json({ detail: 'drafts offline' }, { status: 500 })
          : HttpResponse.json([stored]),
      ),
    )
    const user = userEvent.setup()
    const rendered = renderWizard()

    const panel = await screen.findByTestId('resume-draft-panel')
    const alert = within(panel).getByRole('alert')
    expect(alert).toHaveTextContent('Saved drafts unavailable')
    expect(screen.queryByLabelText('Resume draft')).not.toBeInTheDocument()

    draftsFail = false
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))

    const select = await screen.findByLabelText('Resume draft')
    expect(within(select).getByRole('option', { name: 'Retryable draft' })).toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })

  it('keeps cached draft choices visible under a failed refresh warning', async () => {
    const stored = draftRead({
      id: 'draft-cached',
      title: 'Cached draft',
      payload: STORED,
    })
    installWizardHandlers([stored])
    let draftsFail = false
    server.use(
      http.get('*/v1/drafts', () =>
        draftsFail
          ? HttpResponse.json({ detail: 'draft refresh failed' }, { status: 500 })
          : HttpResponse.json([stored]),
      ),
    )
    const user = userEvent.setup()
    const rendered = renderWizard()
    const { queryClient } = rendered

    const select = await screen.findByLabelText('Resume draft')
    expect(within(select).getByRole('option', { name: 'Cached draft' })).toBeInTheDocument()

    draftsFail = true
    await act(async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.drafts.list(),
        exact: true,
      })
    })

    const alert = await screen.findByText(/Showing cached data/)
    expect(alert).toHaveTextContent('draft refresh failed')
    expect(screen.getByLabelText('Resume draft')).toBe(select)
    expect(within(select).getByRole('option', { name: 'Cached draft' })).toBeInTheDocument()

    draftsFail = false
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText(/Showing cached data/)).not.toBeInTheDocument())
    await flushAndUnmountWizard(rendered)
  })

  it('disables the resume picker while the selected draft is loading', async () => {
    const stored = draftRead({
      id: 'draft-slow',
      title: 'Slow draft',
      payload: STORED,
    })
    installWizardHandlers([stored])
    let release!: () => void
    const blocked = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.get('*/v1/drafts/:id', async () => {
        await blocked
        return HttpResponse.json(stored)
      }),
    )
    const user = userEvent.setup()
    const rendered = renderWizard()

    const select = await screen.findByLabelText('Resume draft')
    await user.selectOptions(select, 'draft-slow')
    await waitFor(() => expect(select).toBeDisabled())
    expect(screen.getByText('Loading draft…')).toBeInTheDocument()

    release()
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Resumed soak'))
    await flushAndUnmountWizard(rendered)
  })

  it('blocks URL and picker draft switches while a work-item lookup targets the current draft', async () => {
    const stored = draftRead({
      id: 'draft-blocked',
      title: 'Blocked switch target',
      payload: STORED,
    })
    installWizardHandlers([stored])
    let draftLoadCount = 0
    let markLookupStarted!: () => void
    const lookupStarted = new Promise<void>((resolve) => {
      markLookupStarted = resolve
    })
    let releaseLookup!: () => void
    const lookupRelease = new Promise<void>((resolve) => {
      releaseLookup = resolve
    })
    server.use(
      http.get('*/v1/drafts/draft-blocked', () => {
        draftLoadCount += 1
        return HttpResponse.json(stored)
      }),
      http.get('*/v1/work-tracking/items/:key', async () => {
        markLookupStarted()
        await lookupRelease
        return HttpResponse.json({
          key: 'PHX-241',
          title: 'Current draft item',
          kind: 'story',
          status: 'open',
          description: '',
          connection_id: 'conn-jira',
          provider: 'jira',
        })
      }),
    )
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=work-items')
    const { router } = rendered

    const select = await screen.findByLabelText('Resume draft')
    await user.type(screen.getByLabelText('Add by key'), 'PHX-241')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await lookupStarted
    expect(select).toBeDisabled()

    await act(async () => {
      await router.navigate('/runs/new?step=work-items&draft=draft-blocked')
    })
    await waitFor(() =>
      expect(router.state.location.search).not.toContain('draft=draft-blocked'),
    )
    expect(draftLoadCount).toBe(0)
    expect(
      screen.getByText('Wait for in-progress wizard operations before switching drafts.'),
    ).toHaveAttribute('role', 'alert')

    await act(async () => releaseLookup())
    const selected = await screen.findByTestId('selected-work-items')
    expect(within(selected).getByText('PHX-241')).toBeInTheDocument()
    expect(screen.queryByText('Resumed soak')).not.toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })
})
