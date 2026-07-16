/**
 * Work-items step: NL translate -> provider/confidence chips + editable query
 * -> execute -> selectable results -> removable chips; direct key add with
 * validate-on-add through getWorkItem.
 */
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { server } from '@/test/server'

import {
  fillScope,
  flushAndUnmountWizard,
  installWizardHandlers,
  renderWizard,
} from './wizardTestUtils'

const TRANSLATED = {
  provider: 'jira',
  query: 'project = PHX AND status = Open',
  confidence: 0.82,
  connection_id: 'conn-jira',
}

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
        return HttpResponse.json({
          items: ITEMS,
          total: 2,
          connection_id: 'conn-jira',
          provider: 'jira',
        })
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

  it('retires prior results when a replacement provider query fails', async () => {
    installWizardHandlers()
    server.use(
      http.post('*/v1/work-tracking/query/translate', () => HttpResponse.json(TRANSLATED)),
      http.post('*/v1/work-tracking/query/execute', async ({ request }) => {
        const body = (await request.json()) as { query: { query: string } }
        if (body.query.query.endsWith('BROKEN')) {
          return HttpResponse.json({ detail: 'provider unavailable' }, { status: 503 })
        }
        return HttpResponse.json({
          items: ITEMS,
          total: 2,
          connection_id: 'conn-jira',
          provider: 'jira',
        })
      }),
    )
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=work-items')

    await user.type(await screen.findByLabelText('Find by description'), 'open stories')
    await user.click(screen.getByRole('button', { name: 'Translate' }))
    await user.click(await screen.findByRole('button', { name: 'Run query' }))
    expect(await screen.findByText('Slow checkout')).toBeInTheDocument()

    const query = screen.getByLabelText('Provider query')
    await user.clear(query)
    await user.type(query, 'project = PHX BROKEN')
    await user.click(screen.getByRole('button', { name: 'Run query' }))

    expect(await screen.findByText('Query failed: provider unavailable')).toBeInTheDocument()
    expect(screen.queryByText('Slow checkout')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Select PHX-101')).not.toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })

  it('does not publish a provider error after the wizard scope changes', async () => {
    installWizardHandlers()
    let markExecuteStarted!: () => void
    const executeStarted = new Promise<void>((resolve) => {
      markExecuteStarted = resolve
    })
    let releaseExecute!: () => void
    const executeRelease = new Promise<void>((resolve) => {
      releaseExecute = resolve
    })
    server.use(
      http.post('*/v1/work-tracking/query/translate', () => HttpResponse.json(TRANSLATED)),
      http.post('*/v1/work-tracking/query/execute', async () => {
        markExecuteStarted()
        await executeRelease
        return HttpResponse.json({ detail: 'provider unavailable' }, { status: 503 })
      }),
    )
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=work-items')

    await user.type(await screen.findByLabelText('Find by description'), 'open stories')
    await user.click(screen.getByRole('button', { name: 'Translate' }))
    await user.click(await screen.findByRole('button', { name: 'Run query' }))
    await executeStarted

    await user.click(screen.getByRole('tab', { name: 'Scope' }))
    const project = screen.getByLabelText('Project')
    await user.clear(project)
    await user.type(project, 'replacement-project')
    await act(async () => {
      releaseExecute()
      await new Promise((resolve) => setTimeout(resolve, 0))
    })

    expect(screen.queryByText('Query failed: provider unavailable')).not.toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })

  it('serializes translation and execution while a provider query is running', async () => {
    installWizardHandlers()
    let translationCount = 0
    let markExecuteStarted!: () => void
    const executeStarted = new Promise<void>((resolve) => {
      markExecuteStarted = resolve
    })
    let releaseExecute!: () => void
    const executeRelease = new Promise<void>((resolve) => {
      releaseExecute = resolve
    })
    server.use(
      http.post('*/v1/work-tracking/query/translate', () => {
        translationCount += 1
        return HttpResponse.json(TRANSLATED)
      }),
      http.post('*/v1/work-tracking/query/execute', async () => {
        markExecuteStarted()
        await executeRelease
        return HttpResponse.json({
          items: ITEMS,
          total: 2,
          connection_id: 'conn-jira',
          provider: 'jira',
        })
      }),
    )
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=work-items')

    await user.type(await screen.findByLabelText('Find by description'), 'open stories')
    await user.click(screen.getByRole('button', { name: 'Translate' }))
    await user.click(await screen.findByRole('button', { name: 'Run query' }))
    await executeStarted

    const translateButton = screen.getByRole('button', { name: 'Translate' })
    expect(translateButton).toBeDisabled()
    expect(screen.getByLabelText('Find by description')).toBeDisabled()
    await user.click(translateButton)
    expect(translationCount).toBe(1)

    await act(async () => releaseExecute())
    expect(await screen.findByText('Slow checkout')).toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })

  it('direct key add validates via getWorkItem and surfaces failures inline', async () => {
    installWizardHandlers()
    const projects: string[] = []
    server.use(
      http.get('*/v1/work-tracking/items/:key', ({ params, request }) => {
        projects.push(new URL(request.url).searchParams.get('project') ?? '')
        return params['key'] === 'PHX-241'
          ? HttpResponse.json({
              key: 'PHX-241',
              title: 'Found',
              kind: 'story',
              status: 'open',
              description: '',
              connection_id: 'conn-jira',
              provider: 'jira',
            })
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

  it('blocks launch until direct-key validation commits to the draft', async () => {
    installWizardHandlers()
    let markLookupStarted!: () => void
    const lookupStarted = new Promise<void>((resolve) => {
      markLookupStarted = resolve
    })
    let releaseLookup!: () => void
    const lookupRelease = new Promise<void>((resolve) => {
      releaseLookup = resolve
    })
    server.use(
      http.get('*/v1/work-tracking/items/:key', async () => {
        markLookupStarted()
        await lookupRelease
        return HttpResponse.json({
          key: 'PHX-241',
          title: 'Found',
          kind: 'story',
          status: 'open',
          description: '',
          connection_id: 'conn-jira',
          provider: 'jira',
        })
      }),
    )
    const user = userEvent.setup()
    const rendered = renderWizard()

    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Work Items' }))
    await user.type(await screen.findByLabelText('Add by key'), 'PHX-241')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await lookupStarted

    expect(screen.getByRole('button', { name: 'Finishing context…' })).toBeDisabled()
    await act(async () => releaseLookup())
    const selectedWorkItems = await screen.findByTestId('selected-work-items')
    expect(within(selectedWorkItems).getByText('PHX-241')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Launch Pipeline' })).toBeEnabled()
    await flushAndUnmountWizard(rendered)
  })
})
