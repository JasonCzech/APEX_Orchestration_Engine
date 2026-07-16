import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  ITEM_PAYMENT,
  ITEM_BUG,
  SAVED_OPEN,
  TRANSLATED,
  createItemHandler,
  createSavedQueryHandler,
  executeHandler,
  getItemHandler,
  resolvedWorkItemPage,
  savedQueriesHandler,
  translateHandler,
  workTrackingBindingHandler,
} from './workItemsTestHandlers'
import { scopedMutationStorageKey } from './durableMutationDraft'

function renderConsole(role: 'operator' | 'viewer' = 'operator') {
  return renderApp({
    initialEntries: ['/work-items'],
    authState: authenticatedState(role),
  })
}

describe('WorkItemsPage', () => {
  beforeEach(() => window.sessionStorage.clear())

  it('translates NL to an editable provider query and executes it', async () => {
    const translate = translateHandler()
    const execute = executeHandler()
    server.use(translate.handler, execute.handler, savedQueriesHandler([]))
    const user = userEvent.setup()
    renderConsole()

    await user.type(await screen.findByLabelText('Find by description'), 'open payment stories')
    await user.click(screen.getByRole('button', { name: 'Translate' }))

    // Editable provider query + confidence chip.
    const providerInput = await screen.findByRole('textbox', {
      name: 'Provider',
    })
    expect(providerInput).toHaveValue('jira')
    expect(screen.getByRole('textbox', { name: 'Provider query' })).toHaveValue(TRANSLATED.query)
    expect(screen.getByText('confidence 82%')).toBeInTheDocument()
    expect(translate.captured).toEqual([
      { text: 'open payment stories', connection_id: 'conn-jira' },
    ])

    await user.click(screen.getByRole('button', { name: 'Execute' }))

    // Results table: key links to detail, kind chip, status badge, tracker link.
    const row = await screen.findByTestId('wi-row-PHX-101')
    expect(within(row).getByRole('link', { name: 'PHX-101' })).toHaveAttribute(
      'href',
      '/work-items/jira/PHX-101?project=proj-alpha&connection_id=conn-jira',
    )
    expect(within(row).getByText('story')).toHaveClass('dash-context-chip')
    expect(within(row).getByText('open')).toHaveClass('status-badge')
    expect(within(row).getByRole('link', { name: 'Open PHX-101 in tracker' })).toHaveAttribute(
      'href',
      ITEM_PAYMENT.url,
    )
    // The url-less row renders no tracker link.
    const bugRow = screen.getByTestId('wi-row-PHX-102')
    expect(within(bugRow).queryByRole('link', { name: /in tracker/ })).not.toBeInTheDocument()

    expect(execute.captured).toEqual([
      {
        query: { provider: 'jira', query: TRANSLATED.query, confidence: 0.82 },
        connection_id: 'conn-jira',
        limit: 25,
        offset: 0,
      },
    ])
    expect(screen.getByText('1–2 of 2')).toBeInTheDocument()
  })

  it('serializes translation and execution so their responses cannot cross', async () => {
    let translationCount = 0
    let markExecuteStarted!: () => void
    const executeStarted = new Promise<void>((resolve) => {
      markExecuteStarted = resolve
    })
    let releaseExecute!: () => void
    const executeRelease = new Promise<void>((resolve) => {
      releaseExecute = resolve
    })
    server.use(
      savedQueriesHandler([]),
      http.post('*/v1/work-tracking/query/translate', () => {
        translationCount += 1
        return HttpResponse.json(TRANSLATED)
      }),
      http.post('*/v1/work-tracking/query/execute', async () => {
        markExecuteStarted()
        await executeRelease
        return HttpResponse.json(resolvedWorkItemPage([ITEM_PAYMENT], 1))
      }),
    )
    const user = userEvent.setup()
    renderConsole()

    await user.type(await screen.findByLabelText('Find by description'), 'open stories')
    await user.click(screen.getByRole('button', { name: 'Translate' }))
    await user.click(await screen.findByRole('button', { name: 'Execute' }))
    await executeStarted

    const translateButton = screen.getByRole('button', { name: 'Translate' })
    expect(translateButton).toBeDisabled()
    expect(screen.getByLabelText('Find by description')).toBeDisabled()
    await user.click(translateButton)
    expect(translationCount).toBe(1)

    releaseExecute()
    expect(await screen.findByTestId('wi-row-PHX-101')).toBeInTheDocument()
  })

  it('manual mode executes a hand-written provider query', async () => {
    const execute = executeHandler([ITEM_PAYMENT], 1)
    server.use(execute.handler, savedQueriesHandler([]))
    const user = userEvent.setup()
    renderConsole()

    await user.click(await screen.findByRole('button', { name: 'Manual query' }))
    await user.type(screen.getByRole('textbox', { name: 'Provider' }), 'ado')
    await user.type(
      screen.getByRole('textbox', { name: 'Provider query' }),
      'status = Active AND type = Bug',
    )
    await user.click(screen.getByRole('button', { name: 'Execute' }))

    await screen.findByTestId('wi-row-PHX-101')
    expect(execute.captured).toEqual([
      {
        query: {
          provider: 'ado',
          query: 'status = Active AND type = Bug',
          confidence: 1,
        },
        limit: 25,
        offset: 0,
      },
    ])
  })

  it('retires prior results when a replacement query fails', async () => {
    server.use(
      savedQueriesHandler([]),
      http.post('*/v1/work-tracking/query/execute', async ({ request }) => {
        const body = (await request.json()) as {
          query: { query: string }
        }
        if (body.query.query === 'status = Broken') {
          return HttpResponse.json({ detail: 'provider unavailable' }, { status: 503 })
        }
        return HttpResponse.json(resolvedWorkItemPage([ITEM_PAYMENT], 1))
      }),
    )
    const user = userEvent.setup()
    renderConsole()

    await user.click(await screen.findByRole('button', { name: 'Manual query' }))
    await user.type(screen.getByRole('textbox', { name: 'Provider' }), 'jira')
    const query = screen.getByRole('textbox', { name: 'Provider query' })
    await user.type(query, 'status = Open')
    await user.click(screen.getByRole('button', { name: 'Execute' }))
    expect(await screen.findByTestId('wi-row-PHX-101')).toBeInTheDocument()

    await user.clear(query)
    await user.type(query, 'status = Broken')
    await user.click(screen.getByRole('button', { name: 'Execute' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Query failed')
    expect(screen.queryByTestId('wi-row-PHX-101')).not.toBeInTheDocument()
    expect(screen.queryByText('1–1 of 1')).not.toBeInTheDocument()
  })

  it('retires prior query and results when replacement translation fails', async () => {
    let translations = 0
    server.use(
      savedQueriesHandler([]),
      http.post('*/v1/work-tracking/query/translate', () => {
        translations += 1
        return translations === 1
          ? HttpResponse.json(TRANSLATED)
          : HttpResponse.json({ detail: 'translator unavailable' }, { status: 503 })
      }),
      http.post('*/v1/work-tracking/query/execute', () =>
        HttpResponse.json(resolvedWorkItemPage([ITEM_PAYMENT], 1)),
      ),
    )
    const user = userEvent.setup()
    renderConsole()

    const description = await screen.findByLabelText('Find by description')
    await user.type(description, 'open stories')
    await user.click(screen.getByRole('button', { name: 'Translate' }))
    await user.click(await screen.findByRole('button', { name: 'Execute' }))
    expect(await screen.findByTestId('wi-row-PHX-101')).toBeInTheDocument()

    await user.clear(description)
    await user.type(description, 'replacement request')
    await user.click(screen.getByRole('button', { name: 'Translate' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Translate failed')
    expect(screen.queryByTestId('provider-query')).not.toBeInTheDocument()
    expect(screen.queryByTestId('wi-row-PHX-101')).not.toBeInTheDocument()
  })

  it('requires and sends a project for multi-project consumers', async () => {
    const translate = translateHandler()
    const execute = executeHandler([ITEM_PAYMENT], 1)
    server.use(translate.handler, execute.handler, savedQueriesHandler([SAVED_OPEN]))
    const user = userEvent.setup()
    const base = authenticatedState('operator')
    if (base.status !== 'authenticated') throw new Error('expected authenticated test state')
    const consumer = {
      ...base.consumer,
      scopes: [
        { project_id: 'proj-alpha', app_id: null },
        { project_id: 'proj-beta', app_id: null },
      ],
    }
    renderApp({
      initialEntries: ['/work-items'],
      authState: {
        ...base,
        consumer,
        systemInfo: { ...base.systemInfo, consumer },
      },
    })

    const project = await screen.findByRole('combobox', {
      name: 'Work tracking project',
    })
    const savedQuery = await screen.findByLabelText('Saved queries')
    const translateButton = screen.getByRole('button', { name: 'Translate' })
    await user.type(screen.getByLabelText('Find by description'), 'open work')
    expect(translateButton).toBeDisabled()
    expect(savedQuery).toBeDisabled()
    await user.selectOptions(project, 'proj-beta')
    expect(savedQuery).toBeEnabled()
    await user.click(translateButton)
    await screen.findByRole('textbox', { name: 'Provider query' })
    await user.click(screen.getByRole('button', { name: 'Execute' }))
    await screen.findByTestId('wi-row-PHX-101')

    expect(translate.projects).toEqual(['proj-beta'])
    expect(execute.projects).toEqual(['proj-beta'])
  })

  it('defers a preloaded query until a multi-project consumer selects its scope', async () => {
    const execute = executeHandler([ITEM_PAYMENT], 1)
    server.use(execute.handler, savedQueriesHandler([]))
    const user = userEvent.setup()
    const base = authenticatedState('operator')
    if (base.status !== 'authenticated') throw new Error('expected authenticated test state')
    const consumer = {
      ...base.consumer,
      scopes: [
        { project_id: 'proj-alpha', app_id: null },
        { project_id: 'proj-beta', app_id: null },
      ],
    }
    renderApp({
      initialEntries: ['/work-items?provider=jira&query=status%20%3D%20Open'],
      authState: {
        ...base,
        consumer,
        systemInfo: { ...base.systemInfo, consumer },
      },
    })

    const project = await screen.findByRole('combobox', {
      name: 'Work tracking project',
    })
    expect(execute.captured).toHaveLength(0)
    await user.selectOptions(project, 'proj-alpha')

    await screen.findByTestId('wi-row-PHX-101')
    expect(execute.projects).toEqual(['proj-alpha'])
    expect(execute.captured).toHaveLength(1)
  })

  it('rehydrates on URL query changes and rejects a stale prior result', async () => {
    const started: string[] = []
    const completed: string[] = []
    server.use(
      savedQueriesHandler([]),
      http.post('*/v1/work-tracking/query/execute', async ({ request }) => {
        const body = (await request.json()) as {
          query: { provider: string; query: string; confidence: number }
          limit: number
          offset: number
        }
        started.push(body.query.query)
        if (body.query.query === 'status = Slow') await delay(140)
        completed.push(body.query.query)
        return HttpResponse.json(
          resolvedWorkItemPage(
            body.query.query === 'status = Slow' ? [ITEM_PAYMENT] : [ITEM_BUG],
            1,
            body.query.provider,
            body.query.provider === 'ado' ? 'conn-ado' : 'conn-jira',
          ),
        )
      }),
    )
    const { router } = renderApp({
      initialEntries: ['/work-items?provider=jira&query=status%20%3D%20Slow'],
      authState: authenticatedState('operator'),
    })

    await waitFor(() => expect(started).toContain('status = Slow'))
    await act(async () =>
      router.navigate('/work-items?provider=ado&query=status%20%3D%20Active'),
    )

    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Provider query' })).toHaveValue(
        'status = Active',
      ),
    )
    expect(screen.getByRole('textbox', { name: 'Provider' })).toHaveValue('ado')
    expect(await screen.findByTestId('wi-row-PHX-102')).toBeInTheDocument()

    await waitFor(() => expect(completed).toContain('status = Slow'))
    expect(screen.getByTestId('wi-row-PHX-102')).toBeInTheDocument()
    expect(screen.queryByTestId('wi-row-PHX-101')).not.toBeInTheDocument()
  })

  it('does not create through a deep-link connection until its provider is validated', async () => {
    const binding = workTrackingBindingHandler({
      connection_id: 'conn-ado',
      provider: 'ado',
    })
    server.use(
      binding.handler,
      savedQueriesHandler([]),
      http.post('*/v1/work-tracking/query/execute', () =>
        HttpResponse.json(
          { detail: 'connection provider does not match query provider' },
          { status: 409 },
        ),
      ),
    )

    renderApp({
      initialEntries: [
        '/work-items?provider=jira&query=status%20%3D%20Open&connection_id=conn-ado',
      ],
      authState: authenticatedState('operator'),
    })

    const newItemButton = await screen.findByRole('button', { name: 'New item' })
    await waitFor(() => expect(binding.connectionIds).toEqual(['conn-ado']))
    expect(await screen.findByRole('alert')).toHaveTextContent('Query failed')
    expect(newItemButton).toBeDisabled()
  })

  it('paginates the submitted query snapshot rather than edited controls', async () => {
    const captured: Array<Record<string, unknown>> = []
    server.use(
      savedQueriesHandler([]),
      http.post('*/v1/work-tracking/query/execute', async ({ request }) => {
        const body = (await request.json()) as {
          query: { provider: string }
          connection_id?: string
        } & Record<string, unknown>
        captured.push(body)
        return HttpResponse.json(
          resolvedWorkItemPage(
            [ITEM_PAYMENT],
            60,
            body.query.provider,
            body.connection_id,
          ),
        )
      }),
    )
    const user = userEvent.setup()
    renderConsole()

    await user.click(await screen.findByRole('button', { name: 'Manual query' }))
    const provider = screen.getByRole('textbox', { name: 'Provider' })
    const query = screen.getByRole('textbox', { name: 'Provider query' })
    await user.type(provider, 'jira')
    await user.type(query, 'project = ALPHA')
    await user.click(screen.getByRole('button', { name: 'Execute' }))
    await screen.findByTestId('wi-row-PHX-101')

    await user.clear(provider)
    await user.type(provider, 'ado')
    await user.clear(query)
    await user.type(query, 'SELECT changed')
    await user.click(screen.getByRole('button', { name: 'Next' }))

    await waitFor(() => expect(captured).toHaveLength(2))
    expect(captured[1]).toMatchObject({
      query: { provider: 'jira', query: 'project = ALPHA', confidence: 1 },
      limit: 25,
      offset: 25,
    })
  })

  it('stops at the provider result-window boundary and explains the limit', async () => {
    const captured: Array<{ offset: number }> = []
    server.use(
      savedQueriesHandler([]),
      http.post('*/v1/work-tracking/query/execute', async ({ request }) => {
        const body = (await request.json()) as {
          query: { provider: string }
          connection_id?: string
          limit: number
          offset: number
        }
        captured.push(body)
        return HttpResponse.json(
          resolvedWorkItemPage(
            [ITEM_PAYMENT],
            1_100,
            body.query.provider,
            body.connection_id,
          ),
        )
      }),
    )
    const user = userEvent.setup()
    renderConsole()

    await user.click(await screen.findByRole('button', { name: 'Manual query' }))
    await user.type(screen.getByRole('textbox', { name: 'Provider' }), 'jira')
    await user.type(screen.getByRole('textbox', { name: 'Provider query' }), 'project = ALPHA')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Page size' }), '50')
    await user.click(screen.getByRole('button', { name: 'Execute' }))
    await screen.findByTestId('wi-row-PHX-101')

    for (let page = 1; page <= 19; page += 1) {
      await user.click(screen.getByRole('button', { name: 'Next' }))
      await waitFor(() => expect(captured.at(-1)?.offset).toBe(page * 50))
    }

    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
    expect(screen.getByText(/Reached the provider result-window limit/)).toBeInTheDocument()
    expect(captured.at(-1)?.offset).toBe(950)
  })

  it('runs a saved query on pick and saves the current query via the modal', async () => {
    const execute = executeHandler([ITEM_PAYMENT], 1)
    const create = createSavedQueryHandler()
    server.use(savedQueriesHandler([SAVED_OPEN]), execute.handler, create.handler)
    const user = userEvent.setup()
    renderConsole()

    await user.selectOptions(await screen.findByLabelText('Saved queries'), SAVED_OPEN.id)

    // Picking executes immediately and loads the query into the editors.
    await screen.findByTestId('wi-row-PHX-101')
    expect(execute.captured).toEqual([
      {
        query: { provider: 'jira', query: SAVED_OPEN.query, confidence: 1 },
        connection_id: 'conn-jira',
        limit: 25,
        offset: 0,
      },
    ])
    expect(screen.getByRole('textbox', { name: 'Provider query' })).toHaveValue(SAVED_OPEN.query)

    await user.click(screen.getByRole('button', { name: 'Save query' }))
    const dialog = await screen.findByRole('dialog', { name: 'Save query' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Query name' }), 'My triage')
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Query description' }),
      'Console pick list',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Save query' }))

    await waitFor(() =>
      expect(create.captured).toEqual([
        {
          name: 'My triage',
          description: 'Console pick list',
          provider: 'jira',
          query: SAVED_OPEN.query,
          project_id: 'proj-alpha',
          connection_id: 'conn-jira',
        },
      ]),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('creates a new item from the modal and lands on its detail', async () => {
    const create = createItemHandler('PHX-300')
    const CREATED = {
      key: 'PHX-300',
      title: 'Harden gateway retries',
      kind: 'task',
      status: 'open',
      description: 'Spike output',
      url: null,
    }
    server.use(savedQueriesHandler([]), create.handler, getItemHandler([CREATED]))
    const user = userEvent.setup()
    const { router } = renderConsole()

    const newItemButton = await screen.findByRole('button', { name: 'New item' })
    await waitFor(() => expect(newItemButton).toBeEnabled())
    await user.click(newItemButton)
    const dialog = await screen.findByRole('dialog', { name: 'New work item' })
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Item title' }),
      'Harden gateway retries',
    )
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Item kind' }), 'task')
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Item description' }),
      'Spike output',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Create item' }))

    await waitFor(() => expect(router.state.location.pathname).toBe('/work-items/jira/PHX-300'))
    const params = new URLSearchParams(router.state.location.search)
    expect(params.get('project')).toBe('proj-alpha')
    expect(params.get('connection_id')).toBe('conn-jira')
    expect(create.captured).toEqual([
      {
        title: 'Harden gateway retries',
        kind: 'task',
        description: 'Spike output',
      },
    ])
    expect(create.idempotencyKeys[0]).toMatch(/^create-/)
    expect(create.connectionIds).toEqual(['conn-jira'])
    expect(
      await screen.findByRole('heading', { name: 'Harden gateway retries' }),
    ).toBeInTheDocument()
  })

  it('retires a successful durable create after the submitting route unmounts', async () => {
    let markCreateStarted!: () => void
    const createStarted = new Promise<void>((resolve) => {
      markCreateStarted = resolve
    })
    let releaseCreate!: () => void
    const createRelease = new Promise<void>((resolve) => {
      releaseCreate = resolve
    })
    server.use(
      savedQueriesHandler([]),
      http.post('*/v1/work-tracking/items', async ({ request }) => {
        const body = (await request.json()) as {
          title: string
          kind: string
          description: string
        }
        markCreateStarted()
        await createRelease
        return HttpResponse.json(
          {
            ...body,
            key: 'PHX-302',
            status: 'open',
            url: null,
            connection_id: 'conn-jira',
            provider: 'jira',
          },
          { status: 201 },
        )
      }),
    )
    const storageKey = scopedMutationStorageKey(
      'apex.work-items.create.v2',
      'proj-alpha',
      'conn-jira',
    )
    const user = userEvent.setup()
    const { router } = renderConsole()

    const newItemButton = await screen.findByRole('button', { name: 'New item' })
    await waitFor(() => expect(newItemButton).toBeEnabled())
    await user.click(newItemButton)
    const dialog = await screen.findByRole('dialog', { name: 'New work item' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Item title' }), 'Finish later')
    await user.click(within(dialog).getByRole('button', { name: 'Create item' }))
    await createStarted
    expect(window.sessionStorage.getItem(storageKey)).not.toBeNull()

    await act(async () => router.navigate('/settings'))
    releaseCreate()

    await waitFor(() => expect(window.sessionStorage.getItem(storageKey)).toBeNull())
    expect(router.state.location.pathname).toBe('/settings')

    await act(async () => router.navigate('/work-items'))
    const remountedButton = await screen.findByRole('button', { name: 'New item' })
    await waitFor(() => expect(remountedButton).toBeEnabled())
    await user.click(remountedButton)
    const remountedDialog = await screen.findByRole('dialog', { name: 'New work item' })
    expect(within(remountedDialog).getByRole('textbox', { name: 'Item title' })).toHaveValue('')
    expect(within(remountedDialog).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('blocks item creation before the request when safe retry storage is unavailable', async () => {
    const create = createItemHandler('PHX-301')
    server.use(savedQueriesHandler([]), create.handler)
    const user = userEvent.setup()
    renderConsole()

    const newItemButton = await screen.findByRole('button', { name: 'New item' })
    await waitFor(() => expect(newItemButton).toBeEnabled())
    await user.click(newItemButton)
    const dialog = await screen.findByRole('dialog', { name: 'New work item' })
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Item title' }),
      'Must be safely retryable',
    )
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)

    try {
      const submit = within(dialog).getByRole('button', { name: 'Create item' })
      await user.click(submit)

      expect(await within(dialog).findByRole('alert')).toHaveTextContent(
        'Create blocked: Safe retry storage is unavailable',
      )
      expect(create.captured).toEqual([])
      expect(submit).toBeDisabled()
    } finally {
      setItem.mockRestore()
    }
  })

  it('hides Save query and New item from viewers', async () => {
    const translate = translateHandler()
    server.use(translate.handler, savedQueriesHandler([]))
    const user = userEvent.setup()
    renderConsole('viewer')

    expect(await screen.findByLabelText('Find by description')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New item' })).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('Find by description'), 'anything open')
    await user.click(screen.getByRole('button', { name: 'Translate' }))
    await screen.findByRole('textbox', { name: 'Provider' })
    expect(screen.queryByRole('button', { name: 'Save query' })).not.toBeInTheDocument()
  })

  it('reuses the durable create key and draft after an ambiguous error and reopen', async () => {
    const keys: string[] = []
    server.use(
      savedQueriesHandler([]),
      http.post('*/v1/work-tracking/items', ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key') ?? '')
        return HttpResponse.json({ detail: 'upstream response lost' }, { status: 502 })
      }),
    )
    const user = userEvent.setup()
    renderConsole()

    const newItemButton = await screen.findByRole('button', { name: 'New item' })
    await waitFor(() => expect(newItemButton).toBeEnabled())
    await user.click(newItemButton)
    let dialog = await screen.findByRole('dialog', { name: 'New work item' })
    await user.type(within(dialog).getByRole('textbox', { name: 'Item title' }), 'Retry me')
    await user.click(within(dialog).getByRole('button', { name: 'Create item' }))
    await within(dialog).findByRole('alert')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await user.click(screen.getByRole('button', { name: 'New item' }))
    dialog = await screen.findByRole('dialog', { name: 'New work item' })
    const title = within(dialog).getByRole('textbox', { name: 'Item title' })
    expect(title).toHaveValue('Retry me')
    await user.clear(title)
    await user.type(title, 'Different payload')
    await user.clear(title)
    await user.type(title, 'Retry me')
    await user.click(within(dialog).getByRole('button', { name: 'Create item' }))
    await waitFor(() => expect(keys).toHaveLength(2))

    expect(keys[0]).toMatch(/^create-/)
    expect(keys[1]).toBe(keys[0])
  })

  it('hides project-wide mutations from app-only operators', async () => {
    server.use(savedQueriesHandler([]))
    const base = authenticatedState('operator')
    if (base.status !== 'authenticated') throw new Error('expected authenticated test state')
    const consumer = {
      ...base.consumer,
      scopes: [{ project_id: 'proj-alpha', app_id: 'app-one' }],
    }
    renderApp({
      initialEntries: ['/work-items'],
      authState: {
        ...base,
        consumer,
        systemInfo: { ...base.systemInfo, consumer },
      },
    })

    expect(await screen.findByLabelText('Find by description')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New item' })).not.toBeInTheDocument()
  })
})
