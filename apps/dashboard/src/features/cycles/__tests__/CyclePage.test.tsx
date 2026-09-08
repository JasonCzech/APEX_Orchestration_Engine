import { screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'
import type { Cycle } from '../api'

const cycle: Cycle = {
  followups: [],
  id: 'c1',
  name: 'Checkout baseline',
  project_id: 'p1',
  app_id: 'app1',
  environment_id: 'env1',
  owner: 'engineer',
  mode: 'simulation',
  revision: 3,
  independent_review: true,
  created_at: '2026-09-07T12:00:00Z',
  updated_at: '2026-09-07T12:00:00Z',
  blockers: ['Approve an active test plan version'],
  fields: {
    test_plan: [
      {
        key: 'objectives',
        label: 'Objectives',
        help: '',
        options: [],
        control: 'multiline',
        required: true,
      },
      {
        key: 'vusers',
        label: 'Concurrent users',
        help: '',
        options: [],
        control: 'number',
        minimum: 1,
        maximum: 1000,
        required: true,
      },
    ],
  },
  versions: [
    {
      id: 'v1',
      kind: 'test_plan',
      title: 'Checkout plan',
      content: { objectives: 'Measure checkout', vusers: 10 },
      checksum: 'abc123',
      dependencies: [],
      change_summary: 'Initial scope',
      status: 'in_review',
      created_by: 'engineer',
      created_at: '2026-09-07T12:00:00Z',
      archived: false,
      reviews: [],
      approval_current: false,
      validation_issues: [],
      available_actions: ['approve', 'request_revision', 'archive'],
    },
  ],
}
function mount(data = cycle, role: 'admin' | 'viewer' = 'admin') {
  server.use(
    http.get('*/v1/cycles/c1', () => HttpResponse.json(data)),
    http.get('*/v1/catalog/applications', () =>
      HttpResponse.json([
        { id: 'app1', name: 'Checkout API', project_id: 'p1' },
      ]),
    ),
    http.get('*/v1/catalog/environments', () =>
      HttpResponse.json([
        { id: 'env1', name: 'Performance staging', application_id: 'app1' },
      ]),
    ),
  )
  const state = authenticatedState(role)
  if (state.status === 'authenticated')
    state.systemInfo.features = { cycles: true }
  return renderApp({
    initialEntries: ['/cycles/c1?tab=test_plan'],
    authState: state,
  })
}

describe('Cycle workspace', () => {
  it('shows target names, simulation and exact-version approval', async () => {
    const user = userEvent.setup()
    let submitted: unknown
    server.use(
      http.post('*/v1/cycles/c1/actions', async ({ request }) => {
        submitted = await request.json()
        return HttpResponse.json({
          ...cycle,
          revision: 4,
          versions: [
            {
              ...cycle.versions[0],
              status: 'approved',
              approval_current: true,
              available_actions: ['archive'],
            },
          ],
        })
      }),
    )
    mount()
    expect(
      await screen.findByText(/Checkout API · Performance staging/),
    ).toBeInTheDocument()
    expect(
      screen.getByText('Simulation — synthetic evidence'),
    ).toBeInTheDocument()
    await user.click(screen.getByText('Version identity and review history'))
    expect(screen.getByText('Content SHA-256: abc123')).toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: 'Approve this version' }),
    )
    await waitFor(() =>
      expect(submitted).toEqual({
        action: 'approve',
        version_id: 'v1',
        expected_revision: 3,
        note: '',
      }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Review action recorded',
    )
  })
  it('retains edited content after a stale-revision rejection', async () => {
    const user = userEvent.setup()
    let submitted: unknown
    server.use(
      http.post('*/v1/cycles/c1/versions', async ({ request }) => {
        submitted = await request.json()
        return HttpResponse.json(
          { detail: 'Cycle changed; refresh before editing' },
          { status: 409 },
        )
      }),
    )
    mount()
    await user.click(
      await screen.findByRole('button', { name: 'Revise as new draft' }),
    )
    await user.clear(screen.getByLabelText('Objectives *'))
    await user.type(
      screen.getByLabelText('Objectives *'),
      'Investigate the checkout regression',
    )
    await user.type(
      screen.getByLabelText('Change summary and affected requirements'),
      'Changed requirement PERF-42',
    )
    await user.click(screen.getByRole('button', { name: 'Save draft version' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Cycle changed')
    expect(screen.getByLabelText('Objectives *')).toHaveValue(
      'Investigate the checkout regression',
    )
    expect(submitted).toMatchObject({
      expected_revision: 3,
      replaces: 'v1',
      content: { vusers: 10 },
    })
  })
  it('requires an explicit choice before discarding unsaved edits through navigation', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(
      await screen.findByRole('button', { name: 'Revise as new draft' }),
    )
    await user.type(
      screen.getByLabelText('Change summary and affected requirements'),
      'Unsaved note',
    )
    await user.click(screen.getByRole('link', { name: 'All cycles' }))
    const dialog = await screen.findByRole('dialog', {
      name: 'Unsaved deliverable changes',
    })
    await user.click(
      within(dialog).getByRole('button', { name: 'Keep editing' }),
    )
    expect(
      screen.getByLabelText('Change summary and affected requirements'),
    ).toHaveValue('Unsaved note')
  })
  it('does not offer approval without a server-authorized action', async () => {
    mount(
      {
        ...cycle,
        versions: [{ ...cycle.versions[0]!, available_actions: [] }],
      },
      'viewer',
    )
    await screen.findByText('Checkout plan')
    expect(
      screen.queryByRole('button', { name: 'Approve this version' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Revise as new draft' }),
    ).not.toBeInTheDocument()
  })
})

it('saves a revised version and exits editing without a leave warning', async () => {
  const user = userEvent.setup()
  server.use(
    http.post('*/v1/cycles/c1/versions', async ({ request }) => {
      const body = (await request.json()) as {
        content: Record<string, unknown>
      }
      return HttpResponse.json({
        ...cycle,
        revision: 4,
        versions: [
          ...cycle.versions,
          {
            ...cycle.versions[0],
            id: 'v2',
            content: body.content,
            status: 'draft',
            available_actions: ['submit'],
            replaces: 'v1',
          },
        ],
      })
    }),
  )
  mount()
  await user.click(
    await screen.findByRole('button', { name: 'Revise as new draft' }),
  )
  await user.type(
    screen.getByLabelText('Change summary and affected requirements'),
    'Update PERF-42',
  )
  await user.click(screen.getByRole('button', { name: 'Save draft version' }))
  await waitFor(() =>
    expect(
      screen.queryByRole('form', { name: 'Deliverable editor' }),
    ).not.toBeInTheDocument(),
  )
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Saved draft version v2')
})

it.each(['scope', 'report'] as const)(
  'preserves frozen %s evidence when removing unsupported fields',
  async (kind) => {
    const user = userEvent.setup()
    const key = kind === 'scope' ? '_source_evidence' : '_execution_evidence'
    const evidence =
      kind === 'scope'
        ? []
        : { results: [], captured_at: '2026-09-07', captured_by: 'engineer' }
    const data = {
      ...cycle,
      fields: { [kind]: cycle.fields!.test_plan! },
      versions: [
        {
          ...cycle.versions[0]!,
          kind,
          content: {
            objectives: 'Measure checkout',
            [key]: evidence,
            obsolete: 'remove me',
          },
        },
      ],
    }
    let submitted: unknown
    server.use(
      http.get('*/v1/cycles/c1/results', () =>
        HttpResponse.json({ results: [], revision: 3, baseline_id: null }),
      ),
      http.post('*/v1/cycles/c1/versions', async ({ request }) => {
        submitted = await request.json()
        return HttpResponse.json(data)
      }),
    )
    mount(data)
    await user.click(
      await screen.findByRole('button', {
        name: kind === 'scope' ? 'Scope' : 'Results & Reports',
      }),
    )
    await user.click(
      screen.getByRole('button', { name: 'Revise as new draft' }),
    )
    await user.click(
      screen.getByText('Unsupported fields from the previous version'),
    )
    await user.click(
      screen.getByRole('button', {
        name: 'Remove unsupported fields from this draft',
      }),
    )
    expect(
      screen.queryByText('Unsupported fields from the previous version'),
    ).not.toBeInTheDocument()
    await user.type(
      screen.getByLabelText('Change summary and affected requirements'),
      'Remove obsolete field',
    )
    await user.click(screen.getByRole('button', { name: 'Save draft version' }))
    await waitFor(() =>
      expect(submitted).toMatchObject({
        content: { objectives: 'Measure checkout', [key]: evidence },
      }),
    )
    expect((submitted as { content: object }).content).not.toHaveProperty(
      'obsolete',
    )
  },
)
