import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
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
    authState: authenticatedState('admin'),
  })
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

  it('rotate confirms first, then reveals the new key in the same gated modal', async () => {
    const rotate = rotateConsumerHandler(CONSUMER_CI, 'apex_key_rotated_456')
    server.use(...consumersReadHandlers(), rotate.handler)
    const user = userEvent.setup()
    renderList()

    const row = await screen.findByTestId('consumer-row-cons-ci')
    await user.click(within(row).getByRole('button', { name: 'Consumer actions: ci-bot' }))
    await user.click(screen.getByRole('menuitem', { name: 'Rotate key…' }))

    const confirm = await screen.findByRole('dialog', { name: 'Rotate key for ci-bot' })
    expect(confirm).toHaveTextContent('revokes the current one immediately')
    await user.click(within(confirm).getByRole('button', { name: 'Rotate key' }))

    const reveal = await screen.findByRole('dialog', { name: 'API key rotated' })
    expect(within(reveal).getByTestId('revealed-api-key')).toHaveTextContent(
      'apex_key_rotated_456',
    )
    expect(rotate.callCount()).toBe(1)
    expect(
      within(reveal).getByRole('button', { name: 'I’ve stored it' }),
    ).toBeDisabled()
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
})
