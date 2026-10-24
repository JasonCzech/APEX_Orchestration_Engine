/**
 * Presets in the wizard: Start offers presets for the primary application
 * (F-01, F-16); the preset bar tracks changes with Reset and Detach (F-05);
 * Save as preset stores the configured test (F-02); launches record which
 * preset they started from; and only the primary system asks for a project
 * (F-17).
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { authenticatedState } from '@/test/render'
import { server } from '@/test/server'

import { fillScope, flushAndUnmountWizard, installWizardHandlers, renderWizard } from './wizardTestUtils'

const NOW = '2026-10-20T00:00:00Z'
function teamPreset(id: string, name: string, applicationId: string | null, sections: Record<string, unknown>, version = 1) {
  return {
    id, name, description: null, project_id: 'demo', application_id: applicationId, visibility: 'project',
    payload: { schema_version: 1, sections }, version, created_by: 'Priya', updated_by: 'Priya',
    created_at: NOW, updated_at: NOW, archived_at: null, mine: false, editable: true,
  }
}
const CHECKOUT_SOAK = teamPreset('preset-soak', 'Checkout soak', 'app-checkout', {
  execution: { engine: 'apex_load', golden_configurable: { load_test: { vusers: 300, duration_s: 3600 } } },
  phases: { phases: null, schema_version: 2, gates_mode: 'all_auto' },
}, 2)
const GENERAL = teamPreset('preset-general', 'Review everything', null, { phases: { phases: null, schema_version: 2, gates_mode: 'all_gated' } })
const OTHER_APP = teamPreset('preset-other', 'Search spike', 'app-search', { phases: { phases: null, schema_version: 2, gates_mode: 'all_auto' } })

function installPresets() {
  const created: Record<string, unknown>[] = []
  server.use(
    http.get('*/v1/test-presets', () => HttpResponse.json([CHECKOUT_SOAK, GENERAL, OTHER_APP])),
    http.post('*/v1/test-presets', async ({ request }) => {
      const body = await request.json() as Record<string, unknown>
      created.push(body)
      return HttpResponse.json({ ...teamPreset('preset-new', String(body.name), body.application_id as string | null, (body.payload as { sections: Record<string, unknown> }).sections), visibility: body.visibility, mine: true }, { status: 201 })
    }),
  )
  return created
}

async function chooseCheckout(user: ReturnType<typeof userEvent.setup>) {
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Project' }), 'demo')
  await waitFor(() => expect(screen.getByRole('option', { name: 'Checkout' })).toBeInTheDocument())
  await user.selectOptions(screen.getByRole('combobox', { name: 'Application' }), 'app-checkout')
}

describe('presets in the new-test wizard', () => {
  it('starts from a preset for the primary application, keeping other applications behind a toggle', async () => {
    installWizardHandlers()
    installPresets()
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new')

    expect(await screen.findByRole('heading', { name: 'Start a new test' })).toBeInTheDocument()
    await chooseCheckout(user)
    const forApp = await screen.findByRole('region', { name: 'For Checkout' })
    expect(within(forApp).getByText('Checkout soak')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'General (any application)' })).getByText('Review everything')).toBeInTheDocument()
    // Presets for other applications would change the systems under test.
    expect(screen.queryByText('Search spike')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Show presets for other applications (1)' }))
    expect(within(screen.getByRole('region', { name: 'Other applications' })).getByText('Search spike')).toBeInTheDocument()

    await user.click(within(forApp).getByRole('button', { name: 'Use preset: Checkout soak' }))
    expect(await screen.findByRole('tab', { name: 'Scope' })).toHaveAttribute('aria-selected', 'true')
    const bar = screen.getByTestId('preset-bar')
    expect(bar).toHaveTextContent('Checkout soak')
    expect(bar).toHaveTextContent('v2')
    expect(bar).toHaveTextContent('no changes')
    // The primary system chosen on Start is kept; the preset filled the rest.
    expect(screen.getByRole('combobox', { name: 'Project' })).toHaveValue('demo')
    expect(screen.getByRole('group', { name: 'Backend · Primary system' })).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    expect(screen.getByRole('radio', { name: /APEX Load/ })).toHaveAttribute('aria-checked', 'true')
    await flushAndUnmountWizard(rendered)
  })

  it('starts blank with the primary system in the chosen tier', async () => {
    installWizardHandlers()
    installPresets()
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new')
    await chooseCheckout(user)
    await user.click(screen.getByRole('radio', { name: 'Frontend' }))
    await user.click(screen.getByRole('radio', { name: /Blank test/ }))
    await user.click(screen.getByRole('button', { name: 'Start blank test' }))
    const frontend = await screen.findByRole('group', { name: 'Frontend · Primary system' })
    expect(within(frontend).getByRole('combobox', { name: 'Application' })).toHaveValue('app-checkout')
    expect(screen.getByTestId('preset-bar')).toHaveTextContent('Not started from a preset')
    await flushAndUnmountWizard(rendered)
  })

  it('counts changes since the preset, and resets or detaches', async () => {
    installWizardHandlers()
    installPresets()
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=config&preset=team:preset-soak')
    const bar = await screen.findByTestId('preset-bar')
    await waitFor(() => expect(bar).toHaveTextContent('Checkout soak'))
    expect(bar).toHaveTextContent('no changes')

    await user.click(screen.getByRole('radio', { name: 'Review every phase' }))
    expect(bar).toHaveTextContent('1 change')
    await user.click(within(bar).getByRole('button', { name: 'View changes' }))
    const changes = await screen.findByRole('dialog', { name: 'Changes since the preset' })
    expect(within(changes).getByRole('row', { name: 'Review policy Run automatically Review every phase' })).toBeInTheDocument()
    await user.click(within(changes).getByRole('button', { name: 'Close' }))

    await user.click(within(bar).getByRole('button', { name: 'Reset to preset' }))
    await user.click(within(await screen.findByRole('dialog', { name: 'Reset to preset' })).getByRole('button', { name: 'Reset' }))
    expect(screen.getByRole('radio', { name: 'Run automatically' })).toBeChecked()
    expect(bar).toHaveTextContent('no changes')

    await user.click(within(bar).getByRole('button', { name: 'Detach' }))
    expect(bar).toHaveTextContent('Not started from a preset')
    expect(screen.getByRole('radio', { name: 'Run automatically' })).toBeChecked()
    await flushAndUnmountWizard(rendered)
  })

  it('saves the configured test as a team preset for its application and tracks it', async () => {
    installWizardHandlers()
    const created = installPresets()
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=scope', authenticatedState('operator', 'Op', [{ project_id: 'demo', app_id: null }]))
    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: 'Save as preset…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Save as preset' })
    expect(within(dialog).getByRole('textbox', { name: 'Name' })).toHaveValue('Checkout soak')
    await user.clear(within(dialog).getByRole('textbox', { name: 'Name' }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Checkout nightly')
    expect(within(dialog).getByRole('radio', { name: /team/ })).toBeChecked()
    expect(within(dialog).getByRole('checkbox', { name: /Offer it only for Checkout tests/ })).toBeChecked()
    // Title and request are opt-in.
    expect(within(dialog).getByRole('checkbox', { name: /Title and request/ })).not.toBeChecked()
    await user.click(within(dialog).getByRole('button', { name: 'Save preset' }))

    await waitFor(() => expect(created).toHaveLength(1))
    expect(created[0]).toMatchObject({ name: 'Checkout nightly', project_id: 'demo', application_id: 'app-checkout', visibility: 'project' })
    expect(Object.keys((created[0]!.payload as { sections: object }).sections)).toEqual(['systems', 'execution', 'phases', 'prompts'])
    expect(await screen.findByTestId('preset-bar')).toHaveTextContent('Checkout nightly')
    expect(screen.getByTestId('review-preset')).toHaveTextContent('Started from preset Checkout nightly')
    await flushAndUnmountWizard(rendered)
  })

  it('records which preset a launch started from, and whether it was edited', async () => {
    installWizardHandlers()
    installPresets()
    const launches: Record<string, unknown>[] = []
    server.use(http.post('*/v1/pipelines', async ({ request }) => {
      launches.push(await request.json() as Record<string, unknown>)
      return HttpResponse.json({ thread_id: 'thread-new', run_id: 'run-1', stream_url: '/s' }, { status: 202 })
    }))
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=scope&preset=team:preset-general')
    await waitFor(() => expect(screen.getByTestId('preset-bar')).toHaveTextContent('Review everything'))
    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: 'Launch test' }))
    await waitFor(() => expect(launches).toHaveLength(1))
    expect(launches[0]!['preset']).toEqual({ kind: 'team', id: 'preset-general', version: 1, name: 'Review everything', modified: false })
    await flushAndUnmountWizard(rendered)
  })

  it('explains a deep link to an archived preset instead of applying it', async () => {
    installWizardHandlers()
    installPresets()
    server.use(http.get('*/v1/test-presets/preset-old', () => HttpResponse.json({ ...teamPreset('preset-old', 'Old soak', 'app-checkout', { phases: { phases: null, schema_version: 2, gates_mode: 'all_auto' } }), archived_at: NOW })))
    const rendered = renderWizard('/runs/new?preset=team:preset-old')
    expect(await screen.findByText('Old soak is archived. Unarchive it on its page to start tests from it.')).toBeInTheDocument()
    expect(screen.getByTestId('preset-bar')).toHaveTextContent('Not started from a preset')
    await flushAndUnmountWizard(rendered)
  })

  it('updates a preset on its own application even after the primary system changed', async () => {
    installWizardHandlers()
    installPresets()
    const updates: Record<string, unknown>[] = []
    server.use(http.put('*/v1/test-presets/:id', async ({ request }) => {
      const body = await request.json() as Record<string, unknown>
      updates.push(body)
      return HttpResponse.json({ ...CHECKOUT_SOAK, version: 3 })
    }))
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=scope&preset=team:preset-soak', authenticatedState('operator', 'Op', [{ project_id: 'demo', app_id: null }]))
    await waitFor(() => expect(screen.getByTestId('preset-bar')).toHaveTextContent('Checkout soak'))
    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: 'Save as preset…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Save as preset' })
    expect(within(dialog).getByRole('radio', { name: /Update Checkout soak/ })).toBeChecked()
    await user.click(within(dialog).getByRole('button', { name: 'Update preset' }))
    await waitFor(() => expect(updates).toHaveLength(1))
    expect(updates[0]).toMatchObject({ application_id: 'app-checkout', expected_version: 2 })
    await flushAndUnmountWizard(rendered)
  })

  it('asks for a project only on the primary system and derives the others', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await fillScope(user, screen)
    await user.click(screen.getByRole('button', { name: 'Add middle-tier system' }))
    const middle = screen.getByRole('group', { name: 'Middle tier' })
    expect(within(middle).queryByRole('combobox', { name: 'Project' })).not.toBeInTheDocument()
    await user.selectOptions(within(middle).getByRole('combobox', { name: 'Application' }), 'app-checkout')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    const scope = JSON.parse(screen.getByTestId('launch-payload-json').textContent!).configurable.test_scope
    expect(scope.participants.map((p: { layer: string; project_id: string }) => [p.layer, p.project_id])).toEqual([['backend', 'demo'], ['middle', 'demo']])
    await flushAndUnmountWizard(rendered)
  })
})
