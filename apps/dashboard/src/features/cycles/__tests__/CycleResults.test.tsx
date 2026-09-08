import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '@/test/server'
import { CycleResults } from '../CycleResults'

const attempt = {
  id: 'run',
  thread_id: 'thread',
  engine: 'apex-load',
  attempt: 1,
  execution_status: 'completed',
  test_outcome: null,
  evidence_status: 'partial',
  started_at: '2026-09-07T12:00:00Z',
  ended_at: null,
  sla_breaches: [],
  warnings: [],
  bindings_verified: false,
  comparison_available: false,
  metrics: [
    {
      key: 'p95_ms',
      label: 'Response time',
      unit: 'ms',
      value: null,
      preference: 'lower',
      difference: null,
      percent_difference: null,
      baseline_value: null,
    },
  ],
}
function mount() {
  render(
    <MemoryRouter>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <CycleResults cycleId="cycle" />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}
describe('Cycle results', () => {
  it('separates missing outcomes and metrics from completed execution', async () => {
    server.use(
      http.get('*/v1/cycles/cycle/results', () =>
        HttpResponse.json({
          read_at: '2026-09-07T12:00:00Z',
          results: [attempt],
          baseline_id: null,
        }),
      ),
    )
    mount()
    const card = await screen.findByRole('article')
    expect(card).toHaveTextContent(
      'Execution status: completed · Test outcome: Not available',
    )
    expect(within(card).getByRole('definition')).toHaveTextContent(
      'Not available',
    )
    expect(within(card).getByRole('link')).toHaveAttribute(
      'href',
      '/runs/thread/phases/execution',
    )
    expect(screen.queryByText('Passed')).not.toBeInTheDocument()
  })
  it('retains selected baseline and explains equal versus missing differences without color', async () => {
    const user = userEvent.setup()
    server.use(
      http.get('*/v1/cycles/cycle/results', ({ request }) => {
        const baseline = new URL(request.url).searchParams.get('baseline_id')
        return HttpResponse.json({
          read_at: '2026-09-07T12:00:00Z',
          baseline_id: baseline,
          results: [
            { ...attempt, id: 'baseline' },
            {
              ...attempt,
              attempt: 2,
              metrics: [
                {
                  ...attempt.metrics[0],
                  value: 0,
                  difference: baseline ? 0 : null,
                  percent_difference: null,
                },
              ],
            },
          ],
        })
      }),
    )
    mount()
    await user.selectOptions(
      await screen.findByLabelText('Comparison baseline'),
      'baseline',
    )
    expect(await screen.findByText(/Equal to baseline/)).toHaveTextContent(
      'Percentage not available',
    )
    expect(screen.getByLabelText('Comparison baseline')).toHaveValue('baseline')
    expect(
      screen.getByText(/Differences alone do not establish improvement/),
    ).toBeInTheDocument()
  })
})
