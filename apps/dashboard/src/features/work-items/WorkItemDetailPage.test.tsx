import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import { ITEM_BUG, ITEM_PAYMENT, enrichHandler, getItemHandler } from './workItemsTestHandlers'

function renderDetail(path = '/work-items/jira/PHX-101', role: 'operator' | 'viewer' = 'operator') {
  return renderApp({
    initialEntries: [path],
    authState: authenticatedState(role),
  })
}

describe('WorkItemDetailPage', () => {
  beforeEach(() => window.sessionStorage.clear())

  it('renders the item and enriches it (POST shape + refreshed detail)', async () => {
    const enrich = enrichHandler({ ...ITEM_PAYMENT, status: 'blocked' })
    server.use(getItemHandler([ITEM_PAYMENT]), enrich.handler)
    const user = userEvent.setup()
    renderDetail()

    expect(
      await screen.findByRole('heading', {
        name: 'Checkout retries drop payments',
      }),
    ).toBeInTheDocument()
    expect(screen.getByText('story')).toHaveClass('dash-context-chip')
    expect(screen.getByText('open')).toHaveClass('status-badge')
    // Blank-line separated description renders as paragraphs.
    expect(screen.getByText('Retries on the payment gateway drop the cart.')).toBeInTheDocument()
    expect(screen.getByText('Observed on staging since 1.42.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open PHX-101 in tracker' })).toHaveAttribute(
      'href',
      ITEM_PAYMENT.url,
    )

    await user.click(screen.getByRole('button', { name: 'Enrich' }))
    const dialog = await screen.findByRole('dialog', {
      name: 'Enrich PHX-101',
    })
    const fields = within(dialog).getByRole('textbox', { name: 'Fields JSON' })

    // Invalid JSON blocks submission with an inline message.
    await user.clear(fields)
    await user.paste('not json')
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Fields are not valid JSON.')
    expect(within(dialog).getByRole('button', { name: 'Enrich item' })).toBeDisabled()

    await user.clear(fields)
    await user.paste('{"priority": "P1"}')
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Enrich comment' }),
      'Re-triaged after load test',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Enrich item' }))

    await waitFor(() =>
      expect(enrich.captured).toEqual([
        { fields: { priority: 'P1' }, comment: 'Re-triaged after load test' },
      ]),
    )
    expect(enrich.idempotencyKeys[0]).toMatch(/^enrich-/)
    expect(enrich.connectionIds).toEqual(['conn-jira'])
    // The 200 body replaces the cached detail — status chip refreshes.
    expect(await screen.findByText('blocked')).toHaveClass('status-badge')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('refreshes a generic tracker detail route after enrichment', async () => {
    const enrich = enrichHandler({ ...ITEM_PAYMENT, status: 'blocked' })
    server.use(getItemHandler([ITEM_PAYMENT]), enrich.handler)
    const user = userEvent.setup()
    renderDetail('/work-items/tracker/PHX-101?connection_id=conn-jira')

    await screen.findByRole('heading', { name: ITEM_PAYMENT.title })
    expect(screen.getByText('open')).toHaveClass('status-badge')
    await user.click(screen.getByRole('button', { name: 'Enrich' }))
    const dialog = await screen.findByRole('dialog', { name: 'Enrich PHX-101' })
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Enrich comment' }),
      'Refresh the generic route',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Enrich item' }))

    expect(await screen.findByText('blocked')).toHaveClass('status-badge')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('locks enrichment across route remounts and clears the durable attempt after success', async () => {
    let markEnrichStarted!: () => void
    const enrichStarted = new Promise<void>((resolve) => {
      markEnrichStarted = resolve
    })
    let releaseEnrich!: () => void
    const enrichRelease = new Promise<void>((resolve) => {
      releaseEnrich = resolve
    })
    server.use(
      getItemHandler([ITEM_PAYMENT, ITEM_BUG]),
      http.post('*/v1/work-tracking/items/PHX-101/enrich', async () => {
        markEnrichStarted()
        await enrichRelease
        return HttpResponse.json({
          ...ITEM_PAYMENT,
          status: 'blocked',
          connection_id: 'conn-jira',
          provider: 'jira',
        })
      }),
    )
    const user = userEvent.setup()
    const { router } = renderDetail()

    await screen.findByRole('heading', { name: ITEM_PAYMENT.title })
    await user.click(screen.getByRole('button', { name: 'Enrich' }))
    await user.type(
      screen.getByRole('textbox', { name: 'Enrich comment' }),
      'Deferred update',
    )
    await user.click(screen.getByRole('button', { name: 'Enrich item' }))
    await enrichStarted

    await act(async () => {
      await router.navigate('/work-items/jira/PHX-102')
      await router.navigate('/work-items/jira/PHX-101')
    })
    await screen.findByRole('heading', { name: ITEM_PAYMENT.title })
    expect(screen.getByRole('button', { name: 'Enrich' })).toBeDisabled()

    releaseEnrich()
    expect(await screen.findByText('blocked')).toHaveClass('status-badge')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enrich' })).toBeEnabled())
    await waitFor(() =>
      expect(
        Object.keys(window.sessionStorage).filter((key) =>
          key.startsWith('apex.work-items.enrich.v2'),
        ),
      ).toHaveLength(0),
    )
  })

  it('blocks enrichment before the request when safe retry storage is unavailable', async () => {
    const enrich = enrichHandler({ ...ITEM_PAYMENT, status: 'blocked' })
    server.use(getItemHandler([ITEM_PAYMENT]), enrich.handler)
    const user = userEvent.setup()
    renderDetail()

    await screen.findByRole('heading', { name: ITEM_PAYMENT.title })
    await user.click(screen.getByRole('button', { name: 'Enrich' }))
    const dialog = await screen.findByRole('dialog', { name: 'Enrich PHX-101' })
    await user.type(
      within(dialog).getByRole('textbox', { name: 'Enrich comment' }),
      'Must be safely retryable',
    )
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)

    try {
      const submit = within(dialog).getByRole('button', { name: 'Enrich item' })
      await user.click(submit)

      expect(await within(dialog).findByRole('alert')).toHaveTextContent(
        'Enrich blocked: Safe retry storage is unavailable',
      )
      expect(enrich.captured).toEqual([])
      expect(submit).toBeDisabled()
    } finally {
      setItem.mockRestore()
    }
  })

  it('renders the Item not found empty state on 404', async () => {
    server.use(getItemHandler([]))
    renderDetail('/work-items/jira/PHX-404')

    expect(await screen.findByRole('heading', { name: 'Item not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to the console' })).toHaveAttribute(
      'href',
      '/work-items',
    )
  })

  it('hides Enrich from viewers', async () => {
    server.use(getItemHandler([ITEM_PAYMENT]))
    renderDetail('/work-items/jira/PHX-101', 'viewer')

    await screen.findByRole('heading', {
      name: 'Checkout retries drop payments',
    })
    expect(screen.queryByRole('button', { name: 'Enrich' })).not.toBeInTheDocument()
  })

  it('hides project-wide enrichment from app-only operators', async () => {
    server.use(getItemHandler([ITEM_PAYMENT]))
    const base = authenticatedState('operator')
    if (base.status !== 'authenticated') throw new Error('expected authenticated test state')
    const consumer = {
      ...base.consumer,
      scopes: [{ project_id: 'proj-alpha', app_id: 'app-one' }],
    }
    renderApp({
      initialEntries: ['/work-items/jira/PHX-101'],
      authState: {
        ...base,
        consumer,
        systemInfo: { ...base.systemInfo, consumer },
      },
    })

    await screen.findByRole('heading', {
      name: 'Checkout retries drop payments',
    })
    expect(screen.queryByRole('button', { name: 'Enrich' })).not.toBeInTheDocument()
  })

  it('closes and reinitializes enrichment when a cached item route replaces it', async () => {
    server.use(getItemHandler([ITEM_PAYMENT, ITEM_BUG]))
    const user = userEvent.setup()
    const { router } = renderDetail('/work-items/jira/PHX-102')

    await screen.findByRole('heading', { name: ITEM_BUG.title })
    await act(async () => router.navigate('/work-items/jira/PHX-101'))
    await screen.findByRole('heading', { name: ITEM_PAYMENT.title })
    await user.click(screen.getByRole('button', { name: 'Enrich' }))
    await user.type(screen.getByRole('textbox', { name: 'Enrich comment' }), 'A-only draft')

    await act(async () => router.navigate('/work-items/jira/PHX-102'))
    await screen.findByRole('heading', { name: ITEM_BUG.title })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Enrich' }))
    const dialog = screen.getByRole('dialog', { name: 'Enrich PHX-102' })
    expect(within(dialog).getByRole('textbox', { name: 'Fields JSON' })).toHaveValue('{}')
    expect(within(dialog).getByRole('textbox', { name: 'Enrich comment' })).toHaveValue('')
  })
})
