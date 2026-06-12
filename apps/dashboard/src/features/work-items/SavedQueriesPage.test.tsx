import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  ITEM_PAYMENT,
  SAVED_BUGS,
  SAVED_OPEN,
  deleteSavedQueryHandler,
  executeHandler,
  savedQueriesHandler,
  updateSavedQueryHandler,
} from './workItemsTestHandlers'

function renderList(role: 'operator' | 'viewer' = 'operator') {
  return renderApp({
    initialEntries: ['/work-items/saved'],
    authState: authenticatedState(role),
  })
}

describe('SavedQueriesPage', () => {
  it('lists queries and Run preloads + auto-executes them in the console', async () => {
    const execute = executeHandler([ITEM_PAYMENT], 1)
    server.use(savedQueriesHandler([SAVED_OPEN, SAVED_BUGS]), execute.handler)
    const user = userEvent.setup()
    const { router } = renderList()

    const row = await screen.findByTestId(`saved-query-row-${SAVED_OPEN.id}`)
    expect(within(row).getByText('Open payment stories')).toHaveClass('strong')
    expect(within(row).getByText('jira')).toHaveClass('dash-context-chip')
    expect(within(row).getByText(SAVED_OPEN.query)).toHaveClass('wi-query-cell')
    expect(within(row).getByText('Sprint triage pick list')).toBeInTheDocument()
    // Null description renders an em dash on the other row.
    const adoRow = screen.getByTestId(`saved-query-row-${SAVED_BUGS.id}`)
    expect(within(adoRow).getByText('—')).toBeInTheDocument()

    await user.click(
      within(row).getByRole('button', { name: 'Saved query actions: Open payment stories' }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Run' }))

    // Run links to the console via ?provider=&query= search params (documented
    // contract) and the console auto-executes once on mount.
    await waitFor(() => expect(router.state.location.pathname).toBe('/work-items'))
    const params = new URLSearchParams(router.state.location.search)
    expect(params.get('provider')).toBe('jira')
    expect(params.get('query')).toBe(SAVED_OPEN.query)

    await screen.findByTestId('wi-row-PHX-101')
    expect(execute.captured).toEqual([
      {
        query: { provider: 'jira', query: SAVED_OPEN.query, confidence: 1 },
        limit: 25,
        offset: 0,
      },
    ])
    expect(screen.getByRole('textbox', { name: 'Provider query' })).toHaveValue(SAVED_OPEN.query)
  })

  it('edits a saved query through the modal (PATCH shape)', async () => {
    const update = updateSavedQueryHandler(SAVED_OPEN)
    server.use(savedQueriesHandler([SAVED_OPEN]), update.handler)
    const user = userEvent.setup()
    renderList()

    const row = await screen.findByTestId(`saved-query-row-${SAVED_OPEN.id}`)
    await user.click(
      within(row).getByRole('button', { name: 'Saved query actions: Open payment stories' }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Edit…' }))

    const dialog = await screen.findByRole('dialog', {
      name: 'Edit saved query Open payment stories',
    })
    const nameInput = within(dialog).getByRole('textbox', { name: 'Query name' })
    expect(nameInput).toHaveValue('Open payment stories')
    await user.clear(nameInput)
    await user.type(nameInput, 'Payment triage')
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(update.captured).toEqual([
        {
          name: 'Payment triage',
          provider: SAVED_OPEN.provider,
          query: SAVED_OPEN.query,
          description: SAVED_OPEN.description,
        },
      ]),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('deletes after the confirm dialog', async () => {
    const del = deleteSavedQueryHandler()
    server.use(savedQueriesHandler([SAVED_OPEN]), del.handler)
    const user = userEvent.setup()
    renderList()

    const row = await screen.findByTestId(`saved-query-row-${SAVED_OPEN.id}`)
    await user.click(
      within(row).getByRole('button', { name: 'Saved query actions: Open payment stories' }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Delete…' }))

    const dialog = await screen.findByRole('dialog', {
      name: 'Delete saved query Open payment stories',
    })
    expect(del.captured).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Delete query' }))

    await waitFor(() => expect(del.captured).toEqual([SAVED_OPEN.id]))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('offers viewers Run but not Edit/Delete', async () => {
    server.use(savedQueriesHandler([SAVED_OPEN]))
    const user = userEvent.setup()
    renderList('viewer')

    const row = await screen.findByTestId(`saved-query-row-${SAVED_OPEN.id}`)
    await user.click(
      within(row).getByRole('button', { name: 'Saved query actions: Open payment stories' }),
    )
    expect(screen.getByRole('menuitem', { name: 'Run' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Edit…' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Delete…' })).not.toBeInTheDocument()
  })
})
