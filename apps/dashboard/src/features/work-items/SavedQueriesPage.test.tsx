import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  ITEM_PAYMENT,
  SAVED_BUGS,
  SAVED_OPEN,
  SAVED_OPEN_LEGACY,
  deleteSavedQueryHandler,
  executeHandler,
  savedQueriesHandler,
  updateSavedQueryHandler,
  workTrackingBindingHandler,
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
    expect(params.get('project')).toBe('proj-alpha')
    expect(params.get('connection_id')).toBe('conn-jira')

    await screen.findByTestId('wi-row-PHX-101')
    expect(execute.captured).toEqual([
      {
        query: { provider: 'jira', query: SAVED_OPEN.query, confidence: 1 },
        connection_id: 'conn-jira',
        limit: 25,
        offset: 0,
      },
    ])
    expect(execute.projects).toEqual(['proj-alpha'])
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
          query: SAVED_OPEN.query,
          description: SAVED_OPEN.description,
        },
      ]),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('locks the saved-query row across route remounts while an update is pending', async () => {
    let markUpdateStarted!: () => void
    const updateStarted = new Promise<void>((resolve) => {
      markUpdateStarted = resolve
    })
    let releaseUpdate!: () => void
    const updateRelease = new Promise<void>((resolve) => {
      releaseUpdate = resolve
    })
    server.use(
      savedQueriesHandler([SAVED_OPEN]),
      http.patch('*/v1/work-tracking/saved-queries/:savedQueryId', async ({ request }) => {
        const body = (await request.json()) as { name: string }
        markUpdateStarted()
        await updateRelease
        return HttpResponse.json({ ...SAVED_OPEN, name: body.name })
      }),
    )
    const user = userEvent.setup()
    const { router } = renderList()

    const row = await screen.findByTestId(`saved-query-row-${SAVED_OPEN.id}`)
    await user.click(
      within(row).getByRole('button', { name: 'Saved query actions: Open payment stories' }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Edit…' }))
    const dialog = await screen.findByRole('dialog', {
      name: 'Edit saved query Open payment stories',
    })
    const name = within(dialog).getByRole('textbox', { name: 'Query name' })
    await user.clear(name)
    await user.type(name, 'Deferred update')
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }))
    await updateStarted
    expect(name).toBeDisabled()

    await act(async () => {
      await router.navigate('/settings')
    })
    expect(router.state.location.pathname).toBe('/settings')
    await act(async () => {
      await router.navigate('/work-items/saved')
    })
    expect(router.state.location.pathname).toBe('/work-items/saved')
    await screen.findByTestId(`saved-query-row-${SAVED_OPEN.id}`)
    await user.click(
      screen.getByLabelText('Saved query actions: Open payment stories'),
    )
    expect(screen.getByRole('menuitem', { name: 'Run' })).toBeDisabled()
    expect(screen.getByRole('menuitem', { name: 'Edit…' })).toBeDisabled()
    expect(screen.getByRole('menuitem', { name: 'Delete…' })).toBeDisabled()

    releaseUpdate()
    await waitFor(() => {
      expect(screen.getByRole('menuitem', { name: 'Run' })).toBeEnabled()
      expect(screen.getByRole('menuitem', { name: 'Edit…' })).toBeEnabled()
    })
  })

  it('rebinds a legacy project query to the resolved exact connection', async () => {
    const binding = workTrackingBindingHandler()
    const update = updateSavedQueryHandler(SAVED_OPEN_LEGACY)
    server.use(
      savedQueriesHandler([SAVED_OPEN_LEGACY]),
      binding.handler,
      update.handler,
    )
    const user = userEvent.setup()
    renderList()

    const row = await screen.findByTestId(`saved-query-row-${SAVED_OPEN_LEGACY.id}`)
    await user.click(
      within(row).getByRole('button', {
        name: `Saved query actions: ${SAVED_OPEN_LEGACY.name}`,
      }),
    )
    expect(screen.getByRole('menuitem', { name: 'Run (rebind required)' })).toBeDisabled()
    await user.click(screen.getByRole('menuitem', { name: 'Rebind…' }))

    const dialog = await screen.findByRole('dialog', {
      name: `Edit saved query ${SAVED_OPEN_LEGACY.name}`,
    })
    expect(
      within(dialog).getByText(/legacy query is not bound to a connection/i),
    ).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Rebind and save' }))

    await waitFor(() => expect(binding.projects).toEqual(['proj-alpha']))
    expect(binding.connectionIds).toEqual([null])
    await waitFor(() =>
      expect(update.captured).toEqual([
        {
          name: SAVED_OPEN_LEGACY.name,
          query: SAVED_OPEN_LEGACY.query,
          description: SAVED_OPEN_LEGACY.description,
          connection_id: 'conn-jira',
        },
      ]),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('refuses to rebind a legacy project query through a different provider', async () => {
    const binding = workTrackingBindingHandler({
      connection_id: 'conn-ado',
      provider: 'ado',
    })
    const update = updateSavedQueryHandler(SAVED_OPEN_LEGACY)
    server.use(
      savedQueriesHandler([SAVED_OPEN_LEGACY]),
      binding.handler,
      update.handler,
    )
    const user = userEvent.setup()
    renderList()

    const row = await screen.findByTestId(`saved-query-row-${SAVED_OPEN_LEGACY.id}`)
    await user.click(
      within(row).getByRole('button', {
        name: `Saved query actions: ${SAVED_OPEN_LEGACY.name}`,
      }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Rebind…' }))
    const dialog = await screen.findByRole('dialog', {
      name: `Edit saved query ${SAVED_OPEN_LEGACY.name}`,
    })
    await user.click(within(dialog).getByRole('button', { name: 'Rebind and save' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Rebind failed: The current project connection uses ado, not jira.',
    )
    expect(binding.projects).toEqual(['proj-alpha'])
    expect(update.captured).toHaveLength(0)
    expect(dialog).toBeInTheDocument()
  })

  it('does not continue a legacy rebind after the modal unmounts', async () => {
    let markBindingStarted!: () => void
    let releaseBinding!: () => void
    const bindingStarted = new Promise<void>((resolve) => {
      markBindingStarted = resolve
    })
    const bindingRelease = new Promise<void>((resolve) => {
      releaseBinding = resolve
    })
    let updates = 0
    server.use(
      savedQueriesHandler([SAVED_OPEN_LEGACY]),
      http.get('*/v1/work-tracking/binding', async () => {
        markBindingStarted()
        await bindingRelease
        return HttpResponse.json({
          connection_id: 'conn-jira',
          provider: 'jira',
        })
      }),
      http.patch('*/v1/work-tracking/saved-queries/:savedQueryId', () => {
        updates += 1
        return HttpResponse.json(SAVED_OPEN)
      }),
    )
    const user = userEvent.setup()
    const rendered = renderList()

    const row = await screen.findByTestId(`saved-query-row-${SAVED_OPEN_LEGACY.id}`)
    await user.click(
      within(row).getByRole('button', {
        name: `Saved query actions: ${SAVED_OPEN_LEGACY.name}`,
      }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Rebind…' }))
    const dialog = await screen.findByRole('dialog', {
      name: `Edit saved query ${SAVED_OPEN_LEGACY.name}`,
    })
    await user.click(within(dialog).getByRole('button', { name: 'Rebind and save' }))
    await bindingStarted

    rendered.unmount()
    releaseBinding()
    await act(async () => {
      await bindingRelease
      await Promise.resolve()
    })

    expect(updates).toBe(0)
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
