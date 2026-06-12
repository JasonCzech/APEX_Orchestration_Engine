import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  ENV_STAGING,
  SNAPSHOT_FRESH,
  SNAPSHOT_STALE,
  catalogReadHandlers,
  inventoryHandler,
  inventoryOf,
  rescanHandler,
  updateEnvironmentHandler,
} from './environmentsTestHandlers'

function renderDetail(
  role: 'operator' | 'viewer' = 'operator',
  entry = `/environments/${ENV_STAGING.id}`,
) {
  return renderApp({ initialEntries: [entry], authState: authenticatedState(role) })
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
    renderDetail('operator', `/environments/${ENV_STAGING.id}?edit=1`)

    const form = await screen.findByRole('form', { name: 'Edit environment' })
    const baseUrl = within(form).getByRole('textbox', { name: 'Base URL' })
    await user.clear(baseUrl)
    await user.paste('https://staging2.checkout.example.com')
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(patch.captured).toHaveLength(1))
    const body = patch.captured[0]!
    // Exact payload shape: the four editable fields, nothing else (no name).
    expect(Object.keys(body).sort()).toEqual(['base_url', 'hosts', 'kind', 'options'])
    expect(body).toEqual({
      base_url: 'https://staging2.checkout.example.com',
      kind: 'k8s',
      hosts: [{ hostname: 'stg-node-1', role: 'worker' }],
      options: { namespace: 'checkout-stg' },
    })
    // Save closes the editor back to the read-mode reference card.
    expect(await screen.findByRole('heading', { name: 'Reference' })).toBeInTheDocument()
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
})
