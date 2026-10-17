import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'
import type { Cycle } from '../api'
const cycle: Cycle = {
  followups: [],
  id: 'cycle',
  name: 'Checkout',
  project_id: 'p',
  app_id: 'a',
  environment_id: 'e',
  mode: 'live',
  owner: 'engineer',
  revision: 4,
  independent_review: false,
  created_at: '2026-09-07T12:00:00Z',
  updated_at: '2026-09-07T12:00:00Z',
  blockers: [],
  fields: {
    script: [
      {
        key: 'source',
        help: '',
        options: [],
        label: 'Script source',
        integer: false,
        control: 'multiline',
        required: true,
      },
    ],
  },
  versions: [
    {
      id: 'script',
      title: 'Checkout source',
      kind: 'script',
      content: {
        source: '{}',
        format: 'apex_native_json',
        engine: 'apex-load',
      },
      checksum: 'approved',
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
    },
  ],
}

function mount(role: 'admin' | 'viewer' = 'admin') {
  server.use(
    http.get('*/v1/cycles/cycle', () => HttpResponse.json(cycle)),
    http.get('*/v1/catalog/applications', () => HttpResponse.json([])),
    http.get('*/v1/catalog/environments', () => HttpResponse.json([])),
  )
  const state = authenticatedState(role, 'Dash Ops', [])
  if (state.status === 'authenticated')
    state.systemInfo.features = { cycles: true }
  renderApp({
    initialEntries: ['/cycles/cycle?tab=postmortem'],
    authState: state,
  })
}
describe('Cycle follow-up', () => {
  it('retains action entries on conflict and submits the same operation when retried', async () => {
    const user = userEvent.setup()
    const requests: unknown[] = []
    server.use(
      http.post('*/v1/cycles/cycle/followups', async ({ request }) => {
        requests.push(await request.json())
        return HttpResponse.json({ detail: 'Cycle changed' }, { status: 409 })
      }),
    )
    mount()
    await user.type(
      await screen.findByLabelText('Action title'),
      'Fix data setup',
    )
    await user.type(screen.getByLabelText('Action owner'), 'Performance team')
    expect(
      screen.getByRole('button', { name: 'Create next cycle' }),
    ).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Save action' }))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('Action title')).toHaveValue('Fix data setup')
    await user.click(screen.getByRole('button', { name: 'Save action' }))
    await waitFor(() => expect(requests).toHaveLength(2))
    expect(requests[1]).toEqual(requests[0])
  })
  it('creates a next cycle with explicit selected drafts and links the result', async () => {
    const user = userEvent.setup()
    let requestBody: unknown
    server.use(
      http.post('*/v1/cycles/cycle/next-cycle', async ({ request }) => {
        requestBody = await request.json()
        return HttpResponse.json({
          ...cycle,
          id: 'child',
          name: 'Next checkout',
        })
      }),
    )
    mount()
    await user.type(
      await screen.findByLabelText('Next cycle name'),
      'Next checkout',
    )
    await user.click(screen.getByRole('button', { name: 'Create next cycle' }))
    expect(
      await screen.findByRole('link', { name: 'Open Next checkout' }),
    ).toHaveAttribute('href', '/cycles/child')
    expect(requestBody).toMatchObject({
      name: 'Next checkout',
      version_ids: [],
      carry_unresolved_actions: true,
      expected_revision: 4,
    })
  })
  it('does not expose mutation forms to viewers', async () => {
    mount('viewer')
    await screen.findByRole('heading', { name: 'Follow-up actions' })
    expect(
      screen.queryByRole('button', { name: 'Save action' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Next cycle name')).not.toBeInTheDocument()
  })
})

it('keeps the edit revision after a refresh reveals a concurrent action update', async () => {
  const user = userEvent.setup()
  mount()
  let latest = {
    ...cycle,
    followups: [
      {
        id: 'action',
        title: 'Fix data',
        owner: 'Alice',
        status: 'open',
        note: '',
        history: [],
      },
    ],
  }
  let submitted: unknown
  server.use(
    http.get('*/v1/cycles/cycle', () => HttpResponse.json(latest)),
    http.post('*/v1/cycles/cycle/followups', async ({ request }) => {
      submitted = await request.json()
      return HttpResponse.json({ detail: 'Cycle changed' }, { status: 409 })
    }),
  )
  await user.click(await screen.findByRole('button', { name: 'Edit action' }))
  await user.type(
    screen.getByLabelText('Progress and remediation notes'),
    'My edits',
  )
  latest = {
    ...latest,
    revision: 5,
    followups: [{ ...latest.followups[0]!, owner: 'Bob' }],
  }
  await user.click(screen.getByRole('button', { name: 'Refresh cycle' }))
  await screen.findByText(/Owner: Bob/)
  await user.click(screen.getByRole('button', { name: 'Save action' }))
  await screen.findByRole('alert')
  expect(submitted).toMatchObject({
    expected_revision: 4,
    owner: 'Alice',
    note: 'My edits',
  })
  expect(screen.getByLabelText('Progress and remediation notes')).toHaveValue(
    'My edits',
  )
  await user.click(screen.getByRole('button', { name: 'Clear action form' }))
  await user.click(screen.getByRole('button', { name: 'Edit action' }))
  await user.click(screen.getByRole('button', { name: 'Save action' }))
  await waitFor(() =>
    expect(submitted).toMatchObject({ expected_revision: 5, owner: 'Bob' }),
  )
})
