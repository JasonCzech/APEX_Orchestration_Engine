import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { AuthProvider, useAuth } from '@/auth/AuthProvider'
import { authenticatedState, createTestQueryClient, renderApp } from '@/test/render'
import { server, SYSTEM_INFO } from '@/test/server'
import { AGENT_ANALYTICS_FIXTURE, EMPTY_AGENT_ANALYTICS_FIXTURE } from './analyticsTestHandlers'

beforeEach(() => {
  server.use(
    http.get('*/v1/analytics/agents/facets', () => HttpResponse.json({ applications: [], users: [], truncated: false })),
    http.get('*/v1/analytics/agents/events', () => HttpResponse.json({ items: [], next_cursor: null, cost_visible: true })),
  )
})

describe('analytics review regressions', () => {
  it('normalizes administrative deep-link filters for personal-only callers', async () => {
    const requests: URLSearchParams[] = []
    server.use(http.get('*/v1/analytics/agents', ({ request }) => {
      requests.push(new URL(request.url).searchParams)
      return HttpResponse.json(EMPTY_AGENT_ANALYTICS_FIXTURE)
    }))
    renderApp({ initialEntries: ['/analytics?view=authorized&consumer_id=another-user&group=user'], authState: authenticatedState('viewer') })
    expect(await screen.findByRole('heading', { name: 'My usage' })).toBeInTheDocument()
    await waitFor(() => expect(requests.length).toBeGreaterThan(0))
    expect(requests.every(p => p.get('view') === 'mine' && !p.has('consumer_id') && p.get('group_by') !== 'user')).toBe(true)
    expect(screen.queryByRole('button', { name: 'User' })).not.toBeInTheDocument()
  })

  it('clears user grouping and pagination when an administrator selects My usage', async () => {
    server.use(http.get('*/v1/analytics/agents', () => HttpResponse.json(EMPTY_AGENT_ANALYTICS_FIXTURE)))
    const { router } = renderApp({ initialEntries: ['/analytics?group=user&offset=40&consumer_id=another-user'], authState: authenticatedState() })
    fireEvent.change(await screen.findByLabelText('Usage view'), { target: { value: 'mine' } })
    await waitFor(() => expect(router.state.location.search).toBe('?view=mine'))
    expect(screen.queryByRole('button', { name: 'User' })).not.toBeInTheDocument()
  })

  it('uses the aggregate time window for event history rather than a moving default', async () => {
    const windows: URLSearchParams[] = []
    server.use(
      http.get('*/v1/analytics/agents', () => HttpResponse.json(EMPTY_AGENT_ANALYTICS_FIXTURE)),
      http.get('*/v1/analytics/agents/events', ({ request }) => { windows.push(new URL(request.url).searchParams); return HttpResponse.json({ items: [], next_cursor: null, cost_visible: true }) }),
    )
    renderApp({ initialEntries: ['/analytics'], authState: authenticatedState() })
    await waitFor(() => expect(windows.some(p => p.get('from') === EMPTY_AGENT_ANALYTICS_FIXTURE.window.from && p.get('to') === EMPTY_AGENT_ANALYTICS_FIXTURE.window.to)).toBe(true))
  })

  it('clears identity-dependent cached usage when only the stable account ID changes', () => {
    const queryClient = createTestQueryClient()
    queryClient.setQueryData(['analytics', 'history'], { items: [{ consumer_id: 'first-account' }] })
    const consumer = { ...SYSTEM_INFO.consumer, id: 'first-account' }
    function Reconcile() {
      const { reconcileSystemInfo } = useAuth()
      return <button onClick={() => reconcileSystemInfo({ ...SYSTEM_INFO, consumer: { ...consumer, id: 'second-account' } })}>Refresh identity</button>
    }
    render(<QueryClientProvider client={queryClient}><AuthProvider staticState={{ status: 'authenticated', consumer, systemInfo: { ...SYSTEM_INFO, consumer } }}><Reconcile /></AuthProvider></QueryClientProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh identity' }))
    expect(queryClient.getQueryData(['analytics', 'history'])).toBeUndefined()
  })
})

it('keeps My usage selected when clearing other filters', async () => {
  server.use(http.get('*/v1/analytics/agents', () => HttpResponse.json(EMPTY_AGENT_ANALYTICS_FIXTURE)))
  const { router } = renderApp({ initialEntries: ['/analytics?view=mine&consumer_id=alice&project=proj-alpha'], authState: authenticatedState() })
  const clear = await screen.findByRole('button', { name: /clear/i })
  fireEvent.click(clear)
  await waitFor(() => expect(router.state.location.search).toBe('?view=mine'))
})


it('displays model labels while drilling into the exact registry identity', async () => {
  const modelId = 'a'.repeat(32)
  const row = { ...AGENT_ANALYTICS_FIXTURE.breakdown[0]!, key: modelId, model_id: modelId, model: 'shared-model', provider: 'provider-one', label: 'provider-one / shared-model', archived: true }
  const requests: URLSearchParams[] = []
  server.use(http.get('*/v1/analytics/agents', ({ request }) => {
    requests.push(new URL(request.url).searchParams)
    return HttpResponse.json({ ...AGENT_ANALYTICS_FIXTURE, breakdown: [row], series: [] })
  }))
  const { router } = renderApp({ initialEntries: ['/analytics'], authState: authenticatedState() })
  const table = await screen.findByTestId('analytics-breakdown-table')
  fireEvent.click(within(table).getByRole('button', { name: 'provider-one / shared-model (Archived)' }))
  await waitFor(() => expect(new URLSearchParams(router.state.location.search).get('model_id')).toBe(modelId))
  await waitFor(() => expect(requests.some(p => p.get('model_id') === modelId)).toBe(true))
  expect(within(screen.getByRole('group', { name: 'Models filter' })).getByRole('button', { name: 'shared-model' })).toBeInTheDocument()
})

it('retains an explicit unknown-provider filter when drilling into legacy usage', async () => {
  const row = { ...AGENT_ANALYTICS_FIXTURE.breakdown[0]!, key: 'legacy:0:12:shared-model', model_id: null, model: 'shared-model', provider: null, label: 'shared-model' }
  const requests: URLSearchParams[] = []
  server.use(http.get('*/v1/analytics/agents', ({ request }) => {
    requests.push(new URL(request.url).searchParams)
    return HttpResponse.json({ ...AGENT_ANALYTICS_FIXTURE, breakdown: [row], series: [] })
  }))
  const { router } = renderApp({ initialEntries: ['/analytics'], authState: authenticatedState() })
  const table = await screen.findByTestId('analytics-breakdown-table')
  fireEvent.click(within(table).getByRole('button', { name: 'shared-model' }))
  await waitFor(() => expect(new URLSearchParams(router.state.location.search).has('provider')).toBe(true))
  await waitFor(() => expect(requests.some(p => p.has('provider') && p.get('provider') === '')).toBe(true))
})

it('keeps the analytics page up when the usage history response is malformed', async () => {
  server.use(
    http.get('*/v1/analytics/agents', () => HttpResponse.json(EMPTY_AGENT_ANALYTICS_FIXTURE)),
    http.get('*/v1/analytics/agents/events', () => HttpResponse.json({})),
  )
  renderApp({ initialEntries: ['/analytics'], authState: authenticatedState('viewer') })
  expect(await screen.findByRole('heading', { name: 'My usage', level: 2 })).toBeInTheDocument()
  expect(await screen.findByText(/Usage history could not be loaded/)).toBeInTheDocument()
})

it('keeps the analytics page up when the filter choices response is malformed', async () => {
  server.use(
    http.get('*/v1/analytics/agents', () => HttpResponse.json(EMPTY_AGENT_ANALYTICS_FIXTURE)),
    http.get('*/v1/analytics/agents/facets', () => HttpResponse.json({})),
  )
  renderApp({ initialEntries: ['/analytics?project=p&app=a'], authState: authenticatedState('viewer') })
  expect(await screen.findByRole('heading', { name: 'My usage', level: 2 })).toBeInTheDocument()
  expect(await screen.findByText(/Usage filter choices could not be loaded/)).toBeInTheDocument()
})
