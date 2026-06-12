import { File as NodeFile } from 'node:buffer'

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  DOC_SPEC,
  deleteDocumentHandler,
  documentsListHandler,
  evidenceHandler,
  summaryAcceptedHandler,
  uploadDocumentHandler,
} from './contextTestHandlers'

// Same realm-mismatch problem the wizard's context.test.tsx solves: undici
// brand-checks FormData/File against Node's classes while jsdom installs its
// own. Swap the globals so the real multipart upload path runs end-to-end.
const jsdomFormData = globalThis.FormData
const jsdomFile = globalThis.File

async function nodeFormDataClass(): Promise<typeof FormData> {
  const body = '--b\r\ncontent-disposition: form-data; name="x"\r\n\r\ny\r\n--b--\r\n'
  const response = new Response(body, {
    headers: { 'content-type': 'multipart/form-data; boundary=b' },
  })
  return (await response.formData()).constructor as typeof FormData
}

beforeAll(async () => {
  globalThis.FormData = await nodeFormDataClass()
  globalThis.File = NodeFile as unknown as typeof File
})

afterAll(() => {
  globalThis.FormData = jsdomFormData
  globalThis.File = jsdomFile
})

function renderContext(path = '/context', role: 'operator' | 'viewer' = 'operator') {
  return renderApp({ initialEntries: [path], authState: authenticatedState(role) })
}

describe('ContextPage', () => {
  it('summaries: 202 renders the accepted card with a run link and session history', async () => {
    const summaries = summaryAcceptedHandler('thread-99')
    server.use(summaries.handler)
    const user = userEvent.setup()
    renderContext()

    await user.type(
      await screen.findByRole('textbox', { name: 'Summary subject' }),
      'Checkout latency context',
    )
    // Work-item keys: Enter and the Add key button both append chips.
    const keyInput = screen.getByLabelText('Work item keys')
    await user.type(keyInput, 'PHX-101{Enter}')
    await user.type(keyInput, 'PHX-102')
    await user.click(screen.getByRole('button', { name: 'Add key' }))
    expect(within(screen.getByTestId('summary-keys')).getByText('PHX-101')).toBeInTheDocument()
    await user.type(screen.getByRole('textbox', { name: 'Project id' }), 'proj-alpha')

    await user.click(screen.getByRole('button', { name: 'Generate summary' }))

    const accepted = await screen.findByTestId('summary-accepted')
    expect(within(accepted).getByText('run-1')).toBeInTheDocument()
    // Thread id parsed from the stream_url -> /runs deep link.
    expect(within(accepted).getByRole('link', { name: 'Open run' })).toHaveAttribute(
      'href',
      '/runs/thread-99',
    )
    expect(summaries.captured).toEqual([
      {
        subject: 'Checkout latency context',
        work_item_keys: ['PHX-101', 'PHX-102'],
        project_id: 'proj-alpha',
      },
    ])

    // A second submission stacks session-local history (playground pattern).
    await user.click(screen.getByRole('button', { name: 'Generate summary' }))
    await within(await screen.findByTestId('summary-accepted')).findByText('run-2')
    expect(within(screen.getByTestId('summary-history')).getAllByRole('listitem')).toHaveLength(2)
  })

  it('documents: lists with filters, uploads multipart, deletes after confirm', async () => {
    const documents = documentsListHandler()
    const upload = uploadDocumentHandler()
    const del = deleteDocumentHandler()
    server.use(documents.handler, upload.handler, del.handler)
    const user = userEvent.setup()
    renderContext('/context?tab=documents')

    const row = await screen.findByTestId(`doc-row-${DOC_SPEC.id}`)
    expect(within(row).getByText('checkout-spec.pdf')).toHaveClass('strong')
    expect(within(row).getByText('application/pdf')).toHaveClass('dash-context-chip')
    expect(within(row).getByText('2.0 MB')).toBeInTheDocument()

    // Committed filters hit the server as ?project & ?q.
    await user.type(screen.getByLabelText('Filter by project'), 'proj-alpha')
    await user.type(screen.getByLabelText('Search documents'), 'spec')
    await user.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() =>
      expect(documents.captured.at(-1)).toEqual({ project: 'proj-alpha', q: 'spec' }),
    )

    // Upload reuses D4's useUploadDocument (multipart with project_id).
    await user.upload(
      screen.getByLabelText('Upload documents'),
      new File(['notes body'], 'notes.txt', { type: 'text/plain' }),
    )
    await waitFor(() =>
      expect(upload.captured).toEqual([{ fileName: 'notes.txt', projectId: 'proj-alpha' }]),
    )

    await user.click(screen.getByRole('button', { name: 'Delete checkout-spec.pdf' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete document checkout-spec.pdf' })
    expect(del.captured).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Delete document' }))
    await waitFor(() => expect(del.captured).toEqual([DOC_SPEC.id]))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('evidence: groups packets by source with run links and filter params', async () => {
    const evidence = evidenceHandler()
    server.use(evidence.handler)
    const user = userEvent.setup()
    renderContext('/context?tab=evidence')

    // Alphabetical source groups: elk before jira; jira gathers both packets.
    const elk = await screen.findByRole('region', { name: 'Source elk' })
    expect(within(elk).getByText('502 spike during ramp')).toBeInTheDocument()
    const jira = screen.getByRole('region', { name: 'Source jira' })
    expect(within(jira).getByText('2 packets')).toBeInTheDocument()
    expect(within(jira).getByText('PHX-101 acceptance criteria')).toBeInTheDocument()
    expect(within(jira).getByText('jira:PHX-101')).toHaveClass('ctx-packet-ref')

    // Thread deep link only where the packet carries a thread_id.
    const packet = within(jira).getByTestId('evidence-ev-jira-1')
    expect(within(packet).getByRole('link', { name: 'Open run' })).toHaveAttribute(
      'href',
      '/runs/thread-7',
    )
    expect(
      within(jira).getByTestId('evidence-ev-jira-2').querySelector('a'),
    ).toBeNull()

    await user.type(screen.getByLabelText('Filter by thread'), 'thread-7')
    await user.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() =>
      expect(evidence.captured.at(-1)).toEqual({ project: null, threadId: 'thread-7' }),
    )
  })

  it('hides mutations from viewers and explains the empty evidence state', async () => {
    const documents = documentsListHandler([DOC_SPEC])
    const evidence = evidenceHandler([])
    server.use(documents.handler, evidence.handler)
    const user = userEvent.setup()
    renderContext('/context', 'viewer')

    // Summaries: form renders but generation is disabled for viewers.
    expect(
      await screen.findByText('Viewer role — summary generation is disabled.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Generate summary' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Documents' }))
    await screen.findByTestId(`doc-row-${DOC_SPEC.id}`)
    expect(screen.queryByRole('button', { name: 'Upload' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Delete checkout-spec.pdf' }),
    ).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Evidence' }))
    expect(await screen.findByRole('heading', { name: 'No evidence yet' })).toBeInTheDocument()
    expect(screen.getByText(/accrue automatically as pipeline runs execute/)).toBeInTheDocument()
  })
})
