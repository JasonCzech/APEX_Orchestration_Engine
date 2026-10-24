import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { DEV_DATA_STORAGE_KEY } from '@/dev-data'
import * as runtimeConfig from '@/config/runtimeConfig'
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
    expect(screen.getByRole('link', { name: 'API consumers' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'System status' })).toBeInTheDocument()
    const statusIcon = screen.getByRole('link', { name: 'System status' }).querySelector('path')?.getAttribute('d')
    const settingsIcon = screen.getByRole('link', { name: 'Settings' }).querySelector('path')?.getAttribute('d')
    expect(statusIcon).toBeTruthy()
    expect(statusIcon).not.toBe(settingsIcon)
  })

  it('expands the existing rail in place and leaves the page interactive', async () => {
    const user = userEvent.setup()
    renderApp({ initialEntries: ['/settings'], authState: authenticatedState() })

    const sidebar = await screen.findByTestId('sidebar')
    const expand = screen.getByRole('button', { name: 'Expand sidebar' })
    expect(sidebar).toHaveClass('collapsed')
    expect(expand).toHaveAttribute('aria-expanded', 'false')
    expect(expand).not.toHaveAttribute('aria-haspopup')
    await user.click(expand)
    expect(sidebar).not.toHaveClass('collapsed')
    expect(screen.getAllByRole('navigation', { name: 'Primary' })).toHaveLength(1)
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument()
    expect(expand).toHaveFocus()
    expect(expand).toHaveAttribute('aria-expanded', 'true')
    expect(window.localStorage.getItem('apex.sidebarCollapsed')).toBe('false')

    await user.click(await screen.findByRole('tab', { name: 'Connection & session' }))
    expect(screen.getByRole('tab', { name: 'Connection & session' })).toHaveAttribute('aria-selected', 'true')
    expect(sidebar).not.toHaveClass('collapsed')
    await user.click(screen.getByRole('button', { name: 'Collapse sidebar' }))
    expect(sidebar).toHaveClass('collapsed')
    expect(expand).toHaveFocus()
    expect(window.localStorage.getItem('apex.sidebarCollapsed')).toBe('true')
  })

  it.each(['true', 'false'])('honors the stored collapsed preference %s without opening a dialog', async stored => {
    window.localStorage.setItem('apex.sidebarCollapsed', stored)
    renderApp({ authState: authenticatedState() })
    const sidebar = await screen.findByTestId('sidebar')
    expect(sidebar.classList.contains('collapsed')).toBe(stored === 'true')
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument()
  })

  it('allows keyboard focus to leave the expanded sidebar', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState() })
    const expand = await screen.findByRole('button', { name: 'Expand sidebar' })
    await user.click(expand)
    await user.tab()
    expect(screen.getByTestId('sidebar')).not.toContainElement(document.activeElement as HTMLElement)
    expect(screen.getByRole('button', { name: 'Collapse sidebar' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('opens from empty rail space and keeps navigation expanded after a destination click', async () => {
    const user = userEvent.setup()
    const { router } = renderApp({ authState: authenticatedState() })
    const sidebar = await screen.findByTestId('sidebar')
    await user.click(sidebar)
    expect(sidebar).not.toHaveClass('collapsed')
    await user.click(within(sidebar).getByRole('link', { name: 'Settings' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/settings'))
    expect(sidebar).not.toHaveClass('collapsed')
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument()
  })

  it('keeps Tests selected throughout a run while New test remains distinct', async () => {
    renderApp({
      initialEntries: ['/runs/thread-1/phases/execution'],
      authState: authenticatedState('operator'),
    })

    expect(await screen.findByRole('link', { name: /^Tests/ })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'New test' })).not.toHaveAttribute('aria-current')
  })

  it('uses Tests as the single sidebar destination for the review queue', async () => {
    renderApp({ initialEntries: ['/approvals'], authState: authenticatedState('operator') })

    expect(await screen.findByRole('link', { name: /^Tests/ })).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByRole('link', { name: /^Approvals/ })).not.toBeInTheDocument()
  })

  it('keeps the pending-review count visible in the collapsed rail', async () => {
    renderApp({ authState: authenticatedState('operator') })

    const sidebar = await screen.findByTestId('sidebar')
    expect(sidebar).toHaveClass('collapsed')
    const tests = within(sidebar).getByRole('link', { name: /^Tests/ })
    expect(within(tests).getByTestId('tests-review-nav-badge')).toBeInTheDocument()
    expect(tests).toHaveAccessibleName('Tests 2 tests need review')
  })

  it('opens account options from the identity control', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState('operator') })

    const account = await screen.findByRole('button', { name: 'Account options for Dash Ops' })
    await user.click(account)
    expect(account).toHaveFocus()
    expect(screen.getByTestId('sidebar')).not.toHaveClass('collapsed')
    expect(account).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('combobox', { name: 'Theme' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Collapse sidebar' }))
    expect(screen.queryByRole('combobox', { name: 'Theme' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Expand sidebar' })).toHaveFocus()
  })

  it('hides the dummy-data switch outside explicit dev auth', async () => {
    renderApp({ authState: authenticatedState() })

    expect(await screen.findByTestId('sidebar-identity')).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Dummy data' })).not.toBeInTheDocument()
  })

  it('describes the collapsed demo status as a simulation', async () => {
    const demo = vi.spyOn(runtimeConfig, 'isDemoDataMode').mockReturnValue(true)
    try {
      renderApp({ authState: authenticatedState() })
      const sidebar = await screen.findByTestId('sidebar')
      expect(within(sidebar).getByTitle('Offline simulation')).toBeInTheDocument()
      expect(within(sidebar).queryByTitle('Connected')).not.toBeInTheDocument()
    } finally {
      demo.mockRestore()
    }
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
