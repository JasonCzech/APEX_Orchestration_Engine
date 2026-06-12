import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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

  it('shows the empty state when the window has no entries', async () => {
    const logs = logsHandler([])
    server.use(logs.handler)
    renderLogs('?q=anything')

    expect(await screen.findByText('No log entries in this window')).toBeInTheDocument()
  })
})
