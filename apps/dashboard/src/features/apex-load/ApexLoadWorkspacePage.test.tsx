import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { ApexLoadWorkspacePage } from './ApexLoadWorkspacePage'

function renderWorkspace() {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <ApexLoadWorkspacePage />
    </QueryClientProvider>,
  )
}

describe('ApexLoadWorkspacePage', () => {
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
        HttpResponse.json({ templates: [{ id: 'checkout-template', name: 'Checkout load model' }] }),
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
        HttpResponse.json({ items: [] }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/schedule-health', () =>
        HttpResponse.json({ details: { status: 'healthy' } }),
      ),
      http.get('*/v1/apex-load/connections/:connectionId/calendar', () =>
        HttpResponse.json({ details: { windows: [] } }),
      ),
      http.get(
        '*/v1/apex-load/connections/:connectionId/scenarios/:scenarioId/schedules',
        () => HttpResponse.json({ items: [{ id: 'schedule-1', name: 'Nightly', status: 'enabled' }] }),
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

    expect(
      await screen.findByText(
        (_, element) => element?.tagName === 'PRE' && element.textContent?.includes('Checkout load model'),
      ),
    ).toBeInTheDocument()
    expect(await screen.findByText('Nightly')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open in APEX Load' })).toHaveAttribute(
      'href',
      consoleUrl,
    )

    await user.click(screen.getByRole('button', { name: 'Live metrics' }))
    expect(
      await screen.findByText(
        (_, element) => element?.tagName === 'PRE' && element.textContent?.includes('"tps_avg": 42.5'),
      ),
    ).toBeInTheDocument()

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
      http.post(
        '*/v1/apex-load/connections/:connectionId/scenarios',
        async ({ params }) => {
          requestedConnection = String(params['connectionId'])
          await requestGate
          requestCompleted = true
          return HttpResponse.json({
            id: 'stale-created-scenario',
            details: { id: 'stale-created-scenario' },
          })
        },
      ),
    )
    const user = userEvent.setup()
    renderWorkspace()

    const connectionSelect = await screen.findByRole('combobox', { name: 'Connection' })
    await waitFor(() => expect(connectionSelect).toHaveValue('connection-1'))
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
