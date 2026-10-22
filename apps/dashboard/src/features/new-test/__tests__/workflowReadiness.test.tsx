import { act, screen, waitFor, within } from '@testing-library/react'
import { PHASE_NAMES } from '@apex/pipeline-events'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '@/test/server'
import { buildConfigurable, emptyDraft, parseDraftPayload, assessWizardReadiness } from '../wizardState'
import { workloadIssues } from '../workload'
import { ENVIRONMENTS, fillScope, flushAndUnmountWizard, installWizardHandlers, renderWizard } from './wizardTestUtils'

describe('new-test workflow readiness', () => {
  it('verifies the primary application when the selected connection requires one', async () => {
    installWizardHandlers()
    const requests: Record<string, unknown>[] = []
    server.use(http.post('*/v1/pipelines/execution-readiness', async ({ request }) => {
      const body = await request.json() as Record<string, unknown>
      requests.push(body)
      return HttpResponse.json({ execution_selected: true, configuration_valid: Boolean(body.app_id), requires_application: true,
        detail: body.app_id ? null : 'Select an application for the configured execution connection' })
    }))
    const user = userEvent.setup()
    const rendered = renderWizard()
    await fillScope(user, screen)
    await user.click(screen.getByRole('button', { name: 'Clear traffic entry' }))
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Launch test' })).toBeEnabled())
    expect(requests.at(-1)).toEqual({ project_id: 'demo', app_id: 'app-checkout', engine: 'sim', phases: [...PHASE_NAMES], test_scope: JSON.parse(screen.getByTestId('launch-payload-json').textContent!).configurable.test_scope })
    expect(within(screen.getByRole('region', { name: /^Review$/ })).getByText(/Provider connectivity, credentials and workload\/release eligibility are checked later/)).toBeInTheDocument()
    const preview = JSON.parse(screen.getByTestId('launch-payload-json').textContent!)
    expect(preview.configurable.phases).toEqual([...PHASE_NAMES])
    expect(preview.configurable.environment_id).toBeUndefined()
    await flushAndUnmountWizard(rendered)
  })

  it('permits simulation without a traffic entry while retaining the primary system', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await fillScope(user, screen)
    await user.click(screen.getByRole('button', { name: 'Clear traffic entry' }))
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Launch test' })).toBeEnabled())
    expect(within(screen.getByRole('region', { name: /^Review$/ })).getByText(/Execution connection metadata is valid/)).toBeInTheDocument()
    const preview = JSON.parse(screen.getByTestId('launch-payload-json').textContent!)
    expect(preview.configurable.app_id).toBe('app-checkout')
    expect(preview.configurable.environment_id).toBeUndefined()
    expect(preview.configurable.test_scope.traffic_entry_participant_id).toBeNull()
    await flushAndUnmountWizard(rendered)
  })

  it.each(['unavailable', 'incomplete', 'invalid'] as const)('fails closed on %s connection metadata and offers retry', async mode => {
    installWizardHandlers()
    let fails = true
    server.use(http.post('*/v1/pipelines/execution-readiness', () => fails
      ? HttpResponse.json(mode === 'unavailable' ? { title: 'Configuration service unavailable' } : mode === 'invalid'
        ? { execution_selected: true, configuration_valid: false, requires_application: false, detail: 'Execution configuration could not be verified; ask an administrator to check the connection' } : {}, { status: mode === 'unavailable' ? 503 : 200 })
      : HttpResponse.json({ execution_selected: true, configuration_valid: true, requires_application: false, detail: null })))
    const user = userEvent.setup()
    const rendered = renderWizard()
    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry execution verification' })).toBeVisible())
    expect(screen.getByRole('button', { name: 'Launch test' })).toBeDisabled()
    expect(screen.getByTestId('review-issues')).toHaveTextContent('Execution configuration could not be verified')
    fails = false
    await user.click(screen.getByRole('button', { name: 'Retry execution verification' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Launch test' })).toBeEnabled())
    await flushAndUnmountWizard(rendered)
  })

  it('does not let failed connection metadata block a planning-only run', () => {
    const draft = emptyDraft()
    draft.config.phases = ['story_analysis']
    expect(assessWizardReadiness(draft, undefined, undefined, undefined, { failed: true }).executionIssues).toEqual([])
  })

  it('retries failed participant verification without replacing the selected participant', async () => {
    installWizardHandlers()
    let fails = false
    server.use(http.get('*/v1/catalog/environments', () => fails
      ? HttpResponse.json({ detail: 'Catalog unavailable' }, { status: 503 })
      : HttpResponse.json(ENVIRONMENTS)))
    const user = userEvent.setup()
    const rendered = renderWizard()
    await fillScope(user, screen)
    fails = true
    await act(async () => { await rendered.queryClient.invalidateQueries({ queryKey: ['test-participant-catalog'] }) })
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await waitFor(() => expect(screen.getByTestId('review-issues')).toHaveTextContent('unavailable or does not match its project'))
    expect(screen.getByRole('button', { name: 'Launch test' })).toBeDisabled()
    fails = false
    await user.click(screen.getByRole('tab', { name: 'Scope' }))
    await user.click(screen.getByRole('button', { name: 'Retry participant verification' }))
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Launch test' })).toBeEnabled())
    await flushAndUnmountWizard(rendered)
  })

  it('does not require target approval for execution assigned to HITL', () => {
    const draft = emptyDraft()
    draft.scope.app_id = 'app-checkout'
    draft.scope.environment_id = 'env-staging'
    draft.config.engine = 'apex_load'
    draft.config.phases = []
    expect(assessWizardReadiness(draft, undefined, undefined, {
      environment: { ...ENVIRONMENTS[0]!, target_approved: false, target_version: 0 },
    }).executionIssues).toEqual([])
  })

  it('authors validated workload inputs and previews the exact fractions sent to execution', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    const vusers = screen.getByRole('spinbutton', { name: 'VUsers' })
    await user.clear(vusers)
    await user.type(vusers, '300')
    const errors = screen.getByRole('spinbutton', { name: 'Maximum error rate (%)' })
    await user.clear(errors)
    await user.type(errors, '0.5')
    await user.type(screen.getByRole('spinbutton', { name: 'Minimum throughput (TPS)' }), '85')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    const preview = JSON.parse(screen.getByTestId('launch-payload-json').textContent!)
    expect(preview.configurable.load_test).toEqual({ vusers: 300, ramp_s: 1, duration_s: 2, slas: { p95_ms: 500, error_rate: 0.005, tps_avg: 85 } })
    expect(screen.getByRole('button', { name: 'Launch test' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Edit Workload and acceptance' }))
    await user.clear(screen.getByRole('spinbutton', { name: 'VUsers' }))
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    expect(screen.getByRole('button', { name: 'Launch test' })).toBeDisabled()
    await user.click(within(screen.getByTestId('review-issues')).getByRole('button', { name: /VUsers must be/ }))
    await waitFor(() => expect(screen.getByRole('spinbutton', { name: 'VUsers' })).toHaveFocus())
    await flushAndUnmountWizard(rendered)
  })

  it('blocks unapproved external targets but permits the same environment as simulated context', async () => {
    installWizardHandlers()
    server.use(http.get('*/v1/catalog/environments/:id', () => HttpResponse.json({ ...ENVIRONMENTS[0], target_approved: false, target_version: 0 })))
    const user = userEvent.setup()
    const rendered = renderWizard()
    await fillScope(user, screen)
    await user.selectOptions(screen.getByLabelText('Application'), 'app-checkout')
    await user.selectOptions(screen.getByLabelText('Environment'), 'env-staging')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Launch test' })).toBeEnabled())
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    await user.click(screen.getByRole('radio', { name: /APEX Load/ }))
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    expect(screen.getByRole('button', { name: 'Launch test' })).toBeDisabled()
    expect(screen.getByRole('link', { name: 'Review environment target' })).toHaveAttribute('href', '/environments/env-staging')
    expect(screen.getByText('Environment staging needs an approved target before launch')).toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })

  it('surfaces the server problem title and retries the same request on the current page', async () => {
    installWizardHandlers()
    const bodies: Record<string, unknown>[] = []
    server.use(http.post('*/v1/pipelines', async ({ request }) => {
      bodies.push(await request.json() as Record<string, unknown>)
      return bodies.length === 1
        ? HttpResponse.json({ type: 'about:blank', title: 'environment target not found', status: 404 }, { status: 404 })
        : HttpResponse.json({ thread_id: 'recovered-run', run_id: 'run-one', stream_url: '/stream' }, { status: 202 })
    }))
    const user = userEvent.setup()
    const rendered = renderWizard()
    const { router } = rendered
    await fillScope(user, screen)
    await user.selectOptions(screen.getByLabelText('Application'), 'app-checkout')
    await user.selectOptions(screen.getByLabelText('Environment'), 'env-staging')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Launch test' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Launch test' }))
    expect(await screen.findByText('Launch failed: environment target not found')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Review environment target' })).toHaveAttribute('href', '/environments/env-staging')
    await user.click(screen.getByRole('button', { name: 'Retry launch' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/recovered-run'))
    expect(bodies).toHaveLength(2)
    expect(bodies[1]).toEqual(bodies[0])
    await flushAndUnmountWizard(rendered)
  })

  it.each([['work-items', 'tickets', 'Tickets'], ['context', 'knowledge', 'Knowledge']])('migrates %s bookmarks to the visible vocabulary', async (old, canonical, label) => {
    installWizardHandlers()
    const rendered = renderWizard(`/runs/new?step=${old}`)
    const { router } = rendered
    await waitFor(() => expect(router.state.location.search).toContain(`step=${canonical}`))
    expect(screen.getByRole('tab', { name: label })).toHaveAttribute('aria-selected', 'true')
    await flushAndUnmountWizard(rendered)
  })

  it('retains structured workload in drafts without dropping provider identity', () => {
    const draft = emptyDraft()
    draft.config.golden_configurable = { load_test: { release_id: 'approved-release', mapping_revision: 'r2', vusers: 300, duration_s: 600, ramp_s: 60, slas: { error_rate: 0.005 } } }
    expect(buildConfigurable(parseDraftPayload(JSON.parse(JSON.stringify(draft)))).load_test).toEqual(draft.config.golden_configurable.load_test)
  })

  it.each([null, [], 'legacy', { vusers: 1.5 }, { duration_s: 0 }, { slas: { error_rate: 5 } }, { slas: { response_time: 100 } }, { slas: { 'bad\0key': 1 } }, { slas: Object.fromEntries(Array.from({ length: 33 }, (_, i) => [`metric${i}`, 1])) }])('rejects malformed workload %j before launch', value => {
    expect(workloadIssues(value).length).toBeGreaterThan(0)
  })

  it('does not claim execution readiness for a model-authored draft requiring provider handoff', () => {
    const draft = emptyDraft()
    draft.config.golden_configurable = { agent_backend: 'anthropic' }
    expect(assessWizardReadiness(draft).executionIssues).toEqual([expect.objectContaining({ message: expect.stringContaining('approved provider or Cycle handoff') })])
  })

  it('keeps LoadRunner reservation estimates editable and displays the actual reservation formula', async () => {
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=config')
    await user.click(screen.getByRole('radio', { name: /LoadRunner/ }))
    expect(screen.getByRole('spinbutton', { name: 'VUsers' })).toBeDisabled()
    const duration = screen.getByRole('spinbutton', { name: 'Reservation duration (seconds)' })
    expect(duration).toBeEnabled()
    await user.clear(duration)
    await user.type(duration, '3600')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    expect(screen.getByText(/Timeslot: 76 minutes/)).toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })
})
