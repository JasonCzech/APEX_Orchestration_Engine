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
  it('renders a compact connection registry with scope, status, and timestamps', async () => {
    server.use(...connectionsReadHandlers())
    renderList()

    const table = await screen.findByRole('table', { name: 'Connections' })
    const jiraRow = within(table).getByTestId('conn-row-conn-jira')
    expect(within(jiraRow).getByText('jira-prod')).toBeInTheDocument()
    expect(within(jiraRow).getByText('work tracking')).toBeInTheDocument()
    expect(within(jiraRow).getByText('jira')).toHaveClass('dash-context-chip')
    expect(within(jiraRow).getByText('proj-alpha')).toBeInTheDocument()
    expect(within(jiraRow).getByRole('button', { name: 'Toggle jira-prod' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    // Global (no project) connection renders the 'global' chip; disabled pill is off.
    const elkRow = within(table).getByTestId('conn-row-conn-elk')
    expect(within(elkRow).getByText('global')).toBeInTheDocument()
    expect(within(elkRow).getByRole('button', { name: 'Toggle elk-global' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )

    expect(screen.getByRole('searchbox', { name: 'Filter connections' })).toBeInTheDocument()
  })

  it('filters the registry by name, kind, provider, or project', async () => {
    server.use(...connectionsReadHandlers())
    const user = userEvent.setup()
    renderList()

    await screen.findByRole('table', { name: 'Connections' })
    await user.type(screen.getByRole('searchbox', { name: 'Filter connections' }), 'elk')
    expect(screen.getByTestId('conn-row-conn-elk')).toBeInTheDocument()
    expect(screen.queryByTestId('conn-row-conn-jira')).not.toBeInTheDocument()
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
    expect(
      within(panel).getByText('env:NAME — references only, never raw secrets'),
    ).toBeInTheDocument()

    // A provider the catalog does not know still has to be reachable.
    await user.selectOptions(within(panel).getByRole('combobox', { name: 'Provider' }), 'Custom…')
    expect(within(panel).getByText('must be a registered provider')).toBeInTheDocument()
    await user.type(within(panel).getByRole('textbox', { name: 'Provider name' }), 'rally')
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
    await user.selectOptions(within(panel).getByRole('combobox', { name: 'Provider' }), 'jira')
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'jira-project')
    await user.selectOptions(within(panel).getByRole('combobox', { name: 'Project' }), 'proj-alpha')
    await user.type(
      within(panel).getByRole('textbox', { name: 'Base URL' }),
      'https://jira.project.example.com',
    )
    await user.type(within(panel).getByRole('textbox', { name: 'User email' }), 'qa@example.com')
    await user.click(within(panel).getByRole('button', { name: 'Create connection' }))

    await waitFor(() => expect(create.captured).toHaveLength(1))
    expect(create.captured[0]).toMatchObject({
      project_id: 'proj-alpha',
      base_url: 'https://jira.project.example.com',
      secret_ref: null,
      // Adapters read base_url out of options, so the guided field fills both.
      options: {
        base_url: 'https://jira.project.example.com',
        user_email: 'qa@example.com',
      },
    })
  })

  it('requires a provider-specific field before the guided form can submit', async () => {
    server.use(...connectionsReadHandlers([]))
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'New connection' }))
    const panel = screen.getByRole('form', { name: 'New connection' })
    await user.selectOptions(within(panel).getByRole('combobox', { name: 'Provider' }), 'jira')
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'jira-prod')
    await user.type(
      within(panel).getByRole('textbox', { name: 'Base URL' }),
      'https://acme.atlassian.net',
    )

    expect(within(panel).getByRole('alert')).toHaveTextContent(
      'Jira Cloud requires user email.',
    )
    expect(within(panel).getByRole('button', { name: 'Create connection' })).toBeDisabled()
  })

  it('offers Jira Align with an auth style that reveals the Atlassian email field', async () => {
    const create = createConnectionHandler()
    server.use(...connectionsReadHandlers([]), create.handler)
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'New connection' }))
    const panel = screen.getByRole('form', { name: 'New connection' })
    await user.selectOptions(
      within(panel).getByRole('combobox', { name: 'Provider' }),
      'jira_align',
    )
    // A Jira Align API token is a bearer credential — no email involved.
    expect(within(panel).queryByRole('textbox', { name: 'User email' })).not.toBeInTheDocument()
    expect(within(panel).getByRole('textbox', { name: 'Secret ref' })).toHaveAttribute(
      'placeholder',
      'env:APEX_INTEGRATION_JIRA_ALIGN_TOKEN',
    )

    await user.selectOptions(
      within(panel).getByRole('combobox', { name: 'Authentication' }),
      'basic',
    )
    await user.type(within(panel).getByRole('textbox', { name: 'User email' }), 'qa@example.com')
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'align-prod')
    await user.type(
      within(panel).getByRole('textbox', { name: 'Base URL' }),
      'https://acme.jiraalign.com',
    )
    await user.type(within(panel).getByRole('textbox', { name: 'Program id' }), '42')
    await user.click(within(panel).getByRole('button', { name: 'Create connection' }))

    await waitFor(() => expect(create.captured).toHaveLength(1))
    expect(create.captured[0]).toMatchObject({
      provider: 'jira_align',
      base_url: 'https://acme.jiraalign.com',
      options: {
        base_url: 'https://acme.jiraalign.com',
        user_email: 'qa@example.com',
        program_id: '42',
      },
    })
  })

  it('survives a detour through the provider placeholder without losing values', async () => {
    server.use(...connectionsReadHandlers([]))
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'New connection' }))
    const panel = screen.getByRole('form', { name: 'New connection' })
    const providerSelect = within(panel).getByRole('combobox', { name: 'Provider' })
    await user.selectOptions(providerSelect, 'jira')
    await user.type(
      within(panel).getByRole('textbox', { name: 'Base URL' }),
      'https://acme.atlassian.net',
    )
    await user.type(within(panel).getByRole('textbox', { name: 'User email' }), 'qa@example.com')

    await user.selectOptions(providerSelect, '')
    await user.selectOptions(providerSelect, 'jira')

    expect(within(panel).getByRole('textbox', { name: 'Base URL' })).toHaveValue(
      'https://acme.atlassian.net',
    )
    expect(within(panel).getByRole('textbox', { name: 'User email' })).toHaveValue(
      'qa@example.com',
    )
  })

  it('keeps unparsable raw-JSON edits when switching to another catalog provider', async () => {
    server.use(...connectionsReadHandlers([]))
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'New connection' }))
    const panel = screen.getByRole('form', { name: 'New connection' })
    const providerSelect = within(panel).getByRole('combobox', { name: 'Provider' })
    await user.selectOptions(providerSelect, 'jira')
    await user.click(within(panel).getByRole('button', { name: 'Edit as JSON' }))
    const jsonBox = within(panel).getByRole('textbox', { name: 'Options JSON' })
    await user.clear(jsonBox)
    await user.paste('{"base_url": "https://half-typed')

    await user.selectOptions(providerSelect, 'jira_align')

    // The switch happens, but the live (invalid) edit is not regenerated over.
    expect(within(panel).getByRole('textbox', { name: 'Options JSON' })).toHaveValue(
      '{"base_url": "https://half-typed',
    )
  })

  it('keeps live raw-JSON edits when switching to a custom provider', async () => {
    server.use(...connectionsReadHandlers([]))
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'New connection' }))
    const panel = screen.getByRole('form', { name: 'New connection' })
    const providerSelect = within(panel).getByRole('combobox', { name: 'Provider' })
    await user.selectOptions(providerSelect, 'jira')
    await user.click(within(panel).getByRole('button', { name: 'Edit as JSON' }))
    const jsonBox = within(panel).getByRole('textbox', { name: 'Options JSON' })
    await user.clear(jsonBox)
    await user.paste('{"webhook_secret_ref": "env:APEX_INTEGRATION_HOOK"}')

    await user.selectOptions(providerSelect, 'Custom…')

    expect(within(panel).getByRole('textbox', { name: 'Options JSON' })).toHaveValue(
      '{"webhook_secret_ref": "env:APEX_INTEGRATION_HOOK"}',
    )
  })

  it('keeps shared field values when switching between catalog providers', async () => {
    server.use(...connectionsReadHandlers([]))
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'New connection' }))
    const panel = screen.getByRole('form', { name: 'New connection' })
    await user.selectOptions(within(panel).getByRole('combobox', { name: 'Provider' }), 'jira')
    await user.type(
      within(panel).getByRole('textbox', { name: 'Base URL' }),
      'https://acme.example.com',
    )
    await user.type(within(panel).getByRole('textbox', { name: 'User email' }), 'qa@example.com')

    await user.selectOptions(
      within(panel).getByRole('combobox', { name: 'Provider' }),
      'jira_align',
    )

    expect(within(panel).getByRole('textbox', { name: 'Base URL' })).toHaveValue(
      'https://acme.example.com',
    )
    // A stored email means basic auth, so the switch infers the Atlassian style.
    expect(within(panel).getByRole('combobox', { name: 'Authentication' })).toHaveValue('basic')
    expect(within(panel).getByRole('textbox', { name: 'User email' })).toHaveValue(
      'qa@example.com',
    )
  })

  it('drops an option belonging to a field the current auth style hides', async () => {
    const create = createConnectionHandler()
    server.use(...connectionsReadHandlers([]), create.handler)
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'New connection' }))
    const panel = screen.getByRole('form', { name: 'New connection' })
    await user.selectOptions(
      within(panel).getByRole('combobox', { name: 'Provider' }),
      'jira_align',
    )
    const auth = within(panel).getByRole('combobox', { name: 'Authentication' })
    await user.selectOptions(auth, 'basic')
    await user.type(within(panel).getByRole('textbox', { name: 'User email' }), 'qa@example.com')
    // Switching back to bearer must not leave the email behind: its presence is
    // what selects basic auth on the server side.
    await user.selectOptions(auth, 'bearer')
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'align-prod')
    await user.type(
      within(panel).getByRole('textbox', { name: 'Base URL' }),
      'https://acme.jiraalign.com',
    )
    await user.click(within(panel).getByRole('button', { name: 'Create connection' }))

    await waitFor(() => expect(create.captured).toHaveLength(1))
    expect(create.captured[0]?.options).toEqual({ base_url: 'https://acme.jiraalign.com' })
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
    await user.type(within(panel).getByRole('textbox', { name: 'Provider name' }), 'kubernetes')
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
