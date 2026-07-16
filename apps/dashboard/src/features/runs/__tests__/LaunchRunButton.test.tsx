import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { AuthProvider, type AuthState } from '@/auth/AuthProvider'
import { bumpSessionRevision } from '@/auth/keyStorage'
import { authenticatedState, createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { LaunchRunButton } from '../LaunchRunButton'
import { ALL_AUTO_GATES } from '../launchRun'

function renderButton(
  options: { authState?: AuthState; persistent?: boolean } = {},
) {
  const router = createMemoryRouter(
    options.persistent
      ? [{ path: '*', element: <LaunchRunButton /> }]
      : [
          { path: '/runs', element: <LaunchRunButton /> },
          { path: '/runs/:threadId', element: <div data-testid="run-page" /> },
        ],
    { initialEntries: ['/runs'] },
  )
  const routed = <RouterProvider router={router} />
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      {options.authState ? (
        <AuthProvider staticState={options.authState}>{routed}</AuthProvider>
      ) : (
        routed
      )}
    </QueryClientProvider>,
  )
  return router
}

describe('LaunchRunButton', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    server.use(
      http.post('*/v1/pipelines', () =>
        HttpResponse.json({
          thread_id: 'thread-new',
          run_id: 'run-1',
          stream_url: '/threads/thread-1/runs/run-1/stream',
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
              stream_url: '/threads/thread-1/runs/run-1/stream',
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

  it('starts a new idempotent attempt when a failed request is edited', async () => {
    const keys: string[] = []
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        const body = (await request.json()) as { idempotency_key: string }
        keys.push(body.idempotency_key)
        return keys.length === 1
          ? HttpResponse.json({ detail: 'ambiguous outage' }, { status: 503 })
          : HttpResponse.json({
              thread_id: 'thread-edited',
              run_id: 'run-edited',
              stream_url: '/threads/thread-edited/runs/run-edited/stream',
            })
      }),
    )
    const user = userEvent.setup()
    renderButton()

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'Original run')
    await user.type(screen.getByLabelText('Request'), 'Original request')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))
    await screen.findByRole('alert')
    await user.type(screen.getByLabelText('Request'), ' with changed scope')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Launch run' }))

    await waitFor(() => expect(keys).toHaveLength(2))
    expect(keys[1]).not.toBe(keys[0])
  })

  it('recovers the original attempt when a failed payload is edited and reverted', async () => {
    const keys: string[] = []
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        const body = (await request.json()) as { idempotency_key: string }
        keys.push(body.idempotency_key)
        return keys.length === 1
          ? HttpResponse.json({ detail: 'ambiguous outage' }, { status: 503 })
          : HttpResponse.json({
              thread_id: 'thread-reverted',
              run_id: 'run-reverted',
              stream_url: '/threads/thread-reverted/runs/run-reverted/stream',
            })
      }),
    )
    const user = userEvent.setup()
    renderButton()

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'Reverted run')
    await user.type(screen.getByLabelText('Request'), 'Original request')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))
    await screen.findByRole('alert')

    const request = screen.getByLabelText('Request')
    await user.type(request, ' changed')
    await user.clear(request)
    await user.type(request, 'Original request')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))

    await waitFor(() => expect(keys).toHaveLength(2))
    expect(keys[1]).toBe(keys[0])
  })

  it('reuses an ambiguous attempt after the modal is closed and reopened', async () => {
    const keys: string[] = []
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        const body = (await request.json()) as { idempotency_key: string }
        keys.push(body.idempotency_key)
        return keys.length === 1
          ? HttpResponse.json({ detail: 'ambiguous outage' }, { status: 503 })
          : HttpResponse.json({
              thread_id: 'thread-reopened',
              run_id: 'run-reopened',
              stream_url: '/threads/thread-reopened/runs/run-reopened/stream',
            })
      }),
    )
    const user = userEvent.setup()
    renderButton()

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'Reopened run')
    await user.type(screen.getByLabelText('Request'), 'Same request')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))
    await screen.findByRole('alert')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'Reopened run')
    await user.type(screen.getByLabelText('Request'), 'Same request')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))

    await waitFor(() => expect(keys).toHaveLength(2))
    expect(keys[1]).toBe(keys[0])
  })

  it('reuses an ambiguous attempt after the page component reloads', async () => {
    const keys: string[] = []
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        const body = (await request.json()) as { idempotency_key: string }
        keys.push(body.idempotency_key)
        return keys.length === 1
          ? HttpResponse.json({ detail: 'ambiguous outage' }, { status: 503 })
          : HttpResponse.json({
              thread_id: 'thread-reloaded',
              run_id: 'run-reloaded',
              stream_url: '/threads/thread-reloaded/runs/run-reloaded/stream',
            })
      }),
    )
    const user = userEvent.setup()
    renderButton()

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'Reloaded run')
    await user.type(screen.getByLabelText('Request'), 'Same request after reload')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))
    await screen.findByRole('alert')
    cleanup()

    renderButton()
    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'Reloaded run')
    await user.type(screen.getByLabelText('Request'), 'Same request after reload')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))

    await waitFor(() => expect(keys).toHaveLength(2))
    expect(keys[1]).toBe(keys[0])
  })

  it('locks request fields while a launch attempt is in flight', async () => {
    let release!: () => void
    const blocked = new Promise<void>((resolve) => {
      release = resolve
    })
    let markStarted!: () => void
    const started = new Promise<void>((resolve) => {
      markStarted = resolve
    })
    server.use(
      http.post('*/v1/pipelines', async () => {
        markStarted()
        await blocked
        return HttpResponse.json({
          thread_id: 'thread-blocked',
          run_id: 'run-blocked',
          stream_url: '/threads/thread-blocked/runs/run-blocked/stream',
        })
      }),
    )
    const user = userEvent.setup()
    const router = renderButton()

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'Locked run')
    await user.type(screen.getByLabelText('Request'), 'Do not mutate in flight')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))
    await started

    expect(screen.getByLabelText('Title')).toBeDisabled()
    expect(screen.getByLabelText('Request')).toBeDisabled()
    expect(screen.getByLabelText('Project')).toBeDisabled()
    expect(screen.getByLabelText('Application (optional)')).toBeDisabled()

    release()
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/thread-blocked'))
  })

  it('does not accept a launch response after the semantic session changes', async () => {
    let release!: () => void
    const blocked = new Promise<void>((resolve) => {
      release = resolve
    })
    let markStarted!: () => void
    const started = new Promise<void>((resolve) => {
      markStarted = resolve
    })
    server.use(
      http.post('*/v1/pipelines', async () => {
        markStarted()
        await blocked
        return HttpResponse.json({
          thread_id: 'thread-old-session',
          run_id: 'run-old-session',
          stream_url: '/threads/thread-old-session/runs/run-old-session/stream',
        })
      }),
    )
    const user = userEvent.setup()
    const router = renderButton()

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'Old-session run')
    await user.type(screen.getByLabelText('Request'), 'Do not navigate after identity changes')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))
    await started

    bumpSessionRevision()
    release()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Authentication changed while the request was in flight',
    )
    expect(router.state.location.pathname).toBe('/runs')
  })

  it('retires the confirmed key and clears form state after a successful attempt', async () => {
    const keys: string[] = []
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        const body = (await request.json()) as { idempotency_key: string }
        keys.push(body.idempotency_key)
        return HttpResponse.json({
          thread_id: `thread-${keys.length}`,
          run_id: `run-${keys.length}`,
          stream_url: `/threads/thread-${keys.length}/runs/run-${keys.length}/stream`,
        })
      }),
    )
    const user = userEvent.setup()
    const router = renderButton({ persistent: true })

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.type(screen.getByLabelText('Title'), 'First run')
    await user.type(screen.getByLabelText('Request'), 'First request')
    await user.type(screen.getByLabelText('Application (optional)'), 'app-one')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/thread-1'))

    await user.click(screen.getByRole('button', { name: 'New run' }))
    expect(screen.getByLabelText('Title')).toHaveValue('')
    expect(screen.getByLabelText('Request')).toHaveValue('')
    expect(screen.getByLabelText('Project')).toHaveValue('demo')
    expect(screen.getByLabelText('Application (optional)')).toHaveValue('')

    await user.type(screen.getByLabelText('Title'), 'First run')
    await user.type(screen.getByLabelText('Request'), 'First request')
    await user.type(screen.getByLabelText('Application (optional)'), 'app-one')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))

    await waitFor(() => expect(keys).toHaveLength(2))
    expect(keys[1]).not.toBe(keys[0])
  })

  it('launches app-scoped operators only into an allowed project and application', async () => {
    let launchBody: {
      project_id?: string | null
      app_id?: string | null
      configurable?: Record<string, unknown> | null
    } | null = null
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        launchBody = (await request.json()) as typeof launchBody
        return HttpResponse.json({
          thread_id: 'thread-scoped',
          run_id: 'run-scoped',
          stream_url: '/threads/thread-scoped/runs/run-scoped/stream',
        })
      }),
    )
    const user = userEvent.setup()
    renderButton({
      authState: authenticatedState('operator', 'Scoped Ops', [
        { project_id: 'proj-b', app_id: 'app-checkout' },
        { project_id: 'proj-a', app_id: 'app-catalog' },
      ]),
    })

    await user.click(screen.getByRole('button', { name: 'New run' }))
    await user.selectOptions(
      screen.getByLabelText('Project / application scope'),
      JSON.stringify(['proj-b', 'app-checkout']),
    )
    expect(screen.queryByLabelText('Project')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Application (optional)')).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('Title'), 'Scoped run')
    await user.type(screen.getByLabelText('Request'), 'Stay inside app scope')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))

    await waitFor(() => expect(launchBody).not.toBeNull())
    expect(launchBody).toMatchObject({
      project_id: 'proj-b',
      app_id: 'app-checkout',
      configurable: { project_id: 'proj-b', app_id: 'app-checkout' },
    })
  })

  it('lets a project-wide operator narrow the launch to an application', async () => {
    let launchBody: { project_id?: string | null; app_id?: string | null } | null = null
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        launchBody = (await request.json()) as typeof launchBody
        return HttpResponse.json({
          thread_id: 'thread-project-wide',
          run_id: 'run-project-wide',
          stream_url: '/threads/thread-project-wide/runs/run-project-wide/stream',
        })
      }),
    )
    const user = userEvent.setup()
    renderButton({
      authState: authenticatedState('operator', 'Project Ops', [
        { project_id: 'proj-wide', app_id: null },
      ]),
    })

    await user.click(screen.getByRole('button', { name: 'New run' }))
    expect(screen.getByLabelText('Project / application scope')).toHaveValue(
      JSON.stringify(['proj-wide', null]),
    )
    await user.type(screen.getByLabelText('Application (optional)'), 'app-orders')
    await user.type(screen.getByLabelText('Title'), 'Project run')
    await user.type(screen.getByLabelText('Request'), 'Target one app')
    await user.click(screen.getByRole('button', { name: 'Launch run' }))

    await waitFor(() => expect(launchBody).not.toBeNull())
    expect(launchBody).toMatchObject({ project_id: 'proj-wide', app_id: 'app-orders' })
  })

  it('hides the entry point when a non-global operator has no usable scope', () => {
    renderButton({ authState: authenticatedState('operator', 'No Scope Ops', []) })

    expect(screen.queryByRole('button', { name: 'New run' })).not.toBeInTheDocument()
  })
})
