import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { DEV_DATA_STORAGE_KEY } from '@/dev-data'
import { authenticatedState, renderApp } from '@/test/render'

describe('Sidebar', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('keeps admin tools hidden for viewers', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState('viewer') })

    expect(await screen.findByRole('link', { name: 'Home' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(screen.queryByText('Admin')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Connections' })).not.toBeInTheDocument()
  })

  it('keeps admin tools hidden for operators', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState('operator') })

    expect(await screen.findByRole('link', { name: 'Home' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(screen.queryByText('Admin')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Connections' })).not.toBeInTheDocument()
  })

  it('shows admin tools inside Advanced for admins', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState('admin') })

    expect(await screen.findByRole('link', { name: 'Home' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
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

    await user.click(screen.getByRole('button', { name: 'Expand sidebar' }))
    expect(sidebar).not.toHaveClass('collapsed')
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
