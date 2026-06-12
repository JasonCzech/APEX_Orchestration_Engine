import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import { ITEM_PAYMENT, enrichHandler, getItemHandler } from './workItemsTestHandlers'

function renderDetail(path = '/work-items/jira/PHX-101', role: 'operator' | 'viewer' = 'operator') {
  return renderApp({ initialEntries: [path], authState: authenticatedState(role) })
}

describe('WorkItemDetailPage', () => {
  it('renders the item and enriches it (POST shape + refreshed detail)', async () => {
    const enrich = enrichHandler({ ...ITEM_PAYMENT, status: 'blocked' })
    server.use(getItemHandler([ITEM_PAYMENT]), enrich.handler)
    const user = userEvent.setup()
    renderDetail()

    expect(
      await screen.findByRole('heading', { name: 'Checkout retries drop payments' }),
    ).toBeInTheDocument()
    expect(screen.getByText('story')).toHaveClass('dash-context-chip')
    expect(screen.getByText('open')).toHaveClass('status-badge')
    // Blank-line separated description renders as paragraphs.
    expect(
      screen.getByText('Retries on the payment gateway drop the cart.'),
    ).toBeInTheDocument()
    expect(screen.getByText('Observed on staging since 1.42.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open PHX-101 in tracker' })).toHaveAttribute(
      'href',
      ITEM_PAYMENT.url,
    )

    await user.click(screen.getByRole('button', { name: 'Enrich' }))
    const dialog = await screen.findByRole('dialog', { name: 'Enrich PHX-101' })
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
    // The 200 body replaces the cached detail — status chip refreshes.
    expect(await screen.findByText('blocked')).toHaveClass('status-badge')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
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

    await screen.findByRole('heading', { name: 'Checkout retries drop payments' })
    expect(screen.queryByRole('button', { name: 'Enrich' })).not.toBeInTheDocument()
  })
})
