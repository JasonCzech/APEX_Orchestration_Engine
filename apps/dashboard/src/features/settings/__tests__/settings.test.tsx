/**
 * /settings: theme swatch switching (persisted via useTheme), replace-key
 * (validated against /v1/system/info BEFORE persisting; rejected keys leave
 * the stored key untouched), and sign-out dropping back to the ApiKeyGate.
 */
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { API_KEY_STORAGE_KEY } from '@/auth/keyStorage'
import { authenticatedState, renderApp } from '@/test/render'
import { server, SYSTEM_INFO } from '@/test/server'
import { THEME_STORAGE_KEY } from '@/theme/useTheme'

function renderSettings() {
  return renderApp({ initialEntries: ['/settings'], authState: authenticatedState() })
}

describe('SettingsPage', () => {
  it('theme swatch selection applies data-theme and persists', async () => {
    const user = userEvent.setup()
    renderSettings()

    const picker = await screen.findByRole('group', { name: 'Theme picker' })
    expect(within(picker).getByRole('button', { name: /^Dark/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    await user.click(within(picker).getByRole('button', { name: 'Solarized Light' }))

    expect(document.documentElement.getAttribute('data-theme')).toBe('solarized-light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('solarized-light')
    expect(within(picker).getByRole('button', { name: /Solarized Light/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('replace key validates against system/info, then saves and updates the mask', async () => {
    const seenKeys: (string | null)[] = []
    server.use(
      http.get('*/v1/system/info', ({ request }) => {
        seenKeys.push(request.headers.get('x-api-key'))
        return HttpResponse.json(SYSTEM_INFO)
      }),
    )
    const user = userEvent.setup()
    renderSettings()

    expect(await screen.findByTestId('settings-key-mask')).toHaveTextContent('not stored')
    await user.type(screen.getByLabelText('Replace key'), 'apex_new_key_1234')
    await user.click(screen.getByRole('button', { name: 'Validate & save' }))

    expect(await screen.findByText('Key validated and saved.')).toBeInTheDocument()
    expect(seenKeys).toContain('apex_new_key_1234') // validated BEFORE saving
    expect(window.localStorage.getItem(API_KEY_STORAGE_KEY)).toBe('apex_new_key_1234')
    expect(screen.getByTestId('settings-key-mask')).toHaveTextContent('••••••••1234')
  })

  it('a rejected replacement key is not saved', async () => {
    server.use(
      http.get('*/v1/system/info', () =>
        HttpResponse.json({ detail: 'invalid key' }, { status: 401 }),
      ),
    )
    const user = userEvent.setup()
    renderSettings()

    await user.type(await screen.findByLabelText('Replace key'), 'apex_bad_key')
    await user.click(screen.getByRole('button', { name: 'Validate & save' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Key was rejected — the stored key is unchanged.',
    )
    expect(window.localStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull()
    expect(screen.getByTestId('settings-key-mask')).toHaveTextContent('not stored')
  })

  it('sign out clears the stored key and drops back to the key gate', async () => {
    window.localStorage.setItem(API_KEY_STORAGE_KEY, 'apex_old_key_9999')
    const user = userEvent.setup()
    // Real AuthProvider flow (no staticState): the stored key validates
    // against the global system/info handler before the shell renders.
    renderApp({ initialEntries: ['/settings'] })

    expect(await screen.findByTestId('settings-key-mask')).toHaveTextContent('••••••••9999')
    // The sidebar footer also offers Sign out — exercise the settings one.
    const keySection = screen.getByRole('region', { name: 'API key' })
    await user.click(within(keySection).getByRole('button', { name: 'Sign out' }))

    expect(
      await screen.findByRole('heading', { name: 'Connect to the control plane' }),
    ).toBeInTheDocument()
    expect(window.localStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull()
  })
})
