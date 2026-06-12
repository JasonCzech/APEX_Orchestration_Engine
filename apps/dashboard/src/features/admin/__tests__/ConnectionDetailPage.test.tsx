import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  connectionsReadHandlers,
  deleteConnectionHandler,
  probeHandler,
  putHostMappingsHandler,
  updateConnectionHandler,
  CONN_JIRA,
} from './adminTestHandlers'

function renderDetail(entry = '/admin/connections/conn-jira') {
  return renderApp({
    initialEntries: [entry],
    authState: authenticatedState('admin'),
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
