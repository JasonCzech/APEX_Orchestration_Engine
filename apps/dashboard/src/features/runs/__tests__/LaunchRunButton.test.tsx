import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { createTestQueryClient } from '@/test/render'

import { LaunchRunButton } from '../LaunchRunButton'
import { ALL_AUTO_GATES } from '../launchRun'

const { threadsCreate, runsCreate } = vi.hoisted(() => ({
  threadsCreate: vi.fn(),
  runsCreate: vi.fn(),
}))

// The launch path goes through the SDK client factory — fake the two calls.
vi.mock('@/api/langgraphClient', () => ({
  getLangGraphClient: () =>
    Promise.resolve({
      threads: { create: threadsCreate },
      runs: { create: runsCreate },
    }),
}))

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
    threadsCreate.mockReset().mockResolvedValue({ thread_id: 'thread-new' })
    runsCreate.mockReset().mockResolvedValue({ run_id: 'run-1' })
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

    await waitFor(() => expect(runsCreate).toHaveBeenCalledTimes(1))
    expect(threadsCreate).toHaveBeenCalledWith({ metadata: { project_id: 'demo' } })
    expect(runsCreate).toHaveBeenCalledWith(
      'thread-new',
      'pipeline',
      expect.objectContaining({
        input: { title: 'Checkout soak', request: 'Soak the checkout flow for 1h' },
        config: {
          recursion_limit: expect.any(Number),
          configurable: { project_id: 'demo', gates: ALL_AUTO_GATES },
        },
        streamResumable: true,
        durability: 'sync',
        multitaskStrategy: 'reject',
      }),
    )
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
    runsCreate.mockRejectedValue(new Error('multitask reject'))
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
})
