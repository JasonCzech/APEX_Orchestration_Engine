import { act, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { ApexLoadWorkspacePage } from './ApexLoadWorkspacePage'

function renderWorkspace(entry = '/apex-load') {
  const router = createMemoryRouter([{ path: '/apex-load', element: <ApexLoadWorkspacePage /> }], { initialEntries: [entry] })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { ...view, router }
}

describe('ApexLoadWorkspacePage', () => {
  it('clears scenario context atomically when navigating to another connection', async () => {
    const scheduleReads: string[] = []
    server.use(
      http.get('*/v1/apex-load/connections', () => HttpResponse.json({ items: [
        { id: 'a', name: 'A', project_id: 'a' }, { id: 'b', name: 'B', project_id: 'b' },
      ] })),
      http.get('*/v1/apex-load/connections/:connectionId/scenarios/:scenarioId/schedules', ({ params }) => {
        scheduleReads.push(`${params.connectionId}/${params.scenarioId}`)
        return HttpResponse.json({ items: [{ id: `schedule-${params.connectionId}`, name: `Schedule ${params.connectionId}` }] })
      }),
      http.get('*/v1/apex-load/connections/:connectionId/scenarios/:scenarioId/schedules/:scheduleId/occurrences', () => HttpResponse.json({ items: [] })),
      http.get('*/v1/apex-load/connections/:connectionId/:resource', ({ params }) => HttpResponse.json({
        items: params.resource === 'scenarios' ? [{ id: `scenario-${params.connectionId}`, name: `Scenario ${params.connectionId}` }] : [],
        details: {},
      })),
    )
    const { router } = renderWorkspace('/apex-load?connection=a')
    const executions = await screen.findByRole('tab', { name: 'Executions & analysis' })
    for (const tab of screen.getAllByRole('tab')) expect(document.getElementById(tab.getAttribute('aria-controls')!)).toHaveAttribute('role', 'tabpanel')
    executions.focus()
    await userEvent.setup().keyboard('{End}')
    const authoring = screen.getByRole('tab', { name: 'Scenarios & schedules' })
    expect(authoring).toHaveFocus()
    expect(screen.getByRole('tabpanel', { name: 'Scenarios & schedules' })).toHaveAttribute('id', authoring.getAttribute('aria-controls'))
    await screen.findByText('Schedule a')
    await act(() => router.navigate('/apex-load?connection=b'))
    await screen.findByText('Schedule b')
    expect(scheduleReads).toEqual(['a/scenario-a', 'b/scenario-b'])
  })

  it('does not silently switch to another connection when the requested context is unavailable', async () => {
    server.use(
      http.get('*/v1/apex-load/connections', () =>
        HttpResponse.json({
          items: [{ id: 'other', name: 'Other project', project_id: 'other' }],
          truncated: false,
        }),
      ),
    )
    renderWorkspace('/apex-load?connection=missing&project=original&run=run-1')
    expect(await screen.findByRole('combobox', { name: 'Connection' })).toHaveValue('')
    expect(screen.getByText(/requested context is unavailable or ambiguous/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create scenario' })).not.toBeInTheDocument()
  })

  it('provides recovery when there is no enabled project connection', async () => {
    server.use(
      http.get('*/v1/apex-load/connections', () =>
        HttpResponse.json({ items: [], truncated: false }),
      ),
    )
    renderWorkspace()
    expect(await screen.findByRole('heading', { name: 'Connect APEX Load' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh connections' })).toBeEnabled()
    expect(screen.getByRole('link', { name: 'Manage connections' })).toHaveAttribute(
      'href',
      '/admin/connections',
    )
  })

  it('shows project-bound native controls, metrics, schedules, and a safe console link', async () => {
    const consoleUrl = 'https://apex-load.example/#/test/native-run-1'
    let scenarioIdempotencyKey: string | null = null
    server.use(
      http.get('*/v1/apex-load/connections', () =>
        HttpResponse.json({
          items: [{ id: 'connection-1', name: 'Load control', project_id: 'project-1' }],
        }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/capabilities', () =>
        HttpResponse.json({
          templates: [{ id: 'checkout-template', name: 'Checkout load model' }],
        }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/scenarios', () =>
        HttpResponse.json({ items: [{ id: 'scenario-1', name: 'Checkout', status: 'ready' }] }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/runs', () =>
        HttpResponse.json({
          items: [
            {
              id: 'native-run-1',
              name: 'Checkout baseline',
              status: 'RUNNING',
              console_url: consoleUrl,
            },
          ],
        }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/mirrored-runs', () =>
        HttpResponse.json({ items: [{
          id: 'native-run-1',
          pipeline_status: 'completed',
          pipeline_thread_id: 'analysis-run-1',
        }] }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/schedule-health', () =>
        HttpResponse.json({ details: { status: 'healthy' } }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/calendar', () =>
        HttpResponse.json({ details: { windows: [] } }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/scenarios/:scenarioId/schedules', () =>
        HttpResponse.json({ items: [{ id: 'schedule-1', name: 'Nightly', status: 'enabled' }] }),
      ),
      http.get(
        '*/v1/apex-load/connections/:connectionId/scenarios/:scenarioId/schedules/:scheduleId/occurrences',
        () => HttpResponse.json({ items: [] }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/runs/:runId/metrics', () =>
        HttpResponse.json({ details: { tps_avg: 42.5, p95_ms: 212 } }),
      ),
      http.post('*/v1/apex-load/connections/:connectionId/scenarios/validation', () =>
        HttpResponse.json({ details: { valid: true, warnings: [] } }),
      ),
      http.post('*/v1/apex-load/connections/:connectionId/scenarios/sizing', () =>
        HttpResponse.json({ details: { recommended_generators: 3 } }),
      ),
      http.post('*/v1/apex-load/connections/:connectionId/scenarios', ({ request }) => {
        scenarioIdempotencyKey = request.headers.get('Idempotency-Key')
        return HttpResponse.json({
          id: 'scenario-created',
          name: 'New scenario',
          details: { id: 'scenario-created', name: 'New scenario' },
        })
      }),
    )
    const user = userEvent.setup()
    renderWorkspace()

    await user.click(await screen.findByRole('tab', { name: 'Scenarios & schedules' }))
    expect(
      await screen.findByText(
        (_, element) =>
          element?.tagName === 'PRE' && element.textContent?.includes('Checkout load model'),
      ),
    ).toBeInTheDocument()
    expect(await screen.findByText('Nightly')).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Executions & analysis' }))
    expect(screen.getByRole('link', { name: 'Open in APEX Load' })).toHaveAttribute(
      'href',
      expect.stringContaining('orchestrationReturn='),
    )
    expect(await screen.findByRole('link', { name: 'Open analysis' })).toHaveAttribute(
      'href',
      '/runs/analysis-run-1',
    )

    await user.click(screen.getByRole('button', { name: 'Live metrics' }))
    expect(
      await screen.findByText(
        (_, element) =>
          element?.tagName === 'PRE' && element.textContent?.includes('"tps_avg": 42.5'),
      ),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Scenarios & schedules' }))
    await user.click(screen.getByRole('button', { name: 'Validate scenario' }))
    expect(
      await screen.findByText(
        (_, element) => element?.tagName === 'PRE' && element.textContent?.includes('valid'),
      ),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Estimate capacity' }))
    expect(
      await screen.findByText(
        (_, element) =>
          element?.tagName === 'PRE' && element.textContent?.includes('recommended_generators'),
      ),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Create scenario' }))
    await waitFor(() => expect(scenarioIdempotencyKey).toMatch(/^[A-Za-z0-9-]+$/))
  })

  it('does not publish a completed mutation into a newly selected connection', async () => {
    let releaseRequest: () => void = () => {}
    const requestGate = new Promise<void>((resolve) => {
      releaseRequest = resolve
    })
    let requestedConnection: string | null = null
    let requestCompleted = false
    server.use(
      http.get('*/v1/apex-load/connections', () =>
        HttpResponse.json({
          items: [
            { id: 'connection-1', name: 'First', project_id: 'project-1' },
            { id: 'connection-2', name: 'Second', project_id: 'project-2' },
          ],
          truncated: false,
        }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/capabilities', () =>
        HttpResponse.json({ load_models: {}, templates: {}, console_url: 'https://apex.test' }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/scenarios', () =>
        HttpResponse.json({ items: [], count: 0, truncated: false }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/runs', () =>
        HttpResponse.json({ items: [], count: 0, truncated: false }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/mirrored-runs', () =>
        HttpResponse.json({ items: [], limit: 25, offset: 0 }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/schedule-health', () =>
        HttpResponse.json({ details: {} }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/calendar', () =>
        HttpResponse.json({ details: {} }),
      ),
      http.post('*/v1/apex-load/connections/:connectionId/scenarios', async ({ params }) => {
        requestedConnection = String(params['connectionId'])
        await requestGate
        requestCompleted = true
        return HttpResponse.json({
          id: 'stale-created-scenario',
          details: { id: 'stale-created-scenario' },
        })
      }),
    )
    const user = userEvent.setup()
    renderWorkspace()

    const connectionSelect = await screen.findByRole('combobox', { name: 'Connection' })
    expect(connectionSelect).toHaveValue('')
    await user.selectOptions(connectionSelect, 'connection-1')
    await user.click(await screen.findByRole('tab', { name: 'Scenarios & schedules' }))
    await user.click(screen.getByRole('button', { name: 'Create scenario' }))
    await waitFor(() => expect(requestedConnection).toBe('connection-1'))

    await user.selectOptions(connectionSelect, 'connection-2')
    releaseRequest()

    await waitFor(() => expect(requestCompleted).toBe(true))
    expect(
      screen.queryByText((content) => content.includes('stale-created-scenario')),
    ).not.toBeInTheDocument()
  })
})
