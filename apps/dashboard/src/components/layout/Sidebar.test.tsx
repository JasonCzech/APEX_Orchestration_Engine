import { act, screen, waitFor, within } from '@testing-library/react'
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

  it('starts with the rail and opens an accessible overlay without replacing it', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState() })

    const sidebar = await screen.findByTestId('sidebar')
    expect(sidebar).toHaveClass('collapsed')
    const expand = screen.getByRole('button', { name: 'Expand sidebar' })
    expect(expand).toHaveAttribute('aria-expanded', 'false')

    await user.click(expand)
    const dialog = screen.getByRole('dialog', { name: 'Navigation' })
    expect(sidebar).toHaveClass('collapsed')
    expect(within(dialog).getByRole('link', { name: 'Agent analytics' })).toBeInTheDocument()
    expect(within(dialog).getByRole('link', { name: 'Logs' })).toBeInTheDocument()
    expect(within(dialog).getByRole('link', { name: 'Connections' })).toBeInTheDocument()
    expect(window.localStorage.getItem('apex.sidebarCollapsed')).toBe('false')

    await user.click(within(dialog).getByRole('button', { name: 'Collapse sidebar' }))
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument()
    await waitFor(() => expect(expand).toHaveFocus())
    expect(window.localStorage.getItem('apex.sidebarCollapsed')).toBe('true')
  })

  it.each(['true', 'false'])('honors the stored collapsed preference %s', async stored => {
    window.localStorage.setItem('apex.sidebarCollapsed', stored)
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState() })
    await screen.findByTestId('sidebar')
    if (stored === 'false') {
      expect(screen.getByRole('dialog', { name: 'Navigation' })).toBeInTheDocument()
      await user.keyboard('{Escape}')
      await waitFor(() => expect(screen.getByRole('button', { name: 'Expand sidebar' })).toHaveFocus())
    } else {
      expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument()
    }
  })

  it('traps Tab only in the expanded overlay and restores focus after Escape', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState() })
    const expand = await screen.findByRole('button', { name: 'Expand sidebar' })
    await user.click(expand)
    const dialog = screen.getByRole('dialog', { name: 'Navigation' })
    const first = within(dialog).getByRole('link', { name: 'New test' })
    const last = within(dialog).getByRole('button', { name: 'Collapse sidebar' })
    expect(first).toHaveFocus()
    await user.tab({ shift: true })
    expect(last).toHaveFocus()
    await user.tab()
    expect(first).toHaveFocus()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(expand).toHaveFocus())
    await user.tab()
    expect(screen.getByTestId('sidebar')).not.toContainElement(document.activeElement as HTMLElement)
  })

  it('does not pull focus back after the user tabs away from a dismissed overlay', async () => {
    const frames: FrameRequestCallback[] = []
    const request = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => {
      frames.push(callback)
      return frames.length
    })
    try {
      const user = userEvent.setup()
      renderApp({ authState: authenticatedState() })
      const expand = await screen.findByRole('button', { name: 'Expand sidebar' })
      await user.click(expand)
      await user.keyboard('{Escape}')
      expect(expand).toHaveFocus()
      await user.tab()
      const nextControl = document.activeElement
      expect(screen.getByTestId('sidebar')).not.toContainElement(nextControl as HTMLElement)
      act(() => { for (const frame of frames) frame(performance.now()) })
      expect(document.activeElement).toBe(nextControl)
    } finally {
      request.mockRestore()
    }
  })

  it('dismisses an outside press and returns focus to its opener', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState() })
    const expand = await screen.findByRole('button', { name: 'Expand sidebar' })
    await user.click(expand)
    const overlay = screen.getByRole('dialog', { name: 'Navigation' }).parentElement!
    await user.click(overlay)
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument()
    await waitFor(() => expect(expand).toHaveFocus())
  })

  it('opens from empty rail space while destination clicks remain direct', async () => {
    const user = userEvent.setup()
    const { router } = renderApp({ authState: authenticatedState() })
    const sidebar = await screen.findByTestId('sidebar')
    await user.click(sidebar)
    expect(screen.getByRole('dialog', { name: 'Navigation' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Expand sidebar' })).toHaveFocus())
    await user.click(within(sidebar).getByRole('link', { name: 'Settings' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/settings'))
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
    const dialog = screen.getByRole('dialog', { name: 'Navigation' })
    expect(within(dialog).getByRole('button', { name: 'Account options for Dash Ops' })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('combobox', { name: 'Theme' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(account).toHaveFocus())
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
