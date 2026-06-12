import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { renderApp } from '@/test/render'
import { server } from '@/test/server'

import { API_KEY_STORAGE_KEY } from './keyStorage'

describe('ApiKeyGate', () => {
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
})
