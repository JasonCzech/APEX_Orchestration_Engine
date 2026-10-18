import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { Environment } from '@/api/hooks/useEnvironments'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  APPS_FIXTURE,
  ENV_PROD,
  ENV_STAGING,
  ENVS_FIXTURE,
  catalogReadHandlers,
  createApplicationHandler,
  createEnvironmentHandler,
  deleteEnvironmentHandler,
  inventoryHandler,
  inventoryOf,
} from './environmentsTestHandlers'

function renderList(role: 'admin' | 'operator' | 'viewer' = 'operator') {
  return renderApp({
    initialEntries: ['/environments'],
    authState: role === 'admin' ? authenticatedState(role, 'Dash Ops', []) : authenticatedState(role),
  })
}

describe('EnvironmentsPage', () => {
  it('groups environments under application headers with project captions', async () => {
    server.use(...catalogReadHandlers())
    renderList()

    const checkout = await screen.findByRole('region', { name: 'Application Checkout' })
    expect(within(checkout).getByText('proj-alpha').closest('.env-group-project')).not.toBeNull()

    const staging = within(checkout).getByTestId(`env-row-${ENV_STAGING.id}`)
    expect(within(staging).getByText('staging')).toBeInTheDocument()
    expect(within(staging).getByText('k8s')).toHaveClass('dash-context-chip')
    expect(within(staging).getByText('https://staging.checkout.example.com')).toHaveClass(
      'env-base-url',
    )
    expect(within(staging).getByText('1')).toBeInTheDocument() // host count
    expect(within(checkout).getByTestId('env-row-env-prod')).toBeInTheDocument()

    // Second application group, with a missing base_url rendered as an em dash.
    const search = screen.getByRole('region', { name: 'Application Search' })
    const dev = within(search).getByTestId('env-row-env-search-dev')
    expect(within(dev).getByText('vm')).toHaveClass('dash-context-chip')
    expect(within(dev).getByText('—')).toBeInTheDocument()
  })

  it('opens an application workspace and searches its environments', async () => {
    server.use(...catalogReadHandlers())
    const user = userEvent.setup()
    const rendered = renderList()
    await user.click(await screen.findByRole('link', { name: 'Checkout' }))
    expect(rendered.router.state.location.search).toBe('?application=app-checkout')
    expect(screen.queryByRole('region', { name: 'Application Search' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New environment' })).not.toBeInTheDocument()
    await user.type(screen.getByRole('textbox', { name: 'Search applications and environments' }), 'does not exist')
    expect(screen.getByRole('heading', { name: 'No matching applications' })).toBeInTheDocument()
    expect(screen.getByText('1 active filter')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(screen.queryByText('1 active filter')).not.toBeInTheDocument()
    await user.clear(screen.getByRole('textbox', { name: 'Search applications and environments' }))
    await user.click(screen.getByRole('link', { name: 'All applications' }))
    expect(screen.getByRole('region', { name: 'Application Search' })).toBeInTheDocument()
  })

  it('keeps applications without environments visible with a direct add action', async () => {
    server.use(...catalogReadHandlers(APPS_FIXTURE, [ENV_STAGING, ENV_PROD]))
    const user = userEvent.setup()
    renderList()

    const search = await screen.findByRole('region', { name: 'Application Search' })
    expect(within(search).getByText('No environments configured.')).toBeInTheDocument()
    await user.click(within(search).getByRole('button', { name: 'Add first environment' }))

    const panel = screen.getByRole('form', { name: 'New environment' })
    expect(within(panel).getByRole('combobox', { name: 'Application' })).toHaveValue('app-search')
  })

  it('clears a missing application scope from the filtered empty state while retaining other query parameters', async () => {
    server.use(...catalogReadHandlers())
    const user = userEvent.setup()
    const { router } = renderApp({
      initialEntries: ['/environments?application=missing&source=bookmark'],
      authState: authenticatedState('operator'),
    })

    expect(await screen.findByRole('heading', { name: 'No matching applications' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Show all applications' }))

    expect(router.state.location.search).toBe('?source=bookmark')
    expect(await screen.findByRole('region', { name: 'Application Checkout' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Application Search' })).toBeInTheDocument()
  })

  it('turns the empty state into an application creation path', async () => {
    server.use(...catalogReadHandlers([], []))
    const user = userEvent.setup()
    renderList('admin')

    expect(await screen.findByRole('heading', { name: 'No applications yet' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New environment' })).not.toBeInTheDocument()
    await user.click(screen.getAllByRole('button', { name: 'New application' }).at(-1)!)
    expect(screen.getByRole('form', { name: 'New application' })).toBeInTheDocument()
  })

  it('does not offer application creation to an operator without project-wide scope', async () => {
    server.use(...catalogReadHandlers([], []))
    renderApp({
      initialEntries: ['/environments'],
      authState: authenticatedState('operator', 'Unscoped Operator', []),
    })

    expect(await screen.findByRole('heading', { name: 'No applications yet' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New application' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create application' })).not.toBeInTheDocument()
    expect(screen.getByText('Application creation requires project-wide access.')).toBeInTheDocument()
  })

  it('keeps application and environment creation mutually exclusive', async () => {
    server.use(...catalogReadHandlers())
    const user = userEvent.setup()
    renderList('admin')

    await user.click(await screen.findByRole('button', { name: 'New application' }))
    expect(screen.getByRole('form', { name: 'New application' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New environment' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add environment' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add first environment' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(within(screen.getByRole('region', { name: 'Application Checkout' })).getByRole('button', { name: 'Add environment' }))
    expect(screen.getByRole('form', { name: 'New environment' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New application' })).not.toBeInTheDocument()
  })

  it('creates an application and continues into environment setup', async () => {
    const create = createApplicationHandler()
    server.use(...catalogReadHandlers(), create.handler)
    const user = userEvent.setup()
    renderList('admin')

    await user.click(await screen.findByRole('button', { name: 'New application' }))
    const panel = screen.getByRole('form', { name: 'New application' })
    await user.type(within(panel).getByRole('textbox', { name: 'Project' }), 'proj-beta')
    await user.type(within(panel).getByRole('textbox', { name: 'Application name' }), 'Orders')
    await user.type(within(panel).getByRole('textbox', { name: 'Application description' }), 'Order services')
    await user.click(within(panel).getByRole('button', { name: 'Create application' }))

    await waitFor(() => expect(create.captured).toEqual([{
      project_id: 'proj-beta',
      name: 'Orders',
      description: 'Order services',
    }]))
    expect(await screen.findByRole('form', { name: 'New environment' })).toBeInTheDocument()
  })

  it('exposes environment details through a keyboard-focusable link', async () => {
    server.use(...catalogReadHandlers(), inventoryHandler(inventoryOf(ENV_STAGING.id, null)))
    const user = userEvent.setup()
    const { router } = renderList()

    const link = await screen.findByRole('link', { name: 'staging' })
    expect(link).toHaveAttribute('href', `/environments/${ENV_STAGING.id}`)

    link.focus()
    await user.keyboard('{Enter}')

    await waitFor(() =>
      expect(router.state.location.pathname).toBe(`/environments/${ENV_STAGING.id}`),
    )
  })

  it('creates an environment from the panel and navigates to its detail', async () => {
    const create = createEnvironmentHandler('env-new')
    const ENV_NEW: Environment = {
      id: 'env-new',
      application_id: 'app-checkout',
      name: 'perf',
      kind: 'k8s',
      base_url: 'https://perf.checkout.example.com',
      target_approved: true,
      target_version: 1,
      hosts: [{ id: 'host-new-0', hostname: 'perf-node-1', role: 'worker' }],
      options: { namespace: 'checkout-perf' },
      created_at: '2026-06-12T12:00:00Z',
      updated_at: '2026-06-12T12:00:00Z',
    }
    server.use(
      ...catalogReadHandlers(APPS_FIXTURE, [...ENVS_FIXTURE, ENV_NEW]),
      create.handler,
      inventoryHandler(inventoryOf('env-new', null)),
    )
    const user = userEvent.setup()
    const { router } = renderList('admin')

    await user.click(within(await screen.findByRole('region', { name: 'Application Checkout' })).getByRole('button', { name: 'Add environment' }))
    const panel = screen.getByRole('form', { name: 'New environment' })
    await user.selectOptions(
      within(panel).getByRole('combobox', { name: 'Application' }),
      'app-checkout',
    )
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'perf')
    await user.type(
      within(panel).getByRole('textbox', { name: 'Base URL' }),
      'https://perf.checkout.example.com',
    )
    await user.click(within(panel).getByRole('button', { name: 'Add host' }))
    await user.type(within(panel).getByRole('textbox', { name: 'Host 1 hostname' }), 'perf-node-1')
    await user.type(within(panel).getByRole('textbox', { name: 'Host 1 role' }), 'worker')
    const options = within(panel).getByRole('textbox', { name: 'Options JSON' })
    await user.clear(options)
    await user.paste('{"namespace": "checkout-perf"}')
    await user.click(within(panel).getByRole('button', { name: 'Create environment' }))

    await waitFor(() => expect(router.state.location.pathname).toBe('/environments/env-new'))
    expect(create.captured).toEqual([
      {
        application_id: 'app-checkout',
        name: 'perf',
        kind: 'k8s',
        base_url: 'https://perf.checkout.example.com',
        hosts: [{ hostname: 'perf-node-1', role: 'worker' }],
        options: { namespace: 'checkout-perf' },
      },
    ])
    // Detail screen renders the new environment.
    expect(await screen.findByRole('heading', { name: 'perf' })).toBeInTheDocument()
  })

  it('requires typing the environment name before delete fires', async () => {
    const del = deleteEnvironmentHandler()
    server.use(...catalogReadHandlers(), del.handler)
    const user = userEvent.setup()
    renderList()

    const row = await screen.findByTestId(`env-row-${ENV_STAGING.id}`)
    await user.click(within(row).getByRole('button', { name: 'Environment actions: staging' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete…' }))

    const dialog = await screen.findByRole('dialog', { name: 'Delete environment staging' })
    const confirm = within(dialog).getByRole('button', { name: 'Delete environment' })
    const input = within(dialog).getByRole('textbox', {
      name: 'Type the environment name to confirm',
    })
    expect(confirm).toBeDisabled()
    await user.type(input, 'stagin') // near miss stays disabled
    expect(confirm).toBeDisabled()
    expect(del.captured).toHaveLength(0)

    await user.type(input, 'g')
    expect(confirm).toBeEnabled()
    await user.click(confirm)

    await waitFor(() => expect(del.captured).toEqual([ENV_STAGING.id]))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('hides every mutation entry point from viewers', async () => {
    server.use(...catalogReadHandlers())
    const user = userEvent.setup()
    renderList('viewer')

    const row = await screen.findByTestId(`env-row-${ENV_STAGING.id}`)
    expect(screen.queryByRole('button', { name: 'New environment' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New application' })).not.toBeInTheDocument()
    expect(screen.getByText('Your role has read-only access to applications and environments.')).toBeInTheDocument()

    await user.click(within(row).getByRole('button', { name: 'Environment actions: staging' }))
    expect(screen.getByRole('menuitem', { name: 'Open' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Delete…' })).not.toBeInTheDocument()
  })
})
