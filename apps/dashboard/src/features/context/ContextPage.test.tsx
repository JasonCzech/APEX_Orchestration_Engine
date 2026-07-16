import { File as NodeFile } from 'node:buffer'

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'

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

  it('summary stream does not reflect a terminal backend error payload', async () => {
    const canary = 'summary-provider-secret-canary'
    const redirectModes: RequestRedirect[] = []
    server.use(
      http.post('*/v1/context/summaries', () =>
        HttpResponse.json(
          { run_id: 'run-stream-error', stream_url: '/summary-stream-error' },
          { status: 202 },
        ),
      ),
      http.get('*/summary-stream-error', ({ request }) => {
        redirectModes.push(request.redirect)
        return HttpResponse.text(`event: error\ndata: ${canary}\n\n`, {
          headers: { 'content-type': 'text/event-stream' },
        })
      }),
    )
    const user = userEvent.setup()
    renderContext()

    await user.type(
      await screen.findByRole('textbox', { name: 'Summary subject' }),
      'Summary with a failed stream',
    )
    await user.click(screen.getByRole('button', { name: 'Generate summary' }))
    const accepted = await screen.findByTestId('summary-accepted')
    await user.click(within(accepted).getByRole('button', { name: 'Open live stream' }))

    expect(await within(accepted).findByText('Unable to read the summary stream.')).toBeInTheDocument()
    expect(within(accepted).queryByText(canary)).not.toBeInTheDocument()
    expect(redirectModes).toEqual(['error'])
  })

  it('summary stream fails closed when the response exceeds its byte budget', async () => {
    server.use(
      http.post('*/v1/context/summaries', () =>
        HttpResponse.json(
          { run_id: 'run-stream-large', stream_url: '/summary-stream-large' },
          { status: 202 },
        ),
      ),
      http.get('*/summary-stream-large', () =>
        HttpResponse.text('x'.repeat(4 * 1024 * 1024 + 1), {
          headers: { 'content-type': 'text/event-stream' },
        }),
      ),
    )
    const user = userEvent.setup()
    renderContext()

    await user.type(
      await screen.findByRole('textbox', { name: 'Summary subject' }),
      'Summary with an oversized stream',
    )
    await user.click(screen.getByRole('button', { name: 'Generate summary' }))
    const accepted = await screen.findByTestId('summary-accepted')
    await user.click(within(accepted).getByRole('button', { name: 'Open live stream' }))

    expect(await within(accepted).findByText('Unable to read the summary stream.')).toBeInTheDocument()
  })

  it('summary stream rejects a media type that only prefixes the SSE type', async () => {
    const canary = 'malformed-sse-media-type-canary'
    server.use(
      http.post('*/v1/context/summaries', () =>
        HttpResponse.json(
          { run_id: 'run-stream-wrong-media', stream_url: '/summary-stream-wrong-media' },
          { status: 202 },
        ),
      ),
      http.get('*/summary-stream-wrong-media', () =>
        HttpResponse.text(`data: ${JSON.stringify({ summary: canary })}\n\n`, {
          headers: { 'content-type': 'text/event-streaming' },
        }),
      ),
    )
    const user = userEvent.setup()
    renderContext()

    await user.type(
      await screen.findByRole('textbox', { name: 'Summary subject' }),
      'Summary with the wrong stream media type',
    )
    await user.click(screen.getByRole('button', { name: 'Generate summary' }))
    const accepted = await screen.findByTestId('summary-accepted')
    await user.click(within(accepted).getByRole('button', { name: 'Open live stream' }))

    expect(await within(accepted).findByText('Unable to read the summary stream.')).toBeInTheDocument()
    expect(within(accepted).queryByText(canary)).not.toBeInTheDocument()
  })

  it('summary stream fails closed on malformed UTF-8', async () => {
    server.use(
      http.post('*/v1/context/summaries', () =>
        HttpResponse.json(
          { run_id: 'run-stream-invalid-utf8', stream_url: '/summary-stream-invalid-utf8' },
          { status: 202 },
        ),
      ),
      http.get(
        '*/summary-stream-invalid-utf8',
        () =>
          new HttpResponse(new Uint8Array([0x64, 0x61, 0x74, 0x61, 0x3a, 0x20, 0xff]), {
            headers: { 'content-type': 'Text/Event-Stream; Charset=UTF-8' },
          }),
      ),
    )
    const user = userEvent.setup()
    renderContext()

    await user.type(
      await screen.findByRole('textbox', { name: 'Summary subject' }),
      'Summary with malformed stream bytes',
    )
    await user.click(screen.getByRole('button', { name: 'Generate summary' }))
    const accepted = await screen.findByTestId('summary-accepted')
    await user.click(within(accepted).getByRole('button', { name: 'Open live stream' }))

    expect(await within(accepted).findByText('Unable to read the summary stream.')).toBeInTheDocument()
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
