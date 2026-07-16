import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { renderApp } from '@/test/render'
import { server, SYSTEM_INFO } from '@/test/server'

import { API_KEY_STORAGE_KEY } from './keyStorage'

describe('ApiKeyGate', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('renders the key-entry gate when no key is stored', () => {
    renderApp()

    expect(
      screen.getByRole('heading', { name: 'Connect to the control plane' }),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('sidebar')).not.toBeInTheDocument()
  })

  it('validates a submitted key against system/info and renders the shell with the consumer identity', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByLabelText('API key'), 'apex_valid_key')
    await user.click(screen.getByRole('button', { name: 'Connect' }))

    const identity = within(await screen.findByTestId('sidebar-identity'))
    expect(identity.getByText('Dash Ops')).toBeInTheDocument()
    expect(identity.getByText('admin')).toBeInTheDocument()
    expect(window.localStorage.getItem(API_KEY_STORAGE_KEY)).toBe('apex_valid_key')
  })

  it('keeps the gate up with an error and clears the stored key on 401', async () => {
    server.use(
      http.get('*/v1/system/info', () =>
        HttpResponse.json({ detail: 'Invalid API key' }, { status: 401 }),
      ),
    )
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByLabelText('API key'), 'apex_bad_key')
    await user.click(screen.getByRole('button', { name: 'Connect' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('API key was rejected')
    expect(
      screen.getByRole('heading', { name: 'Connect to the control plane' }),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('sidebar')).not.toBeInTheDocument()
    expect(window.localStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull()
  })

  it('retries validation when the submitted key matches a stored key after a transient failure', async () => {
    window.localStorage.setItem(API_KEY_STORAGE_KEY, 'apex_retry_key')
    let attempts = 0
    server.use(
      http.get('*/v1/system/info', () => {
        attempts += 1
        return attempts === 1
          ? HttpResponse.json({ detail: 'temporarily unavailable' }, { status: 503 })
          : HttpResponse.json(SYSTEM_INFO)
      }),
    )
    const user = userEvent.setup()
    renderApp()

    expect(await screen.findByRole('alert')).toHaveTextContent('temporarily unavailable')
    await user.type(screen.getByLabelText('API key'), 'apex_retry_key')
    await user.click(screen.getByRole('button', { name: 'Connect' }))

    const identity = within(await screen.findByTestId('sidebar-identity'))
    expect(identity.getByText('Dash Ops')).toBeInTheDocument()
    expect(attempts).toBeGreaterThanOrEqual(2)
  })

  it('bypasses the key-entry gate when explicit Vite dev auth is enabled', async () => {
    vi.stubEnv('VITE_APEX_DEV_AUTH', 'true')
    vi.stubEnv('VITE_APEX_DEV_API_KEY', 'dev-key-local')

    renderApp()

    const identity = within(await screen.findByTestId('sidebar-identity'))
    expect(identity.getByText('Dev Admin')).toBeInTheDocument()
    expect(identity.getByText('admin')).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { name: 'Connect to the control plane' }),
    ).not.toBeInTheDocument()
    expect(window.localStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull()
  })
})
