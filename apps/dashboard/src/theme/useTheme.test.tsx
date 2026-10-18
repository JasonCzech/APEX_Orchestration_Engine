import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'

import { getStoredTheme, THEMES, THEME_STORAGE_KEY } from './useTheme'

describe('theme switcher', () => {
  it('applies the Load light default on mount', async () => {
    renderApp({ authState: authenticatedState() })

    await screen.findByTestId('sidebar')
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
  })

  it('migrates a retired palette when the provider mounts without the early script', async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'apex-light')
    renderApp({ authState: authenticatedState() })
    await screen.findByTestId('sidebar')
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
    expect(THEMES).not.toContain('apex-light')
  })

  it('uses the migrated palette when storage is readable but writes are denied', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'apex-light')
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Denied') })
    try { expect(getStoredTheme()).toBe('light') } finally { setItem.mockRestore() }
  })

  it('sets data-theme and persists the selection', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState() })

    await user.click(await screen.findByRole('button', { name: 'Account options for Dash Ops' }))
    const select = await screen.findByLabelText('Theme')
    await user.selectOptions(select, 'solarized-light')

    expect(document.documentElement.getAttribute('data-theme')).toBe('solarized-light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('solarized-light')
  })

  it('restores a persisted theme on mount', async () => {
    const user = userEvent.setup()
    window.localStorage.setItem(THEME_STORAGE_KEY, 'monokai-dimmed')
    renderApp({ authState: authenticatedState() })

    await screen.findByTestId('sidebar')
    expect(document.documentElement.getAttribute('data-theme')).toBe('monokai-dimmed')
    await user.click(await screen.findByRole('button', { name: 'Account options for Dash Ops' }))
    expect((await screen.findByLabelText<HTMLSelectElement>('Theme')).value).toBe('monokai-dimmed')
  })
})
