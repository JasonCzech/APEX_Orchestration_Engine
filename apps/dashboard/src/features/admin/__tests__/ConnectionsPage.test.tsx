import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  CONN_ELK,
  CONN_JIRA,
  connectionsReadHandlers,
  createConnectionHandler,
  toggleConnectionHandlers,
} from './adminTestHandlers'

function renderList(role: 'admin' | 'operator' = 'admin') {
  return renderApp({
    initialEntries: ['/admin/connections'],
    authState: role === 'admin' ? authenticatedState(role, 'Dash Ops', []) : authenticatedState(role),
  })
}

describe('ConnectionsPage', () => {
  it('groups connection cards by kind with provider/project chips and timestamps', async () => {
    server.use(...connectionsReadHandlers())
    renderList()

    const workTracking = await screen.findByRole('region', { name: 'Kind Work tracking' })
    const jiraCard = within(workTracking).getByTestId('conn-card-conn-jira')
    expect(within(jiraCard).getByText('jira-prod')).toBeInTheDocument()
    expect(within(jiraCard).getByText('jira')).toHaveClass('dash-context-chip')
    expect(within(jiraCard).getByText('proj-alpha')).toBeInTheDocument()
    expect(within(jiraCard).getByRole('button', { name: 'Toggle jira-prod' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    // Global (no project) connection renders the 'global' chip; disabled pill is off.
    const logSearch = screen.getByRole('region', { name: 'Kind Log search' })
    const elkCard = within(logSearch).getByTestId('conn-card-conn-elk')
    expect(within(elkCard).getByText('global')).toBeInTheDocument()
    expect(within(elkCard).getByRole('button', { name: 'Toggle elk-global' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )

    expect(screen.getByRole('region', { name: 'Kind Execution engine' })).toBeInTheDocument()
    // Empty kinds drop out entirely.
    expect(screen.queryByRole('region', { name: 'Kind Secrets' })).not.toBeInTheDocument()
  })

  it('exposes connection details through a keyboard-focusable link', async () => {
    server.use(...connectionsReadHandlers())
    const user = userEvent.setup()
    const { router } = renderList()

    const link = await screen.findByRole('link', { name: 'jira-prod' })
    expect(link).toHaveAttribute('href', '/admin/connections/conn-jira')

    link.focus()
    await user.keyboard('{Enter}')

    await waitFor(() =>
      expect(router.state.location.pathname).toBe('/admin/connections/conn-jira'),
    )
  })

  it('surfaces the 422 registered-provider list inline when create is rejected', async () => {
    const create = createConnectionHandler({ registered: ['jira', 'azure_devops'] })
    server.use(...connectionsReadHandlers(), create.handler)
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'New connection' }))
    const panel = screen.getByRole('form', { name: 'New connection' })
    expect(within(panel).getByText('must be a registered provider')).toBeInTheDocument()
    expect(
      within(panel).getByText('env:NAME — references only, never raw secrets'),
    ).toBeInTheDocument()

    await user.type(within(panel).getByRole('textbox', { name: 'Provider' }), 'rally')
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'rally-test')
    await user.click(within(panel).getByRole('button', { name: 'Create connection' }))

    // Inline problem detail names the registered providers — not a toast.
    const alert = await within(panel).findByRole('alert')
    expect(alert).toHaveTextContent(
      "unknown provider 'rally' for kind 'work_tracking'; registered providers: jira, azure_devops",
    )
    expect(create.captured).toHaveLength(1)
    expect(create.captured[0]).toMatchObject({ kind: 'work_tracking', provider: 'rally' })
    // The panel stays open for a corrected retry.
    expect(screen.getByRole('form', { name: 'New connection' })).toBeInTheDocument()
  })

  it('lets a scoped admin submit an authorized public base URL without exposing secrets', async () => {
    const create = createConnectionHandler()
    server.use(...connectionsReadHandlers([]), create.handler)
    const user = userEvent.setup()
    renderApp({
      initialEntries: ['/admin/connections'],
      authState: authenticatedState('admin', 'Scoped Admin', [
        { project_id: 'proj-alpha', app_id: null },
      ]),
    })

    await user.click(await screen.findByRole('button', { name: 'New connection' }))
    const panel = screen.getByRole('form', { name: 'New connection' })
    expect(within(panel).queryByRole('textbox', { name: 'Secret ref' })).not.toBeInTheDocument()
    await user.type(within(panel).getByRole('textbox', { name: 'Provider' }), 'jira')
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'jira-project')
    await user.selectOptions(within(panel).getByRole('combobox', { name: 'Project' }), 'proj-alpha')
    await user.type(
      within(panel).getByRole('textbox', { name: 'Base URL' }),
      'https://jira.project.example.com',
    )
    await user.click(within(panel).getByRole('button', { name: 'Create connection' }))

    await waitFor(() => expect(create.captured).toHaveLength(1))
    expect(create.captured[0]).toMatchObject({
      project_id: 'proj-alpha',
      base_url: 'https://jira.project.example.com',
      secret_ref: null,
    })
  })

  it('blocks scoped Kubernetes in-cluster identity before create', async () => {
    server.use(...connectionsReadHandlers([]))
    const user = userEvent.setup()
    renderApp({
      initialEntries: ['/admin/connections'],
      authState: authenticatedState('admin', 'Scoped Admin', [
        { project_id: 'proj-alpha', app_id: null },
      ]),
    })

    await user.click(await screen.findByRole('button', { name: 'New connection' }))
    const panel = screen.getByRole('form', { name: 'New connection' })
    await user.selectOptions(
      within(panel).getByRole('combobox', { name: 'Kind' }),
      'cluster_inventory',
    )
    await user.type(within(panel).getByRole('textbox', { name: 'Provider' }), 'kubernetes')
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'in-cluster')
    await user.selectOptions(within(panel).getByRole('combobox', { name: 'Project' }), 'proj-alpha')
    const options = within(panel).getByRole('textbox', { name: 'Options JSON' })
    await user.clear(options)
    await user.paste('{"auth_mode":"in_cluster"}')

    expect(within(panel).getByRole('alert')).toHaveTextContent(
      'Kubernetes in-cluster authentication requires a global administrator.',
    )
    expect(within(panel).getByRole('button', { name: 'Create connection' })).toBeDisabled()
  })

  it('flips the card toggle pill through the disable endpoint', async () => {
    const toggles = toggleConnectionHandlers(CONN_JIRA)
    server.use(...connectionsReadHandlers(), ...toggles.handlers)
    const user = userEvent.setup()
    const { router } = renderList()

    const pill = await screen.findByRole('button', { name: 'Toggle jira-prod' })
    await user.click(pill)

    await waitFor(() => expect(toggles.calls).toEqual(['disable']))
    // The pill click must not trigger the card's navigation.
    expect(router.state.location.pathname).toBe('/admin/connections')
  })

  it('enables a disabled connection through the enable endpoint', async () => {
    const toggles = toggleConnectionHandlers(CONN_ELK)
    server.use(...connectionsReadHandlers(), ...toggles.handlers)
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'Toggle elk-global' }))
    await waitFor(() => expect(toggles.calls).toEqual(['enable']))
  })

  it("shows the 'Requires admin role' empty state to non-admins", async () => {
    server.use(...connectionsReadHandlers())
    renderList('operator')

    expect(await screen.findByRole('heading', { name: 'Requires admin role' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New connection' })).not.toBeInTheDocument()
  })
})
