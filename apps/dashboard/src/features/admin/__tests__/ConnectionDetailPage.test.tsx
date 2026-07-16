import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'
import { queryKeys } from '@/api/queryKeys'

import {
  connectionsReadHandlers,
  deleteConnectionHandler,
  probeHandler,
  putHostMappingsHandler,
  updateConnectionHandler,
  CONN_ENGINE,
  CONN_JIRA,
  CONN_KUBERNETES,
} from './adminTestHandlers'

function renderDetail(entry = '/admin/connections/conn-jira') {
  return renderApp({
    initialEntries: [entry],
    authState: authenticatedState('admin', 'Dash Ops', []),
  })
}

describe('ConnectionDetailPage', () => {
  it('renders a green inline panel with latency when the probe passes', async () => {
    const probe = probeHandler({ ok: true, latency_ms: 41.6, detail: 'GET /rest/api/2/myself 200' })
    server.use(...connectionsReadHandlers(), probe.handler)
    const user = userEvent.setup()
    renderDetail()

    await user.click(await screen.findByRole('button', { name: 'Test connection' }))

    const panel = await screen.findByTestId('probe-result')
    expect(panel).toHaveClass('adm-inline-ok')
    expect(panel).toHaveTextContent('Connection healthy in 42 ms')
    expect(panel).toHaveTextContent('GET /rest/api/2/myself 200')
    expect(probe.callCount()).toBe(1)
  })

  it('renders the failure detail in an inline danger panel (not a toast)', async () => {
    const probe = probeHandler({
      ok: false,
      latency_ms: 1203.4,
      detail: 'SecretResolutionError: env JIRA_API_TOKEN is not set',
    })
    server.use(...connectionsReadHandlers(), probe.handler)
    const user = userEvent.setup()
    renderDetail()

    await user.click(await screen.findByRole('button', { name: 'Test connection' }))

    const panel = await screen.findByTestId('probe-result')
    expect(panel).toHaveClass('adm-inline-error')
    expect(panel).toHaveTextContent('SecretResolutionError: env JIRA_API_TOKEN is not set')
    // The HTTP call was a 200 — the page must NOT surface a query error.
    expect(screen.queryByText(/Test failed/)).not.toBeInTheDocument()
  })

  it('PUTs the full host-mapping list on save (add + edit + remove survive)', async () => {
    const put = putHostMappingsHandler()
    server.use(...connectionsReadHandlers(), put.handler)
    const user = userEvent.setup()
    renderDetail('/admin/connections/conn-jira?tab=host-mappings')

    const table = await screen.findByRole('table', { name: 'Host mappings' })
    // Seeded from the GET fixture.
    expect(within(table).getByRole('textbox', { name: 'Mapping 1 pattern' })).toHaveValue(
      '*.internal.example.com',
    )

    await user.click(screen.getByRole('button', { name: 'Add mapping' }))
    await user.type(
      screen.getByRole('textbox', { name: 'Mapping 2 pattern' }),
      'db.example.com',
    )
    await user.type(screen.getByRole('textbox', { name: 'Mapping 2 target' }), '10.0.0.5')
    await user.click(screen.getByRole('checkbox', { name: 'Mapping 2 enabled' })) // off
    await user.click(screen.getByRole('button', { name: 'Save mappings' }))

    await waitFor(() =>
      expect(put.captured).toEqual([
        [
          { pattern: '*.internal.example.com', target: 'proxy.example.com', enabled: true },
          { pattern: 'db.example.com', target: '10.0.0.5', enabled: false },
        ],
      ]),
    )
  })

  it('preserves unsaved host mappings across an unrelated connections refetch', async () => {
    server.use(...connectionsReadHandlers())
    const user = userEvent.setup()
    const { queryClient } = renderDetail('/admin/connections/conn-jira?tab=host-mappings')

    const pattern = await screen.findByRole('textbox', { name: 'Mapping 1 pattern' })
    await user.clear(pattern)
    await user.type(pattern, '*.edited.example.com')

    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.admin.connections() })
    })

    expect(screen.getByRole('textbox', { name: 'Mapping 1 pattern' })).toHaveValue(
      '*.edited.example.com',
    )
  })

  it('discards a stale host-mapping save after navigating to another connection', async () => {
    let saveStarted!: () => void
    let releaseSave!: () => void
    const started = new Promise<void>((resolve) => {
      saveStarted = resolve
    })
    const release = new Promise<void>((resolve) => {
      releaseSave = resolve
    })
    server.use(
      http.get('*/v1/admin/connections/:id/host-mappings', ({ params }) =>
        HttpResponse.json([
          {
            id: `mapping-${String(params.id)}`,
            pattern: `${String(params.id)}.example.com`,
            target: 'proxy.example.com',
            enabled: true,
          },
        ]),
      ),
      http.put('*/v1/admin/connections/:id/host-mappings', async ({ params, request }) => {
        const body = (await request.json()) as Array<{
          pattern: string
          target: string
          enabled: boolean
        }>
        if (params.id === 'conn-jira') {
          saveStarted()
          await release
        }
        return HttpResponse.json(
          body.map((mapping, index) => ({ ...mapping, id: `saved-${String(params.id)}-${index}` })),
        )
      }),
      ...connectionsReadHandlers([CONN_JIRA, CONN_KUBERNETES], []),
    )
    const user = userEvent.setup()
    const { router } = renderDetail('/admin/connections/conn-jira?tab=host-mappings')

    const pattern = await screen.findByRole('textbox', { name: 'Mapping 1 pattern' })
    await user.clear(pattern)
    await user.type(pattern, 'stale-a.example.com')
    await user.click(screen.getByRole('button', { name: 'Save mappings' }))
    await started

    await act(async () => {
      await router.navigate('/admin/connections/conn-kubernetes?tab=host-mappings')
    })
    expect(await screen.findByRole('textbox', { name: 'Mapping 1 pattern' })).toHaveValue(
      'conn-kubernetes.example.com',
    )

    releaseSave()
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(screen.getByRole('textbox', { name: 'Mapping 1 pattern' })).toHaveValue(
      'conn-kubernetes.example.com',
    )
  })

  it('shows kind as immutable and PATCHes the config form without it', async () => {
    const update = updateConnectionHandler(CONN_JIRA)
    server.use(...connectionsReadHandlers(), update.handler)
    const user = userEvent.setup()
    renderDetail()

    const form = await screen.findByRole('form', { name: 'Connection config' })
    expect(within(form).getByText('Kind (immutable)')).toBeInTheDocument()
    // No editable control for kind — only the chip.
    expect(within(form).queryByRole('combobox')).not.toBeInTheDocument()

    const nameInput = within(form).getByRole('textbox', { name: 'Name' })
    await user.clear(nameInput)
    await user.type(nameInput, 'jira-primary')
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(update.captured).toHaveLength(1))
    expect(update.captured[0]).toEqual({
      name: 'jira-primary',
      provider: 'jira',
      project_id: 'proj-alpha',
      base_url: 'https://jira.example.com',
      secret_ref: 'env:JIRA_API_TOKEN',
      options: { project_key: 'ALPHA' },
    })
    expect(update.captured[0]).not.toHaveProperty('kind')
  })

  it('serializes connection writes so delete and toggle cannot overlap a config save', async () => {
    let markSaveStarted!: () => void
    const saveStarted = new Promise<void>((resolve) => {
      markSaveStarted = resolve
    })
    let releaseSave!: () => void
    const saveRelease = new Promise<void>((resolve) => {
      releaseSave = resolve
    })
    server.use(
      ...connectionsReadHandlers(),
      http.patch('*/v1/admin/connections/:id', async () => {
        markSaveStarted()
        await saveRelease
        return HttpResponse.json({ ...CONN_JIRA, name: 'jira-saving' })
      }),
    )
    const user = userEvent.setup()
    renderDetail()

    const form = await screen.findByRole('form', { name: 'Connection config' })
    const name = within(form).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'jira-saving')
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))
    await saveStarted

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Toggle jira-prod' })).toBeDisabled()
    })

    releaseSave()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled())
  })

  it('keeps the list toggle locked until a detail PATCH finishes', async () => {
    let markSaveStarted!: () => void
    const saveStarted = new Promise<void>((resolve) => {
      markSaveStarted = resolve
    })
    let releaseSave!: () => void
    const saveRelease = new Promise<void>((resolve) => {
      releaseSave = resolve
    })
    const events: string[] = []
    server.use(
      ...connectionsReadHandlers(),
      http.patch('*/v1/admin/connections/:id', async () => {
        events.push('patch:start')
        markSaveStarted()
        await saveRelease
        events.push('patch:end')
        return HttpResponse.json({ ...CONN_JIRA, name: 'jira-updated' })
      }),
      http.post('*/v1/admin/connections/:id/disable', () => {
        events.push('toggle:start')
        return HttpResponse.json({ ...CONN_JIRA, enabled: false })
      }),
    )
    const user = userEvent.setup()
    renderDetail()

    const form = await screen.findByRole('form', { name: 'Connection config' })
    const name = within(form).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'jira-updated')
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))
    await saveStarted

    const breadcrumb = screen.getByRole('navigation', { name: 'Breadcrumb' })
    await user.click(within(breadcrumb).getByRole('link', { name: 'Connections' }))

    const toggle = await screen.findByRole('button', { name: 'Toggle jira-prod' })
    expect(toggle).toBeDisabled()
    await user.click(toggle)
    expect(events).toEqual(['patch:start'])

    releaseSave()
    await waitFor(() => expect(toggle).toBeEnabled())
    await user.click(toggle)
    await waitFor(() =>
      expect(events).toEqual(['patch:start', 'patch:end', 'toggle:start']),
    )
  })

  it('hides a probe result when a newer config write is queued behind it', async () => {
    let markProbeStarted!: () => void
    const probeStarted = new Promise<void>((resolve) => {
      markProbeStarted = resolve
    })
    let releaseProbe!: () => void
    const probeRelease = new Promise<void>((resolve) => {
      releaseProbe = resolve
    })
    let probeCalls = 0
    const events: string[] = []
    server.use(
      ...connectionsReadHandlers(),
      http.post('*/v1/admin/connections/:id/test', async () => {
        probeCalls += 1
        const call = probeCalls
        events.push(`probe:${call}:start`)
        if (call === 1) {
          markProbeStarted()
          await probeRelease
        }
        events.push(`probe:${call}:end`)
        return HttpResponse.json({
          ok: true,
          latency_ms: 12,
          detail: call === 1 ? 'old config' : 'fresh config',
        })
      }),
      http.patch('*/v1/admin/connections/:id', async () => {
        events.push('patch:start')
        events.push('patch:end')
        return HttpResponse.json({ ...CONN_JIRA, name: 'jira-updated' })
      }),
    )
    const user = userEvent.setup()
    renderDetail()

    await user.click(await screen.findByRole('button', { name: 'Test connection' }))
    await probeStarted

    const form = screen.getByRole('form', { name: 'Connection config' })
    const name = within(form).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'jira-updated')
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(within(form).getByRole('button', { name: 'Saving…' })).toBeDisabled(),
    )
    expect(events).toEqual(['probe:1:start'])
    expect(screen.queryByTestId('probe-result')).not.toBeInTheDocument()

    releaseProbe()
    await waitFor(() =>
      expect(events).toEqual([
        'probe:1:start',
        'probe:1:end',
        'patch:start',
        'patch:end',
      ]),
    )
    expect(screen.queryByTestId('probe-result')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    const panel = await screen.findByTestId('probe-result')
    expect(panel).toHaveTextContent('fresh config')
    expect(panel).not.toHaveTextContent('old config')
  })

  it('blocks scoped Kubernetes in-cluster identity before update', async () => {
    const update = updateConnectionHandler(CONN_KUBERNETES)
    server.use(...connectionsReadHandlers([CONN_KUBERNETES]), update.handler)
    const user = userEvent.setup()
    renderApp({
      initialEntries: ['/admin/connections/conn-kubernetes'],
      authState: authenticatedState('admin', 'Scoped Admin', [
        { project_id: 'proj-alpha', app_id: null },
      ]),
    })

    const form = await screen.findByRole('form', { name: 'Connection config' })
    expect(within(form).getByRole('combobox', { name: 'Project' })).toHaveValue('proj-alpha')
    expect(within(form).queryByRole('textbox', { name: 'Secret ref' })).not.toBeInTheDocument()
    const options = within(form).getByRole('textbox', { name: 'Options JSON' })
    await user.clear(options)
    await user.paste('{"auth_mode":"incluster"}')
    expect(within(form).getByRole('alert')).toHaveTextContent(
      'Kubernetes in-cluster authentication requires a global administrator.',
    )
    expect(within(form).getByRole('button', { name: 'Save changes' })).toBeDisabled()
    expect(update.captured).toHaveLength(0)
  })

  it('keeps runtime identity fields read-only and PATCHes only the mutable name', async () => {
    const update = updateConnectionHandler(CONN_ENGINE)
    server.use(...connectionsReadHandlers([CONN_ENGINE]), update.handler)
    const user = userEvent.setup()
    renderDetail('/admin/connections/conn-engine')

    const form = await screen.findByRole('form', { name: 'Connection config' })
    expect(within(form).getByRole('textbox', { name: 'Provider' })).toHaveAttribute('readonly')
    expect(within(form).getByRole('textbox', { name: 'Project' })).toHaveAttribute('readonly')
    expect(within(form).getByRole('textbox', { name: 'Base URL' })).toHaveAttribute('readonly')
    expect(within(form).getByRole('textbox', { name: 'Options JSON' })).toHaveAttribute('readonly')
    expect(within(form).getByText(/Runtime identity fields are immutable/)).toBeInTheDocument()

    const name = within(form).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'apex-load-primary')
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(update.captured).toEqual([{ name: 'apex-load-primary' }]))
  })

  it('resets config and closes a stale delete generation when the route id changes', async () => {
    let deleteStarted!: () => void
    let releaseDelete!: () => void
    const started = new Promise<void>((resolve) => {
      deleteStarted = resolve
    })
    const release = new Promise<void>((resolve) => {
      releaseDelete = resolve
    })
    const update = updateConnectionHandler(CONN_ENGINE)
    server.use(
      http.delete('*/v1/admin/connections/:id', async () => {
        deleteStarted()
        await release
        return new HttpResponse(null, { status: 204 })
      }),
      ...connectionsReadHandlers([CONN_JIRA, CONN_ENGINE]),
      update.handler,
    )
    const user = userEvent.setup()
    const { queryClient, router } = renderDetail()

    const firstForm = await screen.findByRole('form', { name: 'Connection config' })
    const firstName = within(firstForm).getByRole('textbox', { name: 'Name' })
    await user.clear(firstName)
    await user.type(firstName, 'stale-jira-name')
    queryClient.setQueryData(queryKeys.admin.connection(CONN_ENGINE.id), CONN_ENGINE)

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete connection jira-prod' })
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Type the connection name to confirm' }),
      'jira-prod',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Delete connection' }))
    await started

    await act(async () => {
      await router.navigate('/admin/connections/conn-engine')
    })
    const secondForm = await screen.findByRole('form', { name: 'Connection config' })
    expect(within(secondForm).getByRole('textbox', { name: 'Name' })).toHaveValue(
      'apex-load-default',
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    releaseDelete()
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(router.state.location.pathname).toBe('/admin/connections/conn-engine')

    await user.click(within(secondForm).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(update.captured).toEqual([{ name: 'apex-load-default' }]))
  })

  it('deletes only after the connection name is typed, then returns to the list', async () => {
    const del = deleteConnectionHandler()
    server.use(...connectionsReadHandlers(), del.handler)
    const user = userEvent.setup()
    const { router } = renderDetail()

    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete connection jira-prod' })
    const confirm = within(dialog).getByRole('button', { name: 'Delete connection' })
    expect(confirm).toBeDisabled()

    await user.type(
      within(dialog).getByRole('textbox', { name: 'Type the connection name to confirm' }),
      'jira-prod',
    )
    expect(confirm).toBeEnabled()
    await user.click(confirm)

    await waitFor(() => expect(del.captured).toEqual(['conn-jira']))
    await waitFor(() => expect(router.state.location.pathname).toBe('/admin/connections'))
  })
})
