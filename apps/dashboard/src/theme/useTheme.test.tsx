import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'

import { THEME_STORAGE_KEY } from './useTheme'

describe('theme switcher', () => {
  it('applies the dark default on mount', async () => {
    renderApp({ authState: authenticatedState() })

    await screen.findByTestId('sidebar')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })

  it('sets data-theme and persists the selection', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState() })

    const select = await screen.findByLabelText('Theme')
    await user.selectOptions(select, 'solarized-light')

    expect(document.documentElement.getAttribute('data-theme')).toBe('solarized-light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('solarized-light')
  })

  it('restores a persisted theme on mount', async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'monokai-dimmed')
    renderApp({ authState: authenticatedState() })

    await screen.findByTestId('sidebar')
    expect(document.documentElement.getAttribute('data-theme')).toBe('monokai-dimmed')
    expect((await screen.findByLabelText<HTMLSelectElement>('Theme')).value).toBe('monokai-dimmed')
  })
})
