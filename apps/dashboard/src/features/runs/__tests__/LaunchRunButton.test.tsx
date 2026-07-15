import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { LaunchRunButton } from '../LaunchRunButton'
import { ALL_AUTO_GATES } from '../launchRun'

function renderButton() {
  const router = createMemoryRouter(
    [
      { path: '/runs', element: <LaunchRunButton /> },
      { path: '/runs/:threadId', element: <div data-testid="run-page" /> },
    ],
    { initialEntries: ['/runs'] },
  )
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

describe('LaunchRunButton', () => {
  beforeEach(() => {
    server.use(
      http.post('*/v1/pipelines', () =>
        HttpResponse.json({
          thread_id: 'thread-new',
          run_id: 'run-1',
          stream_url: '/runs/run-1/stream',
        }),
      ),
    )
  })

  it('launches with all-auto gates and navigates to the live activity tab', async () => {
    const user = userEvent.setup()
    const router = renderButton()

    await user.click(screen.getByRole('button', { name: 'New run' }))
    const dialog = screen.getByRole('dialog', { name: 'Launch pipeline run' })
    expect(dialog).toBeInTheDocument()

    // Empty form cannot submit.
    expect(screen.getByRole('button', { name: 'Launch run' })).toBeDisabled()

    await user.type(screen.getByLabelText('Title'), 'Checkout soak')
    await user.type(screen.getByLabelText('Request'), 'Soak the checkout flow for 1h')
    // Project defaults to "demo".
    await user.click(screen.getByRole('button', { name: 'Launch run' }))

    // Every phase runs gate-free in D2.
    expect(Object.values(ALL_AUTO_GATES)).toHaveLength(7)
    for (const policy of Object.values(ALL_AUTO_GATES)) {
      expect(policy).toEqual({ prompt_review: 'auto', output_review: 'auto' })
    }

    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/thread-new'))
    expect(router.state.location.search).toBe('?tab=log')
    expect(screen.getByTestId('run-page')).toBeInTheDocument()
  })

  it('keeps the modal open with an inline error when the launch fails', async () => {
    server.use(
      http.post('*/v1/pipelines', () =>
        HttpResponse.json({ detail: 'multitask reject' }, { status: 409 }),
      ),
    )
    const user = userEvent.setup()
    const router = renderButton()

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'Doomed run')
    await user.type(screen.getByLabelText('Request'), 'This will fail')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Launch failed: multitask reject')
    expect(screen.getByRole('dialog', { name: 'Launch pipeline run' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/runs')
  })

  it('reuses one idempotency key when the user retries a failed request', async () => {
    const keys: string[] = []
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        const body = (await request.json()) as { idempotency_key: string }
        keys.push(body.idempotency_key)
        return keys.length === 1
          ? HttpResponse.json({ detail: 'temporary outage' }, { status: 503 })
          : HttpResponse.json({
              thread_id: 'thread-new',
              run_id: 'run-1',
              stream_url: '/runs/run-1/stream',
            })
      }),
    )
    const user = userEvent.setup()
    renderButton()

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'Retryable run')
    await user.type(screen.getByLabelText('Request'), 'Retry without duplication')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))
    await screen.findByRole('alert')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))

    await waitFor(() => expect(keys).toHaveLength(2))
    expect(keys[0]).toBe(keys[1])
  })
})
