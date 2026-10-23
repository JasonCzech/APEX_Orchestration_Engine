import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  APP_CHECKOUT,
  APP_SEARCH,
  ENV_STAGING,
  ENV_PROD,
  SNAPSHOT_FRESH,
  SNAPSHOT_STALE,
  catalogReadHandlers,
  deleteEnvironmentHandler,
  inventoryHandler,
  inventoryOf,
  rescanHandler,
  updateEnvironmentHandler,
} from './environmentsTestHandlers'

function renderDetail(
  role: 'admin' | 'operator' | 'viewer' = 'operator',
  entry = `/environments/${ENV_STAGING.id}`,
) {
  return renderApp({
    initialEntries: [entry],
    authState: role === 'admin' ? authenticatedState(role, 'Dash Ops', []) : authenticatedState(role),
  })
}

describe('EnvironmentDetailPage', () => {
  it('PATCHes exactly the editable fields from the inline edit form', async () => {
    const patch = updateEnvironmentHandler(ENV_STAGING)
    server.use(
      ...catalogReadHandlers(),
      patch.handler,
      inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)),
    )
    const user = userEvent.setup()
    // ?edit=1 — the list row's Edit action lands directly in edit mode.
    renderDetail('admin', `/environments/${ENV_STAGING.id}?edit=1`)

    const form = await screen.findByRole('form', { name: 'Edit environment' })
    const baseUrl = within(form).getByRole('textbox', { name: 'Execution target URL' })
    await user.clear(baseUrl)
    await user.paste('https://staging2.checkout.example.com')
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(patch.captured).toHaveLength(1))
    const body = patch.captured[0]!
    // Only changed fields are sent; unchanged target fields must not trigger re-approval.
    expect(Object.keys(body).sort()).toEqual(['base_url'])
    expect(body).toEqual({
      base_url: 'https://staging2.checkout.example.com',
    })
    // Save closes the editor back to the read-mode reference card.
    expect(await screen.findByRole('heading', { name: 'Configuration' })).toBeInTheDocument()
  })

  it('keeps list edit/delete actions disabled while a detail save survives navigation', async () => {
    let markSaveStarted!: () => void
    const saveStarted = new Promise<void>((resolve) => {
      markSaveStarted = resolve
    })
    let releaseSave!: () => void
    const saveRelease = new Promise<void>((resolve) => {
      releaseSave = resolve
    })
    server.use(
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)),
      http.patch('*/v1/catalog/environments/:id', async () => {
        markSaveStarted()
        await saveRelease
        return HttpResponse.json({ ...ENV_STAGING, kind: 'other' })
      }),
    )
    const user = userEvent.setup()
    const { router } = renderDetail('admin', `/environments/${ENV_STAGING.id}?edit=1`)

    const form = await screen.findByRole('form', { name: 'Edit environment' })
    await user.selectOptions(within(form).getByRole('combobox', { name: 'Platform' }), 'other')
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))
    await saveStarted

    await act(async () => router.navigate('/environments'))
    const row = await screen.findByTestId(`env-row-${ENV_STAGING.id}`)
    await user.click(within(row).getByRole('button', { name: 'Environment actions: staging in Checkout' }))
    expect(screen.getByRole('menuitem', { name: 'Edit' })).toBeDisabled()
    expect(screen.getByRole('menuitem', { name: 'Delete…' })).toBeDisabled()

    releaseSave()
    await waitFor(() => expect(screen.getByRole('menuitem', { name: 'Edit' })).toBeEnabled())
    expect(screen.getByRole('menuitem', { name: 'Delete…' })).toBeEnabled()
  })

  it('locks the editor while a deferred rescan is in flight', async () => {
    let markRescanStarted!: () => void
    const rescanStarted = new Promise<void>((resolve) => {
      markRescanStarted = resolve
    })
    let releaseRescan!: () => void
    const rescanRelease = new Promise<void>((resolve) => {
      releaseRescan = resolve
    })
    server.use(
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)),
      http.post('*/v1/inventory/environments/:id/rescan', async () => {
        markRescanStarted()
        await rescanRelease
        return HttpResponse.json(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH))
      }),
    )
    const user = userEvent.setup()
    renderDetail('admin', `/environments/${ENV_STAGING.id}?edit=1`)

    const form = await screen.findByRole('form', { name: 'Edit environment' })
    const save = within(form).getByRole('button', { name: 'Save changes' })
    const closeEditor = screen.getByRole('button', { name: 'Close editor' })
    const rescan = await screen.findByRole('button', { name: 'Rescan' })
    expect(save).toBeEnabled()

    await user.click(rescan)
    await rescanStarted

    expect(save).toBeDisabled()
    expect(closeEditor).toBeDisabled()
    expect(rescan).toBeDisabled()

    releaseRescan()
    await waitFor(() => expect(save).toBeEnabled())
    expect(closeEditor).toBeEnabled()
    expect(rescan).toBeEnabled()
  })

  it('shows the never-scanned empty state and refreshes the panel after a rescan', async () => {
    const rescan = rescanHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH))
    server.use(
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, null)),
      rescan.handler,
    )
    const user = userEvent.setup()
    renderDetail()

    const empty = await screen.findByTestId('env-inventory-empty')
    expect(within(empty).getByText('Never scanned')).toBeInTheDocument()

    await user.click(within(empty).getByRole('button', { name: 'Scan' }))

    expect(await screen.findByRole('table', { name: 'Services' })).toBeInTheDocument()
    expect(screen.getByText('checkout-api')).toBeInTheDocument()
    expect(screen.getByText(/Scanned just now/)).toBeInTheDocument()
    expect(screen.queryByTestId('env-inventory-empty')).not.toBeInTheDocument()
    expect(rescan.callCount()).toBe(1)
  })

  it('renders a 502 rescan failure as an inline danger card whose retry recovers', async () => {
    const rescan = rescanHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH), {
      failFirst: true,
      detail: 'environment rescan failed: kubeconfig secret missing',
    })
    server.use(
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, null)),
      rescan.handler,
    )
    const user = userEvent.setup()
    renderDetail()

    const empty = await screen.findByTestId('env-inventory-empty')
    await user.click(within(empty).getByRole('button', { name: 'Scan' }))

    // Inline danger card carrying the adapter message — not a toast.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveClass('env-inline-error')
    expect(alert).toHaveTextContent('environment rescan failed: kubeconfig secret missing')

    await user.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('table', { name: 'Services' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(rescan.callCount()).toBe(2)
  })

  it('flags stale snapshots with an amber chip next to the scan caption', async () => {
    server.use(
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_STALE)),
    )
    renderDetail()

    const chip = await screen.findByText('stale')
    expect(chip).toHaveClass('status-badge', 'warning')
    expect(screen.getByRole('table', { name: 'Services' })).toBeInTheDocument()
  })

  it('renders zero-replica services in danger tone and others plainly', async () => {
    server.use(
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)),
    )
    renderDetail()

    const zeroRow = await screen.findByTestId('env-service-checkout-worker')
    expect(within(zeroRow).getByText('0')).toHaveClass('status-badge', 'danger')
    expect(
      within(zeroRow).getByText('registry.example.com/checkout-worker:1.42.0'),
    ).toHaveClass('env-image')

    const healthyRow = screen.getByTestId('env-service-checkout-api')
    expect(within(healthyRow).getByText('3')).not.toHaveClass('status-badge')
  })

  it('hides Edit and Rescan from viewers (including the empty-state CTA)', async () => {
    server.use(
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, null)),
    )
    renderDetail('viewer')

    expect(await screen.findByRole('heading', { name: 'staging' })).toBeInTheDocument()
    expect(await screen.findByTestId('env-inventory-empty')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Rescan' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Scan' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    // The read-only reference card still renders fully.
    expect(screen.getByRole('heading', { name: 'Configuration' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Hosts' })).toBeInTheDocument()
  })

  it('ignores a deep-linked edit request for viewers', async () => {
    server.use(
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)),
    )
    renderDetail('viewer', `/environments/${ENV_STAGING.id}?edit=1`)

    expect(await screen.findByRole('heading', { name: 'Configuration' })).toBeInTheDocument()
    expect(screen.queryByRole('form', { name: 'Edit environment' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
  })

  it('drops edit state and field drafts when navigating to a cached environment', async () => {
    server.use(
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)),
    )
    const user = userEvent.setup()
    const { router } = renderDetail('admin', `/environments/${ENV_PROD.id}`)

    await screen.findByRole('heading', { name: 'production' })
    await act(async () => router.navigate(`/environments/${ENV_STAGING.id}`))
    await screen.findByRole('heading', { name: 'staging' })
    await user.click(screen.getByRole('button', { name: 'Edit' }))
    const stagingUrl = screen.getByRole('textbox', { name: 'Execution target URL' })
    await user.clear(stagingUrl)
    await user.type(stagingUrl, 'https://wrong-for-production.example.com')

    await act(async () => router.navigate(`/environments/${ENV_PROD.id}`))
    await screen.findByRole('heading', { name: 'production' })
    expect(screen.queryByRole('form', { name: 'Edit environment' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByRole('textbox', { name: 'Execution target URL' })).toHaveValue(ENV_PROD.base_url)
  })

  it('names the page after the environment, with a link-only breadcrumb', async () => {
    server.use(...catalogReadHandlers(), inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)))
    const { router } = renderDetail('operator')

    expect(await screen.findByRole('heading', { level: 1, name: 'staging' })).toBeInTheDocument()
    const breadcrumb = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(within(breadcrumb).getByRole('link', { name: 'Checkout' })).toHaveAttribute('href', `/environments?application=${ENV_STAGING.application_id}`)

    // Leaving the page gives the title back to the route.
    await act(async () => router.navigate('/environments'))
    expect(await screen.findByRole('heading', { level: 1, name: 'Applications & environments' })).toBeInTheDocument()
  })

  it('keeps an archived application\'s environment fully linked', async () => {
    const archived = { ...APP_CHECKOUT, archived_at: '2026-10-20T00:00:00Z' }
    server.use(...catalogReadHandlers([archived, APP_SEARCH]), inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)))
    renderDetail('operator')

    const breadcrumb = await screen.findByRole('navigation', { name: 'Breadcrumb' })
    expect(await within(breadcrumb).findByRole('link', { name: 'Checkout' })).toBeInTheDocument()
    expect(await screen.findByText('None of the latest 100 tests in this project used this environment.')).toBeInTheDocument()
  })

  it('renames an environment and refuses a name its application already uses', async () => {
    const patch = updateEnvironmentHandler(ENV_STAGING)
    server.use(...catalogReadHandlers(), patch.handler, inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)))
    const user = userEvent.setup()
    renderDetail('admin', `/environments/${ENV_STAGING.id}?edit=1`)

    const form = await screen.findByRole('form', { name: 'Edit environment' })
    const name = within(form).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'production')
    expect(within(form).getByRole('alert')).toHaveTextContent('already has an environment with this name')
    expect(within(form).getByRole('button', { name: 'Save changes' })).toBeDisabled()

    await user.clear(name)
    await user.type(name, 'staging-eu')
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(patch.captured).toHaveLength(1))
    expect(patch.captured[0]).toEqual({ name: 'staging-eu' })
  })

  it('deletes from the detail page and returns to the application', async () => {
    const remove = deleteEnvironmentHandler()
    server.use(...catalogReadHandlers(), remove.handler, inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)))
    const user = userEvent.setup()
    const { router } = renderDetail('admin')

    await user.click(await screen.findByRole('button', { name: 'Delete…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete environment staging' })
    const confirm = within(dialog).getByRole('button', { name: 'Delete environment' })
    expect(confirm).toBeDisabled()
    await user.type(within(dialog).getByRole('textbox'), 'staging')
    await user.click(confirm)

    await waitFor(() => expect(remove.captured).toEqual([ENV_STAGING.id]))
    await waitFor(() => expect(router.state.location.pathname).toBe('/environments'))
    expect(router.state.location.search).toBe(`?application=${ENV_STAGING.application_id}`)
  })

  it('lists environment-context prompts and the recent tests that used this environment', async () => {
    // Overrides go first: MSW uses the first matching handler in one server.use call.
    server.use(
      http.get('*/v1/prompts', () => HttpResponse.json([
        { id: 'prm-env', key: 'staging-quirks', description: 'Staging quirks', namespace: 'environment', environment_id: ENV_STAGING.id, active_version: null },
      ])),
      http.get('*/v1/pipelines', () => HttpResponse.json({
        items: [
          { thread_id: 'pl-used', title: 'Checkout soak', status: 'completed', updated_at: '2026-10-21T00:00:00Z',
            test_scope: { participants: [{ environment_id: ENV_STAGING.id }] } },
          { thread_id: 'pl-other', title: 'Search spike', status: 'completed', updated_at: '2026-10-21T00:00:00Z',
            test_scope: { participants: [{ environment_id: 'env-elsewhere' }] } },
        ],
        limit: 100, offset: 0,
      })),
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)),
    )
    renderDetail('admin')

    const related = await screen.findByRole('region', { name: 'Related' })
    expect(await within(related).findByRole('link', { name: 'Staging quirks' })).toHaveAttribute('href', '/prompts?ns=environment&prompt=prm-env')
    expect(await within(related).findByRole('link', { name: /Checkout soak/ })).toBeInTheDocument()
    expect(within(related).queryByText(/Search spike/)).not.toBeInTheDocument()
    expect(within(related).getByRole('link', { name: 'Add environment context' })).toBeInTheDocument()
  })
})
