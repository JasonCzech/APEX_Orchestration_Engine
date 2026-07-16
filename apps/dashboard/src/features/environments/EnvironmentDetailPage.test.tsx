import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  ENV_STAGING,
  ENV_PROD,
  SNAPSHOT_FRESH,
  SNAPSHOT_STALE,
  catalogReadHandlers,
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
    const baseUrl = within(form).getByRole('textbox', { name: 'Base URL' })
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
    expect(await screen.findByRole('heading', { name: 'Reference' })).toBeInTheDocument()
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
    await user.selectOptions(within(form).getByRole('combobox', { name: 'Kind' }), 'other')
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))
    await saveStarted

    await act(async () => router.navigate('/environments'))
    const row = await screen.findByTestId(`env-row-${ENV_STAGING.id}`)
    await user.click(within(row).getByRole('button', { name: 'Environment actions: staging' }))
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
    const rescan = screen.getByRole('button', { name: 'Rescan' })
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

    await user.click(within(empty).getByRole('button', { name: 'Rescan' }))

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
    await user.click(within(empty).getByRole('button', { name: 'Rescan' }))

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
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    // The read-only reference card still renders fully.
    expect(screen.getByRole('heading', { name: 'Reference' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Hosts' })).toBeInTheDocument()
  })

  it('ignores a deep-linked edit request for viewers', async () => {
    server.use(
      ...catalogReadHandlers(),
      inventoryHandler(inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)),
    )
    renderDetail('viewer', `/environments/${ENV_STAGING.id}?edit=1`)

    expect(await screen.findByRole('heading', { name: 'Reference' })).toBeInTheDocument()
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
    const stagingUrl = screen.getByRole('textbox', { name: 'Base URL' })
    await user.clear(stagingUrl)
    await user.type(stagingUrl, 'https://wrong-for-production.example.com')

    await act(async () => router.navigate(`/environments/${ENV_PROD.id}`))
    await screen.findByRole('heading', { name: 'production' })
    expect(screen.queryByRole('form', { name: 'Edit environment' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByRole('textbox', { name: 'Base URL' })).toHaveValue(ENV_PROD.base_url)
  })
})
