import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '@/test/server'
import { authenticatedState, renderApp } from '@/test/render'
import type { Cycle } from '../api'
import { ReportEvidence } from '../ReportEvidence'
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
      kind: 'report',
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

function mount(canEdit = true) {
  render(
    <MemoryRouter>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ReportEvidence
          cycle={cycle}
          version={cycle.versions[0]!}
          canEdit={canEdit}
        />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}
describe('Report evidence capture', () => {
  it('submits exact preview fingerprints and links the new draft', async () => {
    const user = userEvent.setup()
    let submitted: unknown
    server.use(
      http.get('*/v1/cycles/cycle/results', () =>
        HttpResponse.json({
          results: [{ id: 'run', evidence_checksum: 'a'.repeat(64) }],
        }),
      ),
      http.post(
        '*/v1/cycles/cycle/reports/script/evidence',
        async ({ request }) => {
          submitted = await request.json()
          return HttpResponse.json({
            ...cycle,
            versions: [
              ...cycle.versions,
              { ...cycle.versions[0]!, id: 'captured', replaces: 'script' },
            ],
          })
        },
      ),
    )
    mount()
    const button = await screen.findByRole('button', {
      name: 'Capture evidence as new report draft',
    })
    await waitFor(() => expect(button).toBeEnabled())
    await user.click(button)
    expect(
      await screen.findByRole('link', { name: 'Open captured draft' }),
    ).toHaveAttribute('href', '/?tab=report&version=captured')
    expect(submitted).toMatchObject({
      expected_revision: 4,
      expected_results: { run: 'a'.repeat(64) },
    })
  })
  it('shows frozen missing outcomes and measured zeros without viewer mutation', async () => {
    const original = cycle.versions[0]!.content
    cycle.versions[0]!.content = {
      ...original,
      _execution_evidence: {
        captured_at: '2026-09-07',
        captured_by: 'engineer',
        results: [
          {
            id: 'run',
            engine: 'apex-load',
            attempt: 1,
            execution_status: 'completed',
            test_outcome: null,
            metrics: [
              { key: 'tps_avg', label: 'Throughput', unit: 'tps', value: 0 },
            ],
            sla_breaches: [],
          },
        ],
      },
    }
    server.use(
      http.get('*/v1/cycles/cycle/results', () =>
        HttpResponse.json({ results: [] }),
      ),
    )
    mount(false)
    expect(screen.getByText(/test outcome Not available/)).toBeInTheDocument()
    expect(screen.getByRole('definition')).toHaveTextContent('0')
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    cycle.versions[0]!.content = original
  })
})

it('keeps one evidence panel when the full report page refreshes', async () => {
  const user = userEvent.setup()
  server.use(
    http.get('*/v1/cycles/cycle', () => HttpResponse.json(cycle)),
    http.get('*/v1/catalog/applications', () => HttpResponse.json([])),
    http.get('*/v1/catalog/environments', () => HttpResponse.json([])),
    http.get('*/v1/cycles/cycle/results', () =>
      HttpResponse.json({
        results: [],
        read_at: '2026-09-07',
        baseline_id: null,
      }),
    ),
  )
  const state = authenticatedState('admin')
  if (state.status === 'authenticated')
    state.systemInfo.features = { cycles: true }
  renderApp({ initialEntries: ['/cycles/cycle?tab=report'], authState: state })
  await screen.findByRole('region', { name: 'Frozen report evidence' })
  await user.click(screen.getByRole('button', { name: 'Refresh cycle' }))
  await user.click(screen.getByRole('button', { name: 'Refresh results' }))
  expect(
    screen.getAllByRole('region', { name: 'Frozen report evidence' }),
  ).toHaveLength(1)
})
