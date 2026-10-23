import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'

import type { Application, Environment } from '@/api/hooks/useEnvironments'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  APP_CHECKOUT,
  APP_SEARCH,
  APPS_FIXTURE,
  ENV_PROD,
  ENV_SEARCH_DEV,
  ENV_STAGING,
  ENVS_FIXTURE,
  applicationWriteHandlers,
  catalogReadHandlers,
  createApplicationHandler,
  createEnvironmentHandler,
  deleteEnvironmentHandler,
  inventoryHandler,
  inventoryOf,
} from './environmentsTestHandlers'

function renderList(role: 'admin' | 'operator' | 'viewer' = 'operator', entry = '/environments') {
  return renderApp({
    initialEntries: [entry],
    authState: role === 'admin' ? authenticatedState(role, 'Dash Ops', []) : authenticatedState(role),
  })
}

const ENV_UNAPPROVED: Environment = {
  ...ENV_PROD,
  id: 'env-perf',
  name: 'perf',
  base_url: 'https://perf.checkout.example.com/load',
  target_approved: false,
  hosts: [],
}

afterEach(() => {
  window.localStorage.clear()
})

describe('EnvironmentsPage', () => {
  it('lists each application once with the projects that use it, and whether each environment is ready', async () => {
    server.use(...catalogReadHandlers())
    renderList()

    const checkout = await screen.findByRole('region', { name: 'Application Checkout' })
    expect(within(within(checkout).getByRole('list', { name: 'Projects using Checkout' })).getAllByRole('link').map(link => link.textContent)).toEqual(['proj-alpha'])
    // A shared application appears once, linked to both projects; its environments aren't copied per project.
    const search = screen.getByRole('region', { name: 'Application Search' })
    expect(within(within(search).getByRole('list', { name: 'Projects using Search' })).getAllByRole('link').map(link => link.getAttribute('href')))
      .toEqual(['/environments?project=proj-alpha', '/environments?project=proj-beta'])
    expect(screen.getAllByTestId('env-row-env-search-dev')).toHaveLength(1)
    expect(within(checkout).getByText('Payment funnel services')).toBeInTheDocument()

    const staging = within(checkout).getByTestId(`env-row-${ENV_STAGING.id}`)
    expect(within(staging).getByText('Approved')).toHaveClass('status-badge')
    expect(within(staging).getByText('staging.checkout.example.com')).toHaveAttribute('title', 'https://staging.checkout.example.com')
    expect(within(staging).getByText('Kubernetes')).toBeInTheDocument()
    expect(within(staging).getByText('1')).toHaveAttribute('title', 'stg-node-1')
    expect(within(staging).getByText('Never')).toBeInTheDocument()

    const dev = within(screen.getByRole('region', { name: 'Application Search' })).getByTestId('env-row-env-search-dev')
    expect(within(dev).getByText('No target')).toBeInTheDocument()
    expect(within(dev).getByText('Virtual machines')).toBeInTheDocument()
    expect(document.querySelector('.env-summary')).toHaveTextContent('2 applications · 3 environments')
  })

  it('narrows to one project and names it in a project bar', async () => {
    server.use(...catalogReadHandlers())
    renderList('operator', '/environments?project=proj-beta')

    expect(await screen.findByRole('region', { name: 'Application Search' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: /^Project .*proj-beta/ })).toHaveTextContent('1 application used by this project')
    expect(screen.queryByRole('region', { name: 'Application Checkout' })).not.toBeInTheDocument()
  })

  it('filters by target status from the summary and keeps filters in the URL', async () => {
    server.use(...catalogReadHandlers(APPS_FIXTURE, [...ENVS_FIXTURE, ENV_UNAPPROVED]))
    const user = userEvent.setup()
    const { router } = renderList()

    await user.click(await screen.findByRole('button', { name: '1 need target approval' }))
    expect(router.state.location.search).toBe('?target=needs-approval')
    expect(screen.getByTestId('env-row-env-perf')).toBeInTheDocument()
    expect(screen.queryByTestId(`env-row-${ENV_STAGING.id}`)).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Application Search' })).not.toBeInTheDocument()
    expect(screen.getByText('1 active filter')).toBeInTheDocument()
  })

  it('narrows search to matching environments and clears every filter at once', async () => {
    server.use(...catalogReadHandlers())
    const user = userEvent.setup()
    const { router } = renderList()

    await user.click(await screen.findByRole('link', { name: 'Checkout' }))
    expect(router.state.location.search).toBe('?application=app-checkout')
    expect(screen.queryByRole('region', { name: 'Application Search' })).not.toBeInTheDocument()
    expect(screen.getByText(/Showing Checkout only/)).toBeInTheDocument()

    await user.type(screen.getByRole('searchbox', { name: 'Search applications and environments' }), 'prod')
    expect(router.state.location.search).toContain('q=prod')
    const checkout = screen.getByRole('region', { name: 'Application Checkout' })
    expect(within(checkout).getByTestId(`env-row-${ENV_PROD.id}`)).toBeInTheDocument()
    expect(within(checkout).queryByTestId(`env-row-${ENV_STAGING.id}`)).not.toBeInTheDocument()
    expect(screen.getByText('2 active filters')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(router.state.location.search).toBe('')
    expect(screen.getByRole('region', { name: 'Application Search' })).toBeInTheDocument()

    // Without an application filter the summary says how many rows the search left.
    await user.type(screen.getByRole('searchbox', { name: 'Search applications and environments' }), 'staging')
    expect(document.querySelector('.env-summary')).toHaveTextContent('1 environment matches')
  })

  it('opens the new environment form inside the application and focuses it', async () => {
    server.use(...catalogReadHandlers(APPS_FIXTURE, [ENV_STAGING, ENV_PROD]))
    const user = userEvent.setup()
    renderList()

    const search = await screen.findByRole('region', { name: 'Application Search' })
    expect(within(search).getByText('No environments yet.')).toBeInTheDocument()
    // One add action for an empty application, not two.
    expect(within(search).getAllByRole('button', { name: /Add/ })).toHaveLength(1)
    await user.click(within(search).getByRole('button', { name: 'Add first environment' }))

    const panel = within(search).getByRole('form', { name: 'New environment' })
    expect(within(panel).getByRole('heading', { name: 'New environment for Search' })).toBeInTheDocument()
    expect(within(panel).getByRole('textbox', { name: 'Name' })).toHaveFocus()
    // Operators can't set the target; the form says who does.
    expect(within(panel).queryByRole('textbox', { name: 'Execution target URL' })).not.toBeInTheDocument()
    expect(panel).toHaveTextContent('A global administrator sets and approves the execution target URL')
    // Other applications can still start their own form.
    expect(within(screen.getByRole('region', { name: 'Application Checkout' })).getByRole('button', { name: 'Add environment' })).toBeEnabled()

    // Cancel closes the form and returns focus to the button that opened it.
    await user.click(within(panel).getByRole('button', { name: 'Cancel' }))
    expect(within(search).getByRole('button', { name: 'Add first environment' })).toHaveFocus()
  })

  it('clears a missing application scope from the filtered empty state while retaining other query parameters', async () => {
    server.use(...catalogReadHandlers())
    const user = userEvent.setup()
    const { router } = renderList('operator', '/environments?application=missing&source=bookmark')

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
    await user.click(screen.getAllByRole('button', { name: 'New application' }).at(-1)!)
    expect(screen.getByRole('dialog', { name: 'New application' })).toBeInTheDocument()
  })

  it('does not offer application creation to an operator without project-wide scope', async () => {
    server.use(...catalogReadHandlers([], []))
    renderApp({ initialEntries: ['/environments'], authState: authenticatedState('operator', 'Unscoped Operator', []) })

    expect(await screen.findByRole('heading', { name: 'No applications yet' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New application' })).not.toBeInTheDocument()
    expect(screen.getByText('Application creation requires project-wide access.')).toBeInTheDocument()
  })

  it('creates an application and continues into its first environment', async () => {
    const apps: Application[] = [...APPS_FIXTURE]
    const create = createApplicationHandler('app-new', apps)
    server.use(...catalogReadHandlers(apps), create.handler)
    const user = userEvent.setup()
    renderList('admin')

    await user.click(await screen.findByRole('button', { name: 'New application' }))
    const dialog = screen.getByRole('dialog', { name: 'New application' })
    await user.click(within(dialog).getByRole('checkbox', { name: /proj-alpha/ }))
    // A project that has no applications yet can be typed in by an unscoped administrator.
    await user.type(within(dialog).getByRole('textbox', { name: 'Other project ID' }), 'proj-gamma')
    await user.click(within(dialog).getByRole('button', { name: 'Add project' }))
    expect(within(dialog).getByRole('checkbox', { name: /proj-gamma/ })).toBeChecked()
    await user.type(within(dialog).getByRole('textbox', { name: 'Application name' }), 'checkout')
    // Names are unique by exact match (the database constraint is case-sensitive).
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument()
    await user.clear(within(dialog).getByRole('textbox', { name: 'Application name' }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Application name' }), 'Checkout')
    // Names are global: another project links the existing application instead of copying it.
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Link your project to it')
    expect(within(dialog).getByRole('button', { name: 'Create application' })).toBeDisabled()
    await user.clear(within(dialog).getByRole('textbox', { name: 'Application name' }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Application name' }), 'Orders')
    await user.type(within(dialog).getByRole('textbox', { name: 'Application description' }), 'Order services')
    await user.click(within(dialog).getByRole('button', { name: 'Create application' }))

    await waitFor(() => expect(create.captured).toEqual([{ project_ids: ['proj-alpha', 'proj-gamma'], name: 'Orders', description: 'Order services' }]))
    const orders = await screen.findByRole('region', { name: 'Application Orders' })
    const form = within(orders).getByRole('form', { name: 'New environment' })
    expect(within(form).getByRole('status')).toHaveTextContent('Orders was created')
    expect(within(form).getByRole('button', { name: 'Skip for now' })).toBeInTheDocument()
  })

  it('exposes environment details through a keyboard-focusable link', async () => {
    server.use(...catalogReadHandlers(), inventoryHandler(inventoryOf(ENV_STAGING.id, null)))
    const user = userEvent.setup()
    const { router } = renderList()

    const link = await screen.findByRole('link', { name: 'staging' })
    expect(link).toHaveAttribute('href', `/environments/${ENV_STAGING.id}`)
    link.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(router.state.location.pathname).toBe(`/environments/${ENV_STAGING.id}`))
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
    // The new environment only exists once created; its detail lookup is served directly.
    server.use(http.get('*/v1/catalog/environments/env-new', () => HttpResponse.json(ENV_NEW)), ...catalogReadHandlers(), create.handler, inventoryHandler(inventoryOf('env-new', null)))
    const user = userEvent.setup()
    const { router } = renderList('admin')

    await user.click(within(await screen.findByRole('region', { name: 'Application Checkout' })).getByRole('button', { name: 'Add environment' }))
    const panel = screen.getByRole('form', { name: 'New environment' })
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'staging')
    expect(within(panel).getByRole('alert')).toHaveTextContent('Checkout already has an environment with this name')
    await user.clear(within(panel).getByRole('textbox', { name: 'Name' }))
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'perf')
    await user.type(within(panel).getByRole('textbox', { name: 'Execution target URL' }), 'https://perf.checkout.example.com')
    await user.click(within(panel).getByRole('button', { name: 'Add host' }))
    await user.type(within(panel).getByRole('textbox', { name: 'Host 1 hostname' }), 'perf-node-1')
    await user.type(within(panel).getByRole('textbox', { name: 'Host 1 role' }), 'worker')
    await user.click(within(panel).getByText('Advanced options'))
    const options = within(panel).getByRole('textbox', { name: 'Options JSON' })
    await user.clear(options)
    await user.paste('{"namespace": "checkout-perf"}')
    await user.click(within(panel).getByRole('button', { name: 'Create environment' }))

    await waitFor(() => expect(router.state.location.pathname).toBe('/environments/env-new'))
    expect(create.captured).toEqual([{
      application_id: 'app-checkout',
      name: 'perf',
      kind: 'k8s',
      base_url: 'https://perf.checkout.example.com',
      hosts: [{ hostname: 'perf-node-1', role: 'worker' }],
      options: { namespace: 'checkout-perf' },
    }])
    // The detail page is titled with the environment's name.
    expect(await screen.findByRole('heading', { level: 1, name: 'perf' })).toBeInTheDocument()
  })

  it('edits, archives and (as a global admin) deletes an application from its menu', async () => {
    const apps: Application[] = APPS_FIXTURE.map(app => ({ ...app }))
    const writes = applicationWriteHandlers(apps)
    server.use(...catalogReadHandlers(apps), ...writes.handlers)
    const user = userEvent.setup()
    renderList('admin')

    const checkout = await screen.findByRole('region', { name: 'Application Checkout' })
    await user.click(within(checkout).getByRole('button', { name: 'Application actions: Checkout' }))
    await user.click(screen.getByRole('menuitem', { name: 'Edit details…' }))
    const edit = screen.getByRole('dialog', { name: 'Edit application Checkout' })
    await user.clear(within(edit).getByRole('textbox', { name: 'Application name' }))
    await user.type(within(edit).getByRole('textbox', { name: 'Application name' }), 'Search')
    expect(within(edit).getByRole('alert')).toHaveTextContent('Another application already uses this name')
    await user.clear(within(edit).getByRole('textbox', { name: 'Application name' }))
    await user.type(within(edit).getByRole('textbox', { name: 'Application name' }), 'Checkout web')
    // Share it with a second project.
    await user.click(within(edit).getByRole('checkbox', { name: /proj-beta/ }))
    await user.click(within(edit).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(writes.captured[0]).toEqual({ method: 'PATCH', path: APP_CHECKOUT.id, body: { name: 'Checkout web', project_ids: ['proj-alpha', 'proj-beta'] } }))

    const search = await screen.findByRole('region', { name: 'Application Search' })
    await user.click(within(search).getByRole('button', { name: 'Application actions: Search' }))
    await user.click(screen.getByRole('menuitem', { name: 'Archive…' }))
    const archive = screen.getByRole('dialog', { name: 'Archive application Search' })
    expect(archive).toHaveTextContent('New tests can no longer choose Search or its 1 environment')
    await user.click(within(archive).getByRole('button', { name: 'Archive application' }))
    await waitFor(() => expect(writes.captured.at(-1)).toEqual({ method: 'POST', path: `${APP_SEARCH.id}/archive` }))
    // Archived: the application and its environment leave the list together; nothing is orphaned.
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Application Search' })).not.toBeInTheDocument())
    expect(screen.queryByTestId(`env-row-${ENV_SEARCH_DEV.id}`)).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Unknown project' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Show archived applications' }))
    const archived = await screen.findByRole('region', { name: 'Application Search' })
    expect(within(archived).getByText('Archived')).toBeInTheDocument()
    expect(within(archived).getByTestId(`env-row-${ENV_SEARCH_DEV.id}`)).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Show archived applications' }))

    await user.click(within(await screen.findByRole('region', { name: 'Application Checkout web' })).getByRole('button', { name: 'Application actions: Checkout web' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete…' }))
    const remove = screen.getByRole('dialog', { name: 'Delete application Checkout web' })
    const confirm = within(remove).getByRole('button', { name: 'Delete application' })
    expect(confirm).toBeDisabled()
    await user.type(within(remove).getByRole('textbox', { name: 'Type the application name to confirm' }), 'Checkout web')
    await user.click(confirm)
    await waitFor(() => expect(writes.captured.at(-1)).toEqual({ method: 'DELETE', path: APP_CHECKOUT.id }))
  })

  it('offers application delete only to administrators', async () => {
    server.use(...catalogReadHandlers())
    const user = userEvent.setup()
    renderList('operator')

    const checkout = await screen.findByRole('region', { name: 'Application Checkout' })
    await user.click(within(checkout).getByRole('button', { name: 'Application actions: Checkout' }))
    expect(screen.getByRole('menuitem', { name: 'Edit details…' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Archive…' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Delete…' })).not.toBeInTheDocument()
  })

  it('requires typing the environment name before delete fires', async () => {
    const del = deleteEnvironmentHandler()
    server.use(...catalogReadHandlers(), del.handler)
    const user = userEvent.setup()
    renderList()

    const row = await screen.findByTestId(`env-row-${ENV_STAGING.id}`)
    await user.click(within(row).getByRole('button', { name: 'Environment actions: staging in Checkout' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete…' }))

    const dialog = await screen.findByRole('dialog', { name: 'Delete environment staging' })
    expect(dialog).toHaveTextContent('Tests and test presets that used it keep its old identifier')
    const confirm = within(dialog).getByRole('button', { name: 'Delete environment' })
    const input = within(dialog).getByRole('textbox', { name: 'Type the environment name to confirm' })
    expect(confirm).toBeDisabled()
    await user.type(input, 'stagin')
    expect(confirm).toBeDisabled()
    expect(del.captured).toHaveLength(0)
    await user.type(input, 'g')
    await user.click(confirm)
    await waitFor(() => expect(del.captured).toEqual([ENV_STAGING.id]))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('hides every mutation entry point from viewers', async () => {
    server.use(...catalogReadHandlers())
    const user = userEvent.setup()
    renderList('viewer')

    const row = await screen.findByTestId(`env-row-${ENV_STAGING.id}`)
    expect(screen.queryByRole('button', { name: 'New application' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add environment' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Application actions/ })).not.toBeInTheDocument()
    expect(screen.getByText('Your role has read-only access to applications and environments.')).toBeInTheDocument()

    await user.click(within(row).getByRole('button', { name: 'Environment actions: staging in Checkout' }))
    expect(screen.getByRole('menuitem', { name: 'Open' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Delete…' })).not.toBeInTheDocument()
  })

  it('shows a new application and its form even when the list was filtered', async () => {
    const apps: Application[] = [...APPS_FIXTURE]
    const create = createApplicationHandler('app-new', apps)
    server.use(...catalogReadHandlers(apps), create.handler)
    const user = userEvent.setup()
    const { router } = renderList('admin', '/environments?target=needs-approval&q=zzz')

    await user.click(await screen.findByRole('button', { name: 'New application' }))
    const dialog = screen.getByRole('dialog', { name: 'New application' })
    await user.click(within(dialog).getByRole('checkbox', { name: /proj-alpha/ }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Application name' }), 'Orders')
    await user.click(within(dialog).getByRole('button', { name: 'Create application' }))

    // Filters that would hide an application with no environments are cleared.
    const orders = await screen.findByRole('region', { name: 'Application Orders' })
    expect(within(orders).getByRole('form', { name: 'New environment' })).toBeInTheDocument()
    expect(router.state.location.search).toBe('')
  })


  it('offers archive and delete of a shared application only with access to every project using it', async () => {
    server.use(...catalogReadHandlers())
    const user = userEvent.setup()
    renderApp({ initialEntries: ['/environments'], authState: authenticatedState('admin', 'Alpha admin', [{ project_id: 'proj-alpha', app_id: null }]) })

    // Search is shared with proj-beta, which this administrator can't act in: it can be edited, not archived or deleted.
    const search = await screen.findByRole('region', { name: 'Application Search' })
    await user.click(within(search).getByRole('button', { name: 'Application actions: Search' }))
    expect(screen.getByRole('menuitem', { name: 'Edit details…' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Archive…' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Delete…' })).not.toBeInTheDocument()
    await user.keyboard('{Escape}')
    await user.click(within(search).getByRole('button', { name: 'Environment actions: dev in Search' }))
    expect(screen.queryByRole('menuitem', { name: 'Delete…' })).not.toBeInTheDocument()
    await user.keyboard('{Escape}')

    // Checkout is used by proj-alpha only.
    const checkout = screen.getByRole('region', { name: 'Application Checkout' })
    await user.click(within(checkout).getByRole('button', { name: 'Application actions: Checkout' }))
    expect(screen.getByRole('menuitem', { name: 'Archive…' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Delete…' })).toBeInTheDocument()
  })
})
