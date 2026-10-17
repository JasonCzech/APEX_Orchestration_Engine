import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'
import type { components } from '@apex/api-client'
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
const preparation: components['schemas']['PreparationRead'] = {
  id: 'preparation-full-identity', created_at: base.created_at, created_by: 'engineer', checked_at: base.created_at,
  launch_available: false, checks: [], archived_at: null, archived_by: null,
  snapshot: {
    plan_version_id: 'plan', plan_title: 'Checkout plan', plan_checksum: 'plan-checksum',
    script_version_id: 'script', script_title: 'Checkout script', script_checksum: 'script-checksum',
    script_source_checksum: 'source-checksum', script_format: 'apex_native_json',
    readiness_version_id: null, readiness_checksum: null, connection_id: null, connection_version: null,
    environment_id: 'env1', environment_version: 2, engine: 'apex-load', mode: 'simulation',
    launch_authority: 'pipeline', execution_policy: 'sequential', workload: { vusers: 10, ramp_s: 1, duration_s: 60 },
  },
}
function mount(role: 'admin' | 'viewer' = 'admin', preparations: () => components['schemas']['PreparationRead'][] = () => [], revision: () => number = () => 3) {
  server.use(
    http.get('*/v1/cycles/c1', () => HttpResponse.json(cycle)),
    http.get('*/v1/cycles/c1/executions', () =>
      HttpResponse.json({
        revision: revision(),
        preparations: preparations(),
        connections: [],
        linked_runs: [run],
        recent_runs: [run],
        target_version: 2,
        inventory_snapshot_id: 'scan',
        inventory_scanned_at: '2026-09-07T12:00:00Z',
      }),
    ),
    http.get('*/v1/cycles/c1/executions/:id/script-validations', () => HttpResponse.json({ revision: revision(), blockers: [], validations: [] })),
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

describe('Archiving execution preparations', () => {
  it('confirms exact identity and revision, then retains archived audit details', async () => {
    const user = userEvent.setup()
    let items = [preparation]
    let revision = 3
    const requests: unknown[] = []
    server.use(http.post('*/v1/cycles/c1/executions/preparation-full-identity/archive', async ({ request }) => {
      requests.push(await request.json())
      items = [{ ...preparation, archived_at: '2026-10-17T12:00:00Z', archived_by: 'Dash Ops' }]
      revision = 4
      return HttpResponse.json({ id: preparation.id, revision })
    }))
    mount('admin', () => items, () => revision)
    await user.click(await screen.findByRole('button', { name: 'Archive preparation' }))
    let dialog = screen.getByRole('dialog', { name: 'Archive execution preparation' })
    expect(dialog).toHaveTextContent(preparation.id)
    expect(dialog).toHaveTextContent('cycle revision 3')
    expect(dialog).toHaveTextContent('Already-started provider work and linked runs are not cancelled')
    expect(requests).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(requests).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: 'Archive preparation' }))
    dialog = screen.getByRole('dialog', { name: 'Archive execution preparation' })
    await user.click(within(dialog).getByRole('button', { name: 'Archive preparation' }))
    await screen.findByText('Preparation archived: Checkout script.')
    expect(requests).toEqual([{ expected_revision: 3 }])
    await screen.findByText(/No active execution preparations/)
    await user.click(screen.getByRole('checkbox', { name: 'Show archived preparations (1)' }))
    expect(screen.getByRole('heading', { name: 'Checkout script · apex-load' })).toBeInTheDocument()
    expect(screen.getByText(/Bindings and validation history are retained/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Archive preparation' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Validate with APEX Load' })).not.toBeInTheDocument()
  })

  it('does not silently rebase a confirmation after a conflicting revision', async () => {
    const user = userEvent.setup()
    let revision = 3
    const requests: unknown[] = []
    server.use(http.post('*/v1/cycles/c1/executions/preparation-full-identity/archive', async ({ request }) => {
      requests.push(await request.json())
      revision = 4
      return HttpResponse.json({ detail: 'Cycle changed concurrently' }, { status: 409 })
    }))
    mount('admin', () => [preparation], () => revision)
    await user.click(await screen.findByRole('button', { name: 'Archive preparation' }))
    const dialog = screen.getByRole('dialog', { name: 'Archive execution preparation' })
    await user.click(within(dialog).getByRole('button', { name: 'Archive preparation' }))
    await within(dialog).findByRole('alert')
    expect(dialog).toHaveTextContent('Cycle changed concurrently')
    expect(dialog).toHaveTextContent('cycle revision 3')
    expect(within(dialog).getByRole('button', { name: 'Archive preparation' })).toBeDisabled()
    expect(requests).toEqual([{ expected_revision: 3 }])
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Archive preparation' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('cycle revision 4')
  })

  it('lets viewers inspect preparations without archive or validation actions', async () => {
    mount('viewer', () => [preparation])
    await screen.findByRole('heading', { name: 'Checkout script · apex-load' })
    expect(screen.queryByRole('button', { name: 'Archive preparation' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Validate with APEX Load' })).not.toBeInTheDocument()
  })
})
