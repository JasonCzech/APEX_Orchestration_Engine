import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import {
  consumerWriteMutationKey,
  type ConsumerCreated,
} from '@/api/hooks/useConsumers'
import { bumpSessionRevision } from '@/auth/keyStorage'
import { authenticatedState, createTestQueryClient, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  CONSUMER_CI,
  CONSUMER_OPS,
  consumersReadHandlers,
  createConsumerHandler,
  deleteConsumerHandler,
  rotateConsumerHandler,
} from './adminTestHandlers'

function renderList() {
  return renderApp({
    initialEntries: ['/admin/consumers'],
    authState: authenticatedState('admin', 'Dash Ops', []),
  })
}

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function delayedCreated(name: string, apiKey: string): ConsumerCreated {
  return {
    ...CONSUMER_CI,
    id: 'cons-delayed',
    name,
    role: 'viewer',
    scopes: [],
    key_fingerprint: 'feedf00d',
    api_key: apiKey,
  }
}

describe('ConsumersPage', () => {
  it('renders the table with type/role chips, scope summaries and fingerprints', async () => {
    server.use(...consumersReadHandlers())
    renderList()

    const ci = await screen.findByTestId('consumer-row-cons-ci')
    expect(within(ci).getByText('ci-bot')).toBeInTheDocument()
    expect(within(ci).getByText('headless')).toHaveClass('dash-context-chip')
    expect(within(ci).getByText('operator')).toHaveClass('status-badge')
    // Multi-scope summary: first scope + overflow count.
    expect(within(ci).getByText('demo/app1 +2')).toBeInTheDocument()
    expect(within(ci).getByText('deadbeef')).toHaveClass('adm-fingerprint')

    const ops = screen.getByTestId('consumer-row-cons-ops')
    expect(within(ops).getByText('proj-alpha')).toBeInTheDocument()
  })

  it('reveals the created api_key exactly once behind the stored-confirmation gate', async () => {
    const create = createConsumerHandler('apex_key_only_shown_once_123')
    server.use(...consumersReadHandlers(), create.handler)
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'New consumer' }))
    const panel = screen.getByRole('form', { name: 'New consumer' })
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'ci-bot-2')
    await user.selectOptions(within(panel).getByRole('combobox', { name: 'Type' }), 'headless')
    await user.click(within(panel).getByRole('button', { name: 'operator' }))
    await user.type(within(panel).getByRole('textbox', { name: 'Scope 1 project' }), 'demo')
    await user.type(
      within(panel).getByRole('textbox', { name: 'Scope 1 app (optional)' }),
      'app1',
    )
    await user.click(within(panel).getByRole('button', { name: 'Create consumer' }))

    // Wire shape: role from the segmented control, scopes from the editor rows.
    await waitFor(() => expect(create.captured).toHaveLength(1))
    expect(create.captured[0]).toEqual({
      name: 'ci-bot-2',
      consumer_type: 'headless',
      role: 'operator',
      scopes: [{ project_id: 'demo', app_id: 'app1' }],
    })

    // Key reveal modal: key visible, warning shown, close gated on confirmation.
    const dialog = await screen.findByRole('dialog', { name: 'API key created' })
    expect(within(dialog).getByTestId('revealed-api-key')).toHaveTextContent(
      'apex_key_only_shown_once_123',
    )
    expect(dialog).toHaveTextContent('Store it now — it will never be shown again')
    const done = within(dialog).getByRole('button', { name: 'I’ve stored it' })
    expect(done).toBeDisabled()

    await user.click(
      within(dialog).getByRole('checkbox', { name: 'I have stored this key somewhere safe' }),
    )
    expect(done).toBeEnabled()
    await user.click(done)

    // Once dismissed the key is gone from the document — shown exactly once.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('apex_key_only_shown_once_123')).not.toBeInTheDocument()
  })

  it('keeps a delayed create protected across navigation and reveals it exactly once', async () => {
    const started = deferred()
    const release = deferred()
    const apiKey = 'apex_key_delayed_create'
    server.use(
      ...consumersReadHandlers(),
      http.post('*/v1/admin/consumers', async ({ request }) => {
        const body = (await request.json()) as { name: string }
        started.resolve()
        await release.promise
        return HttpResponse.json(delayedCreated(body.name, apiKey), { status: 201 })
      }),
    )
    const user = userEvent.setup()
    const { router } = renderList()

    await user.click(await screen.findByRole('button', { name: 'New consumer' }))
    const panel = screen.getByRole('form', { name: 'New consumer' })
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'delayed-bot')
    await user.click(within(panel).getByRole('button', { name: 'Create consumer' }))
    await started.promise

    const pendingUnload = new Event('beforeunload', { cancelable: true })
    expect(window.dispatchEvent(pendingUnload)).toBe(false)
    expect(pendingUnload.defaultPrevented).toBe(true)

    await act(async () => {
      void router.navigate('/settings')
    })
    const pendingGuard = await screen.findByRole('dialog', {
      name: 'API key request still pending',
    })
    expect(router.state.location.pathname).toBe('/admin/consumers')
    await user.click(within(pendingGuard).getByRole('button', { name: 'Leave anyway' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/settings'))

    release.resolve()
    const reveal = await screen.findByRole('dialog', { name: 'API key created' })
    expect(screen.getAllByTestId('revealed-api-key')).toHaveLength(1)
    expect(within(reveal).getByTestId('revealed-api-key')).toHaveTextContent(apiKey)

    const unacknowledgedUnload = new Event('beforeunload', { cancelable: true })
    expect(window.dispatchEvent(unacknowledgedUnload)).toBe(false)
    expect(unacknowledgedUnload.defaultPrevented).toBe(true)

    await act(async () => {
      void router.navigate('/admin/consumers')
    })
    await waitFor(() =>
      expect(
        Array.from(router.state.blockers.values()).some((blocker) => blocker.state === 'blocked'),
      ).toBe(true),
    )
    expect(router.state.location.pathname).toBe('/settings')

    await user.click(
      within(reveal).getByRole('checkbox', {
        name: 'I have stored this key somewhere safe',
      }),
    )
    await user.click(within(reveal).getByRole('button', { name: 'I’ve stored it' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/admin/consumers'))
    expect(screen.queryByText(apiKey)).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'API key created' })).not.toBeInTheDocument()
  })

  it('rotate confirms first, then reveals the new key in the same gated modal', async () => {
    const rotate = rotateConsumerHandler(CONSUMER_CI, 'apex_key_rotated_456')
    server.use(...consumersReadHandlers(), rotate.handler)
    const user = userEvent.setup()
    renderList()

    const row = await screen.findByTestId('consumer-row-cons-ci')
    await user.click(within(row).getByRole('button', { name: 'Consumer actions: ci-bot' }))
    await user.click(screen.getByRole('menuitem', { name: 'Rotate key…' }))

    const confirm = await screen.findByRole('dialog', { name: 'Rotate key for ci-bot' })
    expect(confirm).toHaveTextContent('remains valid for five minutes')
    await user.click(within(confirm).getByRole('button', { name: 'Rotate key' }))

    const reveal = await screen.findByRole('dialog', { name: 'API key rotated' })
    expect(within(reveal).getByTestId('revealed-api-key')).toHaveTextContent(
      'apex_key_rotated_456',
    )
    expect(rotate.callCount()).toBe(1)
    expect(rotate.captured).toEqual([{ grace_period_seconds: 300 }])
    expect(
      within(reveal).getByRole('button', { name: 'I’ve stored it' }),
    ).toBeDisabled()
    expect(
      within(reveal).queryByRole('button', { name: /Use this key for this dashboard/ }),
    ).not.toBeInTheDocument()
  })

  it('preserves delayed self-rotation metadata after navigation and reveals once', async () => {
    const started = deferred()
    const release = deferred()
    const apiKey = 'apex_key_delayed_self_rotate'
    let calls = 0
    server.use(
      ...consumersReadHandlers(),
      http.post('*/v1/admin/consumers/:id/rotate', async ({ params }) => {
        expect(params.id).toBe(CONSUMER_OPS.id)
        calls += 1
        started.resolve()
        await release.promise
        return HttpResponse.json({
          ...CONSUMER_OPS,
          key_fingerprint: 'r0t4t3d0',
          api_key: apiKey,
        } satisfies ConsumerCreated)
      }),
    )
    const user = userEvent.setup()
    const { router } = renderList()

    const row = await screen.findByTestId('consumer-row-cons-ops')
    await user.click(within(row).getByRole('button', { name: 'Consumer actions: Dash Ops' }))
    await user.click(screen.getByRole('menuitem', { name: 'Rotate key…' }))
    await user.click(
      within(await screen.findByRole('dialog', { name: 'Rotate key for Dash Ops' })).getByRole(
        'button',
        { name: 'Rotate key' },
      ),
    )
    await started.promise

    await act(async () => {
      void router.navigate('/settings')
    })
    await user.click(
      within(
        await screen.findByRole('dialog', { name: 'API key request still pending' }),
      ).getByRole('button', { name: 'Leave anyway' }),
    )
    await waitFor(() => expect(router.state.location.pathname).toBe('/settings'))

    release.resolve()
    const reveal = await screen.findByRole('dialog', { name: 'API key rotated' })
    expect(calls).toBe(1)
    expect(screen.getAllByTestId('revealed-api-key')).toHaveLength(1)
    expect(within(reveal).getByTestId('revealed-api-key')).toHaveTextContent(apiKey)
    expect(
      within(reveal).getByRole('button', {
        name: 'Use this key for this dashboard (recommended)',
      }),
    ).toBeInTheDocument()

    await user.click(
      within(reveal).getByRole('checkbox', {
        name: 'I have stored this key somewhere safe',
      }),
    )
    await user.click(within(reveal).getByRole('button', { name: 'I’ve stored it' }))
    expect(screen.queryByText(apiKey)).not.toBeInTheDocument()
  })

  it('recommends switching the dashboard when the current consumer rotates itself', async () => {
    const rotate = rotateConsumerHandler(CONSUMER_OPS, 'apex_key_self_rotated')
    server.use(...consumersReadHandlers(), rotate.handler)
    const user = userEvent.setup()
    renderList()

    const row = await screen.findByTestId('consumer-row-cons-ops')
    await user.click(within(row).getByRole('button', { name: 'Consumer actions: Dash Ops' }))
    await user.click(screen.getByRole('menuitem', { name: 'Rotate key…' }))
    await user.click(
      within(await screen.findByRole('dialog', { name: 'Rotate key for Dash Ops' })).getByRole(
        'button',
        { name: 'Rotate key' },
      ),
    )

    const reveal = await screen.findByRole('dialog', { name: 'API key rotated' })
    expect(reveal).toHaveTextContent('previous key remains valid for five minutes')
    await user.click(within(reveal).getByLabelText('I have stored this key somewhere safe'))
    await user.click(
      within(reveal).getByRole('button', {
        name: 'Use this key for this dashboard (recommended)',
      }),
    )
    expect(window.localStorage.getItem('apex.apiKey')).toBe('apex_key_self_rotated')
  })

  it('drops a delayed one-time key when the authenticated session changes', async () => {
    const started = deferred()
    const release = deferred()
    const apiKey = 'apex_key_from_stale_session'
    server.use(
      ...consumersReadHandlers(),
      http.post('*/v1/admin/consumers', async ({ request }) => {
        const body = (await request.json()) as { name: string }
        started.resolve()
        await release.promise
        return HttpResponse.json(delayedCreated(body.name, apiKey), { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderList()

    await user.click(await screen.findByRole('button', { name: 'New consumer' }))
    const panel = screen.getByRole('form', { name: 'New consumer' })
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'stale-session-bot')
    await user.click(within(panel).getByRole('button', { name: 'Create consumer' }))
    await started.promise

    act(() => {
      bumpSessionRevision()
    })
    release.resolve()

    expect(await within(panel).findByRole('alert')).toHaveTextContent(
      'Authentication changed while the request was in flight.',
    )
    expect(screen.queryByRole('dialog', { name: 'API key created' })).not.toBeInTheDocument()
    expect(screen.queryByText(apiKey)).not.toBeInTheDocument()
  })

  it('disables same-consumer row writes while a matching mutation is pending', async () => {
    server.use(...consumersReadHandlers())
    const user = userEvent.setup()
    const queryClient = createTestQueryClient()
    renderApp({
      initialEntries: ['/admin/consumers'],
      authState: authenticatedState('admin', 'Dash Ops', []),
      queryClient,
    })

    const row = await screen.findByTestId('consumer-row-cons-ci')
    const release = deferred()
    const mutation = queryClient.getMutationCache().build<void, Error, void, unknown>(queryClient, {
      mutationKey: consumerWriteMutationKey(CONSUMER_CI.id),
      mutationFn: () => release.promise,
    })
    let mutationPromise!: Promise<void>
    act(() => {
      mutationPromise = mutation.execute(undefined)
    })
    await waitFor(() =>
      expect(
        queryClient.isMutating({ mutationKey: consumerWriteMutationKey(CONSUMER_CI.id) }),
      ).toBe(1),
    )

    await user.click(within(row).getByRole('button', { name: 'Consumer actions: ci-bot' }))
    expect(screen.getByRole('menuitem', { name: 'Open' })).toBeEnabled()
    expect(screen.getByRole('menuitem', { name: 'Edit' })).toBeDisabled()
    expect(screen.getByRole('menuitem', { name: 'Rotate key…' })).toBeDisabled()
    expect(screen.getByRole('menuitem', { name: 'Delete…' })).toBeDisabled()

    await act(async () => {
      release.resolve()
      await mutationPromise
    })
    await waitFor(() => expect(screen.getByRole('menuitem', { name: 'Edit' })).toBeEnabled())
  })

  it("maps the self-delete 409 to an inline 'your own consumer' message", async () => {
    const del = deleteConsumerHandler(CONSUMER_OPS.id)
    server.use(...consumersReadHandlers(), del.handler)
    const user = userEvent.setup()
    renderList()

    const row = await screen.findByTestId('consumer-row-cons-ops')
    await user.click(within(row).getByRole('button', { name: 'Consumer actions: Dash Ops' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete…' }))

    const dialog = await screen.findByRole('dialog', { name: 'Delete consumer Dash Ops' })
    await user.click(within(dialog).getByRole('button', { name: 'Delete consumer' }))

    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent('You cannot delete your own consumer')
    expect(del.captured).toHaveLength(0)
    // The modal stays open; the destructive button locks out a retry.
    expect(within(dialog).getByRole('button', { name: 'Delete consumer' })).toBeDisabled()
  })

  it("shows the 'Requires admin role' empty state to non-admins", async () => {
    server.use(...consumersReadHandlers())
    renderApp({
      initialEntries: ['/admin/consumers'],
      authState: authenticatedState('viewer'),
    })

    expect(await screen.findByRole('heading', { name: 'Requires admin role' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New consumer' })).not.toBeInTheDocument()
  })

  it('blocks delegated grants outside a scoped administrator\'s access', async () => {
    server.use(...consumersReadHandlers([CONSUMER_OPS]))
    const user = userEvent.setup()
    renderApp({
      initialEntries: ['/admin/consumers'],
      authState: authenticatedState('admin', 'Scoped Admin', [
        { project_id: 'proj-alpha', app_id: 'checkout' },
      ]),
    })

    await user.click(await screen.findByRole('button', { name: 'New consumer' }))
    const panel = screen.getByRole('form', { name: 'New consumer' })
    await user.type(within(panel).getByRole('textbox', { name: 'Name' }), 'delegated')
    await user.type(within(panel).getByRole('textbox', { name: 'Scope 1 project' }), 'proj-alpha')
    expect(within(panel).getByRole('alert')).toHaveTextContent('outside your access')
    expect(within(panel).getByRole('button', { name: 'Create consumer' })).toBeDisabled()

    await user.type(
      within(panel).getByRole('textbox', { name: 'Scope 1 app (optional)' }),
      'checkout',
    )
    expect(within(panel).queryByRole('alert')).not.toBeInTheDocument()
    expect(within(panel).getByRole('button', { name: 'Create consumer' })).toBeEnabled()
  })
})
