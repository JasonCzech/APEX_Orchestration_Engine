import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import { logsErrorHandler, logsHandler, makeEntries } from './logsTestHandlers'

function renderLogs(search = '') {
  return renderApp({
    initialEntries: [`/logs${search}`],
    authState: authenticatedState(),
  })
}

describe('LogsPage', () => {
  it('does not label the previous response as results for a newly submitted search', async () => {
    server.use(
      http.post('*/v1/logs/search', async ({ request }) => {
        const body = (await request.json()) as {
          query?: { text?: string }
          limit: number
          offset: number
        }
        const text = body.query?.text ?? 'empty'
        if (text === 'second') await delay(150)
        return HttpResponse.json({
          entries: [
            {
              at: '2026-06-12T10:00:00Z',
              level: 'INFO',
              service: 'apex-api',
              message: `${text} result`,
              fields: {},
            },
          ],
          total: 1,
          limit: body.limit,
          offset: body.offset,
          window: { from: null, to: null },
        })
      }),
    )
    const user = userEvent.setup()
    renderLogs('?q=first')

    expect(await screen.findByText('first result')).toBeInTheDocument()
    const input = screen.getByRole('searchbox', { name: 'Log query' })
    await user.clear(input)
    await user.type(input, 'second')
    await user.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByRole('status', { name: 'Searching logs' })).toBeInTheDocument()
    expect(screen.queryByText('first result')).not.toBeInTheDocument()
    expect(await screen.findByText('second result')).toBeInTheDocument()
  })

  it('searches only on explicit submit — typing fires no requests', async () => {
    const logs = logsHandler()
    server.use(logs.handler)
    const user = userEvent.setup()
    const { router } = renderLogs()

    expect(await screen.findByText('Search the logs')).toBeInTheDocument()
    await user.type(screen.getByRole('searchbox', { name: 'Log query' }), 'engine timeout')
    expect(logs.captured).toHaveLength(0)

    await user.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText('phase execution failed: engine timeout')).toBeInTheDocument()
    expect(logs.captured).toHaveLength(1)
    expect(logs.captured[0]!.query?.text).toBe('engine timeout')
    expect(logs.captured[0]!.offset).toBe(0)
    // Submit commits the search to the URL for shareable deep links.
    expect(router.state.location.search).toBe('?q=engine+timeout')
  })

  it('renders tonal level badges per entry', async () => {
    const logs = logsHandler()
    server.use(logs.handler)
    renderLogs('?q=anything')

    const table = await screen.findByRole('table')
    expect(within(table).getByText('ERROR')).toHaveClass('status-badge', 'danger')
    expect(within(table).getByText('WARN')).toHaveClass('status-badge', 'warning')
    expect(within(table).getByText('INFO')).toHaveClass('status-badge', 'info')
    expect(within(table).getByText('DEBUG')).toHaveClass('status-badge', 'neutral')
    // Service chips render alongside.
    expect(within(table).getAllByText('apex-api')[0]).toHaveClass('dash-context-chip')
  })

  it('expands a row to show the provider fields JSON', async () => {
    const logs = logsHandler()
    server.use(logs.handler)
    const user = userEvent.setup()
    renderLogs('?q=anything')

    await screen.findByRole('table')
    // Only the ERROR entry carries extras, so exactly one expand affordance.
    const toggles = screen.getAllByRole('button', { name: /Toggle fields/ })
    expect(toggles).toHaveLength(1)

    await user.click(toggles[0]!)
    const fieldsRow = screen.getByTestId('log-fields-row')
    expect(fieldsRow).toHaveTextContent('"thread_id": "run-123"')
    expect(fieldsRow).toHaveTextContent('"attempt": 2')

    await user.click(screen.getByRole('button', { name: /Toggle fields/ }))
    expect(screen.queryByTestId('log-fields-row')).not.toBeInTheDocument()
  })

  it('shows the provider reason inline when the query is rejected (422)', async () => {
    const rejected = logsErrorHandler(
      422,
      'log provider rejected the query: Failed to parse query [level:[}]',
    )
    server.use(rejected.handler)
    renderLogs('?q=level%3A%5B%7D')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Query rejected')
    expect(alert).toHaveTextContent(
      'log provider rejected the query: Failed to parse query [level:[}]',
    )
  })

  it('shows a connection problem card with retry on upstream failure (502)', async () => {
    const down = logsErrorHandler(502, 'log search upstream failure: connect timeout')
    server.use(down.handler)
    const user = userEvent.setup()
    renderLogs('?q=anything')

    const card = await screen.findByText('Log search connection problem')
    expect(card).toBeInTheDocument()
    expect(screen.getByText('log search upstream failure: connect timeout')).toBeInTheDocument()

    // Retry re-runs the same submitted search once the upstream recovers.
    const recovered = logsHandler()
    server.use(recovered.handler)
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('pipeline started')).toBeInTheDocument()
    expect(recovered.captured).toHaveLength(1)
  })

  it('prefills the thread filter from ?thread and auto-runs the deep link', async () => {
    const logs = logsHandler()
    server.use(logs.handler)
    renderLogs('?thread=run-123')

    await screen.findByRole('table')
    expect(screen.getByRole('textbox', { name: 'Thread id filter' })).toHaveValue('run-123')
    // The deep link searches with the conventional thread_id exact-match filter.
    expect(logs.captured).toHaveLength(1)
    expect(logs.captured[0]!.query?.filters).toEqual({ thread_id: 'run-123' })
  })

  it('paginates the submitted search with offset and a total caption', async () => {
    const logs = logsHandler(makeEntries(120))
    server.use(logs.handler)
    const user = userEvent.setup()
    renderLogs('?q=anything')

    await screen.findByText('log line 0')
    expect(screen.getByText('1–50 of 120 entries')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Next' }))

    expect(await screen.findByText('log line 50')).toBeInTheDocument()
    expect(screen.getByText('51–100 of 120 entries')).toBeInTheDocument()
    await waitFor(() => expect(logs.captured).toHaveLength(2))
    expect(logs.captured[1]!.offset).toBe(50)
    expect(logs.captured[1]!.query?.text).toBe('anything')
  })

  it('retains cached results and pagination when a background refresh fails', async () => {
    const logs = logsHandler(makeEntries(120))
    server.use(logs.handler)
    const { queryClient } = renderLogs('?q=anything')

    expect(await screen.findByText('log line 0')).toBeInTheDocument()
    server.use(logsErrorHandler(502, 'provider unavailable').handler)
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['logs'] })
    })

    expect(await screen.findByText(/Showing cached data/)).toBeInTheDocument()
    expect(screen.getByText('log line 0')).toBeInTheDocument()
    expect(screen.getByText('1–50 of 120 entries')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled()
  })

  it('keeps Previous available when loading a later page fails', async () => {
    server.use(
      http.post('*/v1/logs/search', async ({ request }) => {
        const body = (await request.json()) as { limit: number; offset: number }
        if (body.offset > 0) {
          return HttpResponse.json({ detail: 'provider unavailable' }, { status: 502 })
        }
        return HttpResponse.json({
          entries: makeEntries(50),
          total: 120,
          limit: body.limit,
          offset: body.offset,
          window: { from: null, to: null },
        })
      }),
    )
    const user = userEvent.setup()
    renderLogs('?q=anything')

    expect(await screen.findByText('log line 0')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Next' }))

    expect(await screen.findByText('Log search connection problem')).toBeInTheDocument()
    const previous = screen.getByRole('button', { name: 'Previous' })
    expect(previous).toBeEnabled()
    await user.click(previous)
    expect(await screen.findByText('log line 0')).toBeInTheDocument()
  })

  it('stops at the provider result-window boundary and explains the limit', async () => {
    const logs = logsHandler(makeEntries(1_100))
    server.use(logs.handler)
    const user = userEvent.setup()
    renderLogs('?q=anything')

    await screen.findByText('1–50 of 1100 entries')
    for (let page = 1; page <= 20; page += 1) {
      await user.click(screen.getByRole('button', { name: 'Next' }))
      await screen.findByText(`${page * 50 + 1}–${page * 50 + 50} of 1100 entries`)
    }

    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
    expect(screen.getByText(/Reached the provider result-window limit/)).toBeInTheDocument()
    expect(logs.captured.at(-1)?.offset).toBe(1_000)
  })

  it('re-runs the URL-committed search on browser Back navigation', async () => {
    const logs = logsHandler()
    server.use(logs.handler)
    const user = userEvent.setup()
    const { router, queryClient } = renderLogs('?q=first')

    const input = await screen.findByRole('searchbox', { name: 'Log query' })
    await waitFor(() => expect(logs.captured.at(-1)?.query?.text).toBe('first'))
    await user.clear(input)
    await user.type(input, 'second')
    await user.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(logs.captured.at(-1)?.query?.text).toBe('second'))

    await act(async () => {
      await router.navigate(-1)
    })
    await waitFor(() => expect(input).toHaveValue('first'))
    // The first search is still fresh in React Query, so Back may reuse it
    // without another POST. The active observer must nevertheless move back
    // to the URL's committed query rather than keep observing "second".
    await waitFor(() => {
      const active = queryClient
        .getQueryCache()
        .getAll()
        .find(
          (query) =>
            query.getObserversCount() > 0 &&
            query.queryKey[0] === 'logs' &&
            query.queryKey[1] === 'search',
        )
      expect(active?.queryKey[2]).toMatchObject({ text: 'first', offset: 0 })
    })
  })

  it('shows the empty state when the window has no entries', async () => {
    const logs = logsHandler([])
    server.use(logs.handler)
    renderLogs('?q=anything')

    expect(await screen.findByText('No log entries in this window')).toBeInTheDocument()
  })
})
