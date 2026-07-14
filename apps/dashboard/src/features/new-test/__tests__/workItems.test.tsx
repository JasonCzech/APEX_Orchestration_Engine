/**
 * Work-items step: NL translate -> provider/confidence chips + editable query
 * -> execute -> selectable results -> removable chips; direct key add with
 * validate-on-add through getWorkItem.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { server } from '@/test/server'

import { flushAndUnmountWizard, installWizardHandlers, renderWizard } from './wizardTestUtils'

const TRANSLATED = { provider: 'jira', query: 'project = PHX AND status = Open', confidence: 0.82 }

const ITEMS = [
  { key: 'PHX-101', title: 'Slow checkout', kind: 'story', status: 'open', description: '', url: 'https://jira/PHX-101' },
  { key: 'PHX-102', title: 'Cart race', kind: 'bug', status: 'open', description: '', url: null },
]

describe('WorkItemsStep', () => {
  it('translate -> execute -> select flows into removable chips', async () => {
    installWizardHandlers()
    const projects: string[] = []
    server.use(
      http.post('*/v1/work-tracking/query/translate', ({ request }) => {
        projects.push(new URL(request.url).searchParams.get('project') ?? '')
        return HttpResponse.json(TRANSLATED)
      }),
      http.post('*/v1/work-tracking/query/execute', ({ request }) => {
        projects.push(new URL(request.url).searchParams.get('project') ?? '')
        return HttpResponse.json({ items: ITEMS, total: 2 })
      }),
    )
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=work-items')

    await user.type(
      await screen.findByLabelText('Find by description'),
      'open phoenix stories',
    )
    await user.click(screen.getByRole('button', { name: 'Translate' }))

    // Provider + confidence chips and the editable provider query.
    const translated = await screen.findByTestId('translated-query')
    expect(within(translated).getByText('jira')).toBeInTheDocument()
    expect(within(translated).getByText('confidence 82%')).toBeInTheDocument()
    const queryInput = screen.getByLabelText('Provider query')
    expect(queryInput).toHaveValue(TRANSLATED.query)
    await user.type(queryInput, ' ORDER BY rank') // editable before execute

    await user.click(screen.getByRole('button', { name: 'Run query' }))
    expect(await screen.findByText('Slow checkout')).toBeInTheDocument()
    expect(projects).toEqual(['demo', 'demo'])

    await user.click(screen.getByLabelText('Select PHX-101'))
    await user.click(screen.getByLabelText('Select PHX-102'))
    const chips = screen.getByTestId('selected-work-items')
    expect(within(chips).getByText('PHX-101')).toBeInTheDocument()
    expect(within(chips).getByText('PHX-102')).toBeInTheDocument()

    // Chips are removable and the table checkbox follows.
    await user.click(screen.getByRole('button', { name: 'Remove PHX-102' }))
    expect(within(chips).queryByText('PHX-102')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Select PHX-102')).not.toBeChecked()
    await flushAndUnmountWizard(rendered)
  })

  it('direct key add validates via getWorkItem and surfaces failures inline', async () => {
    installWizardHandlers()
    const projects: string[] = []
    server.use(
      http.get('*/v1/work-tracking/items/:key', ({ params, request }) => {
        projects.push(new URL(request.url).searchParams.get('project') ?? '')
        return params['key'] === 'PHX-241'
          ? HttpResponse.json({ key: 'PHX-241', title: 'Found', kind: 'story', status: 'open', description: '' })
          : HttpResponse.json({ detail: 'unknown work item' }, { status: 404 })
      }),
    )
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=work-items')

    const keyInput = await screen.findByLabelText('Add by key')
    await user.type(keyInput, 'PHX-241')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    const chips = await screen.findByTestId('selected-work-items')
    expect(within(chips).getByText('PHX-241')).toBeInTheDocument()
    expect(keyInput).toHaveValue('') // cleared on success

    await user.type(keyInput, 'PHX-999')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.getByText('unknown work item')).toBeInTheDocument())
    expect(within(chips).queryByText('PHX-999')).not.toBeInTheDocument()
    expect(projects).toEqual(['demo', 'demo'])
    await flushAndUnmountWizard(rendered)
  })
})
