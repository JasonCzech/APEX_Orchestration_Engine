import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'

function renderSystem(role: 'admin' | 'viewer' = 'admin') {
  return renderApp({
    initialEntries: ['/admin/system'],
    authState: authenticatedState(role),
    seedSystemInfo: false,
  })
}

describe('AdminSystemPage', () => {
  it('renders the read-only system, identity, features and connectivity cards', async () => {
    renderSystem()

    // System card fed by GET /v1/system/info (default test/server handler).
    const system = await screen.findByLabelText('System info')
    expect(within(system).getByText('APEX Orchestration Engine')).toBeInTheDocument()
    expect(within(system).getByText('0.0.0-test')).toBeInTheDocument()
    expect(within(system).getByText('test')).toHaveClass('dash-context-chip')

    // Identity card mirrors the authenticated consumer.
    const identity = screen.getByLabelText('Your identity')
    expect(within(identity).getByText('Dash Ops')).toBeInTheDocument()
    expect(within(identity).getByText('admin')).toHaveClass('status-badge')
    expect(within(identity).getByText('proj-alpha')).toBeInTheDocument()

    // Feature flags from the fixture ({engines: true, documents: true}).
    const features = screen.getByLabelText('Features')
    expect(within(features).getByText('engines')).toBeInTheDocument()
    expect(within(features).getAllByText('on')).toHaveLength(2)

    // Connectivity reuses the ConnectivityContext state.
    const connectivity = screen.getByLabelText('Connectivity')
    expect(await within(connectivity).findByText('ok')).toHaveClass('status-badge')
    expect(within(connectivity).getByText('Last checked')).toBeInTheDocument()

    // Quick links card carries the runbooks note; everything is read-only.
    const links = screen.getByLabelText('Quick links')
    expect(links).toHaveTextContent('see docs/runbooks in the repo')
    expect(within(links).getByRole('link', { name: 'Connection registry' })).toHaveAttribute(
      'href',
      '/admin/connections',
    )
  })

  it("shows the 'Requires admin role' empty state to non-admins", async () => {
    renderSystem('viewer')

    expect(await screen.findByRole('heading', { name: 'Requires admin role' })).toBeInTheDocument()
    expect(screen.queryByLabelText('System info')).not.toBeInTheDocument()
  })
})
