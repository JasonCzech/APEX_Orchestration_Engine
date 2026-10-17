import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'
import type { Cycle } from '../api'

const base = {
  checksum: 'approved-checksum',
  dependencies: [],
  change_summary: 'Initial',
  status: 'approved',
  created_by: 'engineer',
  created_at: '2026-09-07T12:00:00Z',
  archived: false,
  reviews: [],
  approval_current: true,
  validation_issues: [],
  available_actions: [],
}
const cycle: Cycle = {
  followups: [],
  id: 'c1',
  name: 'Checkout',
  project_id: 'p1',
  app_id: 'app1',
  environment_id: 'env1',
  owner: 'engineer',
  mode: 'simulation',
  revision: 3,
  independent_review: false,
  created_at: '2026-09-07T12:00:00Z',
  updated_at: '2026-09-07T12:00:00Z',
  blockers: [],
  fields: {},
  versions: [
    {
      ...base,
      id: 'plan',
      title: 'Checkout plan',
      kind: 'test_plan',
      content: { vusers: 10, ramp_s: 1, duration_s: 60 },
    },
    {
      ...base,
      id: 'script',
      title: 'Checkout script',
      kind: 'script',
      dependencies: ['plan'],
      content: { engine: 'apex-load', format: 'apex_native_json' },
    },
    {
      ...base,
      id: 'wrong',
      title: 'Unrelated script',
      kind: 'script',
      dependencies: ['different-plan'],
      content: { engine: 'loadrunner' },
    },
  ],
}
const run = {
  id: 'run',
  thread_id: 'thread',
  engine: 'apex-load',
  attempt: 1,
  status: 'completed',
  started_at: '2026-09-07T12:00:00Z',
  bindings_verified: false,
}
function mount(role: 'admin' | 'viewer' = 'admin') {
  server.use(
    http.get('*/v1/cycles/c1', () => HttpResponse.json(cycle)),
    http.get('*/v1/cycles/c1/executions', () =>
      HttpResponse.json({
        revision: 3,
        preparations: [],
        connections: [],
        linked_runs: [run],
        recent_runs: [run],
        target_version: 2,
        inventory_snapshot_id: 'scan',
        inventory_scanned_at: '2026-09-07T12:00:00Z',
      }),
    ),
    http.get('*/v1/catalog/applications', () => HttpResponse.json([])),
    http.get('*/v1/catalog/environments', () => HttpResponse.json([])),
  )
  const state = authenticatedState(role, 'Dash Ops', [])
  if (state.status === 'authenticated')
    state.systemInfo.features = { cycles: true }
  return renderApp({
    initialEntries: ['/cycles/c1?tab=executions'],
    authState: state,
  })
}

describe('Execution preparation', () => {
  it('binds exact approved versions and preserves idempotency on an uncertain retry', async () => {
    const user = userEvent.setup()
    const requests: Record<string, unknown>[] = []
    server.use(
      http.post('*/v1/cycles/c1/executions', async ({ request }) => {
        requests.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          { detail: 'Temporary failure' },
          { status: 503 },
        )
      }),
    )
    mount()
    await user.selectOptions(
      await screen.findByLabelText('Approved test plan'),
      'plan',
    )
    expect(
      screen.queryByRole('option', { name: /Unrelated script/ }),
    ).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Approved script'), 'script')
    expect(
      screen.queryByLabelText('Execution connection'),
    ).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Prepare execution' }))
    await screen.findByRole('alert')
    await user.click(screen.getByRole('button', { name: 'Prepare execution' }))
    await waitFor(() => expect(requests).toHaveLength(2))
    expect(requests[0]).toMatchObject({
      expected_revision: 3,
      plan_version_id: 'plan',
      script_version_id: 'script',
      connection_id: null,
      readiness_version_id: null,
      launch_authority: 'pipeline',
    })
    expect(requests[0]?.idempotency_key).toMatch(/^[a-f0-9]{32}$/)
    expect(requests[1]?.idempotency_key).toBe(requests[0]?.idempotency_key)
    expect(
      screen.queryByRole('button', { name: /^Launch/ }),
    ).not.toBeInTheDocument()
  })
  it('keeps historical links unverified and gives viewers no mutation controls', async () => {
    mount('viewer')
    const link = await screen.findByRole('link', {
      name: /Title unavailable · apex-load attempt 1/,
    })
    expect(link).toHaveAttribute('href', '/runs/thread/phases/execution')
    expect(link.parentElement).toHaveTextContent('bindings unverified')
    expect(
      screen.queryByRole('button', { name: 'Prepare execution' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Link existing run' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByText(/passed/i)).not.toBeInTheDocument()
  })
})
