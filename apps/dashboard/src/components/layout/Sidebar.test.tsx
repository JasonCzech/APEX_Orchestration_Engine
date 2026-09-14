import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { DEV_DATA_STORAGE_KEY } from '@/dev-data'
import { authenticatedState, renderApp } from '@/test/render'

vi.mock('@/features/approvals/useApprovalsInbox', () => ({
  useApprovalsInbox: () => ({
    count: 2,
    hasStale: false,
    items: [],
    removedItems: [],
    isLoading: false,
    error: null,
  }),
}))

describe('Sidebar', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    window.localStorage.removeItem('apex.sidebarCollapsed')
  })

  it('keeps admin tools hidden for viewers', async () => {
    renderApp({ authState: authenticatedState('viewer') })

    expect(await screen.findByRole('link', { name: 'Home' })).toBeInTheDocument()
    expect(screen.queryByText('Admin')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Connections' })).not.toBeInTheDocument()
  })

  it('keeps admin tools hidden for operators', async () => {
    renderApp({ authState: authenticatedState('operator') })

    expect(await screen.findByRole('link', { name: 'Home' })).toBeInTheDocument()
    expect(screen.queryByText('Admin')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Connections' })).not.toBeInTheDocument()
  })

  it('shows an Administration group for admins', async () => {
    renderApp({ authState: authenticatedState('admin') })

    expect(await screen.findByRole('link', { name: 'Home' })).toBeInTheDocument()
    expect(screen.getByText('Admin')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Connections' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Consumers' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'System' })).toBeInTheDocument()
  })

  it('toggles the collapsed class via the collapse button', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState() })

    const sidebar = await screen.findByTestId('sidebar')
    expect(sidebar).not.toHaveClass('collapsed')

    await user.click(screen.getByRole('button', { name: 'Collapse sidebar' }))
    expect(sidebar).toHaveClass('collapsed')
    expect(within(sidebar).getByRole('link', { name: 'Agent Analytics' })).toBeInTheDocument()
    expect(within(sidebar).getByRole('link', { name: 'Logs' })).toBeInTheDocument()
    expect(within(sidebar).getByRole('link', { name: 'Connections' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Expand sidebar' }))
    expect(sidebar).not.toHaveClass('collapsed')
  })

  it('keeps Tests selected throughout a run while New Test remains distinct', async () => {
    renderApp({
      initialEntries: ['/runs/thread-1/phases/execution'],
      authState: authenticatedState('operator'),
    })

    expect(await screen.findByRole('link', { name: 'Tests' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'New Test' })).not.toHaveAttribute('aria-current')
  })

  it('keeps the pending approval count visible in the collapsed rail', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState('operator') })

    const sidebar = await screen.findByTestId('sidebar')
    await user.click(screen.getByRole('button', { name: 'Collapse sidebar' }))
    const approvals = within(sidebar).getByRole('link', { name: /Approvals/ })
    expect(within(approvals).getByTestId('approvals-nav-badge')).toBeInTheDocument()
  })

  it('opens account options from the identity control', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState('operator') })

    const account = await screen.findByRole('button', { name: 'Account options for Dash Ops' })
    await user.click(account)
    expect(account).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('combobox', { name: 'Theme' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
  })

  it('hides the dummy-data switch outside explicit dev auth', async () => {
    renderApp({ authState: authenticatedState() })

    expect(await screen.findByTestId('sidebar-identity')).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Dummy data' })).not.toBeInTheDocument()
  })

  it('toggles and persists dummy-data mode from the dev account card', async () => {
    vi.stubEnv('VITE_APEX_DEV_AUTH', 'true')
    vi.stubEnv('VITE_APEX_DEV_API_KEY', 'dev-key-local')
    const user = userEvent.setup()
    renderApp()

    await user.click(await screen.findByRole('button', { name: 'Account options for Dev Admin' }))
    const toggle = await screen.findByRole('switch', { name: 'Dummy data' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')

    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(window.localStorage.getItem(DEV_DATA_STORAGE_KEY)).toBe('true')

    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(window.localStorage.getItem(DEV_DATA_STORAGE_KEY)).toBeNull()
  })
})
