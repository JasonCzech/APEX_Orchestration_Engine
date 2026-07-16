import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

describe('system health status dot', () => {
  it('shows ok when system/info responds', async () => {
    renderApp({ authState: authenticatedState(), seedSystemInfo: false })

    const dot = await screen.findByTestId('connection-status-dot')
    await waitFor(() => expect(dot).toHaveAttribute('data-state', 'ok'))
  })

  it('shows unreachable on network error', async () => {
    server.use(http.get('*/v1/system/info', () => HttpResponse.error()))
    renderApp({ authState: authenticatedState(), seedSystemInfo: false })

    const dot = await screen.findByTestId('connection-status-dot')
    await waitFor(() => expect(dot).toHaveAttribute('data-state', 'unreachable'))
  })

  it('shows degraded when the API answers with an error status', async () => {
    server.use(
      http.get('*/v1/system/info', () =>
        HttpResponse.json({ detail: 'down' }, { status: 503 }),
      ),
    )
    renderApp({ authState: authenticatedState(), seedSystemInfo: false })

    const dot = await screen.findByTestId('connection-status-dot')
    await waitFor(() => expect(dot).toHaveAttribute('data-state', 'degraded'))
  })
})
