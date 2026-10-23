import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import type { Environment } from '@/api/hooks/useEnvironments'
import { queryKeys } from '@/api/queryKeys'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'
import { ENV_STAGING, ENV_PROD, catalogReadHandlers, inventoryHandler, inventoryOf } from './environmentsTestHandlers'

const UNAPPROVED: Environment = { ...ENV_STAGING, target_approved: false, target_version: 0 }

function setupTarget(environment = UNAPPROVED) {
  let current = environment
  server.use(
    http.get(`*/v1/catalog/environments/${environment.id}`, () => HttpResponse.json(current)),
    ...catalogReadHandlers(undefined, [environment, ENV_PROD]),
    inventoryHandler(inventoryOf(environment.id, null)),
  )
  return { update: (next: Environment) => { current = next } }
}

function renderTarget(role: 'admin' | 'operator' | 'viewer' = 'admin', global = true) {
  return renderApp({
    initialEntries: [`/environments/${UNAPPROVED.id}`],
    authState: authenticatedState(role, 'Target reviewer', global ? [] : [{ project_id: 'proj-alpha', app_id: null }]),
  })
}

describe('Environment target approval', () => {
  it('requires explicit review and sends the reviewed version without replaying the target URL', async () => {
    const target = setupTarget()
    const requests: unknown[] = []
    server.use(http.post('*/v1/catalog/environments/:id/target-approval', async ({ request }) => {
      requests.push(await request.json())
      const approved = { ...UNAPPROVED, target_approved: true, target_version: 1 }
      target.update(approved)
      return HttpResponse.json(approved)
    }))
    const user = userEvent.setup()
    renderTarget()
    const panel = await screen.findByRole('region', { name: 'Execution target' })
    expect(within(panel).getByText('Needs approval')).toBeVisible()
    await user.click(within(panel).getByRole('button', { name: 'Review and approve target' }))
    const dialog = screen.getByRole('dialog', { name: 'Approve execution target' })
    expect(within(dialog).getByText(UNAPPROVED.base_url!)).toBeVisible()
    expect(requests).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Approve target' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(requests).toEqual([{ expected_target_version: 0 }])
    expect(await within(panel).findByText('Approved')).toBeVisible()
  })

  it('shows a stale-version refusal, refreshes the target, and requires a new review', async () => {
    const target = setupTarget()
    const newer = { ...UNAPPROVED, base_url: 'https://new.checkout.example.com', target_version: 2 }
    const requests: unknown[] = []
    server.use(http.post('*/v1/catalog/environments/:id/target-approval', async ({ request }) => {
      requests.push(await request.json())
      if (requests.length === 1) {
        target.update(newer)
        return HttpResponse.json({ title: 'Environment target changed. Reload before approving.' }, { status: 409 })
      }
      const approved = { ...newer, target_approved: true, target_version: 3 }
      target.update(approved)
      return HttpResponse.json(approved)
    }))
    const user = userEvent.setup()
    renderTarget()
    await user.click(await screen.findByRole('button', { name: 'Review and approve target' }))
    let dialog = screen.getByRole('dialog', { name: 'Approve execution target' })
    await user.click(within(dialog).getByRole('button', { name: 'Approve target' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Environment target changed')
    expect(within(dialog).getByRole('button', { name: 'Approve target' })).toBeDisabled()
    await within(dialog).findByText(newer.base_url)
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Review and approve target' }))
    dialog = screen.getByRole('dialog', { name: 'Approve execution target' })
    expect(dialog).toHaveTextContent('If the target changes before you approve, the approval is refused')
    await user.click(within(dialog).getByRole('button', { name: 'Approve target' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(requests).toEqual([{ expected_target_version: 0 }, { expected_target_version: 2 }])
  })

  it('disables confirmation with an explanation if target data changes during review', async () => {
    setupTarget()
    const user = userEvent.setup()
    const { queryClient } = renderTarget()
    await user.click(await screen.findByRole('button', { name: 'Review and approve target' }))
    await act(async () => {
      queryClient.setQueryData(queryKeys.catalog.environment(UNAPPROVED.id), { ...UNAPPROVED, target_version: 1 })
    })
    const dialog = screen.getByRole('dialog', { name: 'Approve execution target' })
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('target changed during this review')
    expect(within(dialog).getByRole('button', { name: 'Approve target' })).toBeDisabled()
  })

  it.each(['operator', 'viewer', 'admin'] as const)('shows target state but no approval controls to a scoped %s', async role => {
    setupTarget()
    const user = userEvent.setup()
    renderTarget(role, false)
    const panel = await screen.findByRole('region', { name: 'Execution target' })
    expect(within(panel).getByText('Needs approval')).toBeVisible()
    expect(panel).toHaveTextContent('Ask a global administrator')
    expect(within(panel).queryByRole('button')).not.toBeInTheDocument()
    if (role !== 'viewer') {
      await user.click(screen.getByRole('button', { name: /^Edit$/ }))
      const form = screen.getByRole('form', { name: 'Edit environment' })
      expect(within(form).queryByRole('textbox', { name: 'Execution target URL' })).not.toBeInTheDocument()
      expect(within(form).getByRole('combobox', { name: 'Platform' })).toBeEnabled()
    } else expect(screen.queryByRole('button', { name: /^Edit$/ })).not.toBeInTheDocument()
  })

  it('offers target configuration to a global admin when the URL is absent', async () => {
    setupTarget({ ...UNAPPROVED, base_url: null })
    const user = userEvent.setup()
    renderTarget()
    await user.click(await screen.findByRole('button', { name: 'Configure target URL' }))
    expect(screen.getByRole('textbox', { name: 'Execution target URL' })).toHaveValue('')
    expect(screen.queryByRole('button', { name: 'Review and approve target' })).not.toBeInTheDocument()
  })

  it('does not carry an open review to another cached environment', async () => {
    setupTarget()
    const user = userEvent.setup()
    const { router, queryClient } = renderTarget()
    queryClient.setQueryData(queryKeys.catalog.environment(ENV_PROD.id), ENV_PROD)
    await user.click(await screen.findByRole('button', { name: 'Review and approve target' }))
    await act(async () => router.navigate(`/environments/${ENV_PROD.id}`))
    await screen.findByRole('heading', { name: 'production' })
    expect(screen.queryByRole('dialog', { name: 'Approve execution target' })).not.toBeInTheDocument()
  })

  it('allows an operator to edit metadata without resubmitting unchanged reserved options', async () => {
    const environment = { ...UNAPPROVED, options: { namespace: 'checkout-stg', _apex_trusted_private_host: true } }
    const target = setupTarget(environment)
    const requests: unknown[] = []
    server.use(http.patch('*/v1/catalog/environments/:id', async ({ request }) => {
      const body = await request.json()
      requests.push(body)
      const updated = { ...environment, kind: 'vm' }
      target.update(updated)
      return HttpResponse.json(updated)
    }))
    const user = userEvent.setup()
    renderTarget('operator', false)
    await user.click(await screen.findByRole('button', { name: /^Edit$/ }))
    const form = screen.getByRole('form', { name: 'Edit environment' })
    await user.selectOptions(within(form).getByRole('combobox', { name: 'Platform' }), 'vm')
    const save = within(form).getByRole('button', { name: 'Save changes' })
    expect(save).toBeEnabled()
    const options = within(form).getByRole('textbox', { name: 'Options JSON' })
    await user.clear(options)
    await user.paste('{"namespace":"checkout-stg"}')
    expect(save).toBeDisabled()
    expect(within(form).getByRole('alert')).toHaveTextContent('global administrator')
    await user.clear(options)
    await user.paste(JSON.stringify(environment.options, null, 2))
    expect(save).toBeEnabled()
    await user.click(save)
    await waitFor(() => expect(requests).toEqual([{ kind: 'vm' }]))
    await waitFor(() => expect(screen.queryByRole('form', { name: 'Edit environment' })).not.toBeInTheDocument())
  })

  it('keeps target approval and metadata writes serialized while confirmation is pending', async () => {
    const target = setupTarget()
    let release!: () => void
    const pending = new Promise<void>(resolve => { release = resolve })
    let requested = false
    server.use(http.post('*/v1/catalog/environments/:id/target-approval', async () => {
      requested = true
      await pending
      const approved = { ...UNAPPROVED, target_approved: true, target_version: 1 }
      target.update(approved)
      return HttpResponse.json(approved)
    }))
    const user = userEvent.setup()
    renderTarget()
    const edit = await screen.findByRole('button', { name: /^Edit$/ })
    await user.click(screen.getByRole('button', { name: 'Review and approve target' }))
    const dialog = screen.getByRole('dialog', { name: 'Approve execution target' })
    await user.click(within(dialog).getByRole('button', { name: 'Approve target' }))
    try {
      await waitFor(() => expect(requested).toBe(true))
      expect(edit).toBeDisabled()
      expect(within(dialog).getByRole('button', { name: 'Approving…' })).toBeDisabled()
      expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled()
      await user.keyboard('{Escape}')
      expect(dialog).toBeInTheDocument()
    } finally { release() }
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(edit).toBeEnabled()
  })
})
