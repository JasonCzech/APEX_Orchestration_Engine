import { File as NodeFile } from 'node:buffer'

import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'

import { bumpSessionRevision } from '@/auth/keyStorage'
import {
  documentUploadBatchMutationKey,
  documentUploadMutationKey,
} from '@/api/hooks/useDocuments'
import { queryKeys } from '@/api/queryKeys'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  DOC_SPEC,
  deleteDocumentHandler,
  documentsListHandler,
  evidenceAssociationHandlers,
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

function renderContext(
  path = '/context?tab=summaries',
  role: 'operator' | 'viewer' = 'operator',
  scopes?: { project_id: string; app_id: string | null }[],
) {
  return renderApp({
    initialEntries: [path],
    authState: authenticatedState(role, 'Dash Ops', scopes),
  })
}

describe('ContextPage', () => {
  it('summaries: 202 renders the accepted card with a run link and session history', async () => {
    const summaries = summaryAcceptedHandler('thread-99')
    server.use(summaries.handler, documentsListHandler([]).handler)
    const user = userEvent.setup()
    renderContext()

    await user.type(
      await screen.findByRole('textbox', { name: 'Summary subject' }),
      'Checkout latency context',
    )
    // Work-item keys: Enter and the Add key button both append chips.
    const keyInput = screen.getByLabelText('Ticket keys')
    await user.type(keyInput, 'PHX-101{Enter}')
    await user.type(keyInput, 'PHX-102')
    await user.click(screen.getByRole('button', { name: 'Add key' }))
    expect(within(screen.getByTestId('summary-keys')).getByText('PHX-101')).toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Project id' }), 'proj-alpha')

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

    await user.click(screen.getByRole('tab', { name: 'Documents' }))
    await user.click(screen.getByRole('tab', { name: 'Summaries' }))
    expect(within(screen.getByTestId('summary-accepted')).getByText('run-2')).toBeInTheDocument()
    expect(within(screen.getByTestId('summary-history')).getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByRole('textbox', { name: 'Summary subject' })).toHaveValue(
      'Checkout latency context',
    )
  })

  it('summaries: preserves a deferred accepted response across tab switches', async () => {
    const captured: Array<{ subject: string }> = []
    let release!: () => void
    const blocked = new Promise<void>((resolve) => {
      release = resolve
    })
    let markStarted!: () => void
    const started = new Promise<void>((resolve) => {
      markStarted = resolve
    })
    server.use(
      documentsListHandler([]).handler,
      http.post('*/v1/context/summaries', async ({ request }) => {
        captured.push((await request.json()) as { subject: string })
        markStarted()
        await blocked
        return HttpResponse.json(
          {
            run_id: 'run-deferred',
            stream_url: '/threads/thread-deferred/runs/run-deferred/stream',
          },
          { status: 202 },
        )
      }),
    )
    const user = userEvent.setup()
    renderContext()

    await user.type(
      await screen.findByRole('textbox', { name: 'Summary subject' }),
      'Deferred context',
    )
    await user.click(screen.getByRole('button', { name: 'Generate summary' }))
    await started

    await user.click(screen.getByRole('tab', { name: 'Documents' }))
    await screen.findByRole('heading', { name: 'No documents' })
    await user.click(screen.getByRole('tab', { name: 'Summaries' }))
    expect(screen.getByRole('button', { name: 'Submitting…' })).toBeDisabled()
    expect(captured).toHaveLength(1)

    await user.click(screen.getByRole('tab', { name: 'Documents' }))
    release()
    await waitFor(() => expect(captured).toHaveLength(1))
    await user.click(screen.getByRole('tab', { name: 'Summaries' }))

    const accepted = await screen.findByTestId('summary-accepted')
    expect(within(accepted).getByText('run-deferred')).toBeInTheDocument()
    expect(within(accepted).getByRole('link', { name: 'Open run' })).toHaveAttribute(
      'href',
      '/runs/thread-deferred',
    )
    expect(within(screen.getByTestId('summary-history')).getAllByRole('listitem')).toHaveLength(1)
  })

  it('summaries: locks and publishes a deferred response across route remounts', async () => {
    const captured: Array<{ subject: string }> = []
    let release!: () => void
    const blocked = new Promise<void>((resolve) => {
      release = resolve
    })
    let markStarted!: () => void
    const started = new Promise<void>((resolve) => {
      markStarted = resolve
    })
    server.use(
      http.post('*/v1/context/summaries', async ({ request }) => {
        captured.push((await request.json()) as { subject: string })
        markStarted()
        await blocked
        return HttpResponse.json(
          {
            run_id: 'run-route-deferred',
            stream_url: '/threads/thread-route-deferred/runs/run-route-deferred/stream',
          },
          { status: 202 },
        )
      }),
    )
    const user = userEvent.setup()
    const { router } = renderContext()

    await user.type(
      await screen.findByRole('textbox', { name: 'Summary subject' }),
      'Route deferred context',
    )
    await user.click(screen.getByRole('button', { name: 'Generate summary' }))
    await started

    await act(async () => router.navigate('/settings'))
    await screen.findByRole('heading', { name: 'Settings' })
    await act(async () => router.navigate('/context?tab=summaries'))

    await user.type(
      await screen.findByRole('textbox', { name: 'Summary subject' }),
      'Must remain queued',
    )
    expect(screen.getByRole('button', { name: 'Submitting…' })).toBeDisabled()
    expect(captured).toHaveLength(1)

    await act(async () => router.navigate('/settings'))
    release()
    await waitFor(() => expect(captured).toHaveLength(1))
    await act(async () => router.navigate('/context?tab=summaries'))

    const accepted = await screen.findByTestId('summary-accepted')
    expect(within(accepted).getByText('run-route-deferred')).toBeInTheDocument()
    expect(within(accepted).getByRole('link', { name: 'Open run' })).toHaveAttribute(
      'href',
      '/runs/thread-route-deferred',
    )
    expect(within(screen.getByTestId('summary-history')).getAllByRole('listitem')).toHaveLength(1)
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

  it('does not carry a previous run stream result into a newly accepted run', async () => {
    let accepted = 0
    server.use(
      http.post('*/v1/context/summaries', () => {
        accepted += 1
        return HttpResponse.json(
          { run_id: `run-stream-${accepted}`, stream_url: `/summary-stream-${accepted}` },
          { status: 202 },
        )
      }),
      http.get('*/summary-stream-1', () =>
        HttpResponse.text('event: summary\ndata: {"summary":"Run one result"}\n\n', {
          headers: { 'content-type': 'text/event-stream' },
        }),
      ),
    )
    const user = userEvent.setup()
    renderContext()

    await user.type(
      await screen.findByRole('textbox', { name: 'Summary subject' }),
      'Stream identity check',
    )
    await user.click(screen.getByRole('button', { name: 'Generate summary' }))
    const first = await screen.findByTestId('summary-accepted')
    await user.click(within(first).getByRole('button', { name: 'Open live stream' }))
    expect(await within(first).findByText('Run one result')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Generate summary' }))
    const second = await screen.findByTestId('summary-accepted')
    expect(within(second).getByText('run-stream-2')).toBeInTheDocument()
    expect(within(second).queryByText('Run one result')).not.toBeInTheDocument()
    expect(within(second).getByRole('button', { name: 'Open live stream' })).toBeInTheDocument()
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
    expect(within(row).getByText('checkout-spec.pdf').closest('td')).toHaveClass('strong')
    expect(within(row).getByText('application/pdf')).toHaveClass('dash-context-chip')
    expect(within(row).getByText('2.0 MB')).toBeInTheDocument()

    // Committed filters hit the server as ?project & ?q.
    await user.selectOptions(screen.getByLabelText('Filter by project'), 'proj-alpha')
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
      expect(upload.captured).toEqual([
        { fileName: 'notes.txt', projectId: 'proj-alpha', appId: null },
      ]),
    )

    await user.click(screen.getByRole('button', { name: 'Document actions: checkout-spec.pdf' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete document checkout-spec.pdf' })
    expect(del.captured).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Delete document' }))
    await waitFor(() => expect(del.captured).toEqual([DOC_SPEC.id]))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('documents: opens a detail preview with full identity and source knowledge guidance', async () => {
    server.use(documentsListHandler().handler,
      http.get('*/v1/documents/doc-spec', () => HttpResponse.json({ ...DOC_SPEC, summary: 'Checkout requirements', text_preview: 'Bounded source text for the operator.' })))
    const user = userEvent.setup()
    renderContext('/context?tab=documents')
    await user.click(await screen.findByRole('button', { name: 'checkout-spec.pdf' }))
    const dialog = await screen.findByRole('dialog', { name: 'Document details: checkout-spec.pdf' })
    expect(await within(dialog).findByText('Bounded source text for the operator.')).toBeInTheDocument()
    expect(within(dialog).getByText('Checkout requirements')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Copy Document ID' })).toBeInTheDocument()
    expect(within(dialog).getByRole('link', { name: 'Open test knowledge' })).toHaveAttribute('href', '/runs/new?step=context')
    await user.click(within(dialog).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('documents: does not assert preview absence when the details request fails', async () => {
    server.use(documentsListHandler().handler,
      http.get('*/v1/documents/doc-spec', () => HttpResponse.json({ detail: 'Document store unavailable' }, { status: 503 })))
    const user = userEvent.setup()
    renderContext('/context?tab=documents')
    await user.click(await screen.findByRole('button', { name: 'checkout-spec.pdf' }))
    const dialog = await screen.findByRole('dialog', { name: 'Document details: checkout-spec.pdf' })
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Document store unavailable')
    expect(within(dialog).queryByText('No text preview is available for this document.')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Retry' })).toBeEnabled()
  })

  it('documents: lets app-scoped operators select an authorized upload audience', async () => {
    const documents = documentsListHandler([])
    const upload = uploadDocumentHandler()
    server.use(documents.handler, upload.handler)
    const user = userEvent.setup()
    renderContext('/context?tab=documents', 'operator', [
      { project_id: 'proj-alpha', app_id: 'app-checkout' },
      { project_id: 'proj-alpha', app_id: 'app-admin' },
    ])

    await user.selectOptions(await screen.findByLabelText('Filter by project'), 'proj-alpha')
    expect(screen.queryByRole('button', { name: 'Upload' })).not.toBeInTheDocument()

    await user.selectOptions(
      await screen.findByLabelText('Filter by application'),
      'app-checkout',
    )
    await user.upload(
      screen.getByLabelText('Upload documents'),
      new File(['scoped body'], 'checkout.txt', { type: 'text/plain' }),
    )

    await waitFor(() =>
      expect(upload.captured).toEqual([
        {
          fileName: 'checkout.txt',
          projectId: 'proj-alpha',
          appId: 'app-checkout',
        },
      ]),
    )
  })

  it('documents: stops a multi-file upload batch after a session transition', async () => {
    let uploads = 0
    server.use(
      documentsListHandler([]).handler,
      http.post('*/v1/documents', async ({ request }) => {
        const form = await request.formData()
        const file = form.get('file') as File
        uploads += 1
        bumpSessionRevision()
        return HttpResponse.json(
          {
            id: `doc-${uploads}`,
            name: file.name,
            media_type: 'text/plain',
            size_bytes: file.size,
            artifact_key: `documents/doc-${uploads}`,
            project_id: 'proj-alpha',
            app_id: null,
          },
          { status: 201 },
        )
      }),
    )
    const user = userEvent.setup()
    renderContext('/context?tab=documents')
    await user.selectOptions(await screen.findByLabelText('Filter by project'), 'proj-alpha')

    await user.upload(screen.getByLabelText('Upload documents'), [
      new File(['first'], 'first.txt', { type: 'text/plain' }),
      new File(['second'], 'second.txt', { type: 'text/plain' }),
    ])

    await waitFor(() => expect(uploads).toBe(1))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('documents: locks the upload audience for the full multi-file batch', async () => {
    const captured: Array<{ projectId: string | null; appId: string | null }> = []
    let releaseFirst!: () => void
    const firstBlocked = new Promise<void>((resolve) => {
      releaseFirst = resolve
    })
    let markStarted!: () => void
    const firstStarted = new Promise<void>((resolve) => {
      markStarted = resolve
    })
    server.use(
      documentsListHandler([]).handler,
      http.post('*/v1/documents', async ({ request }) => {
        const form = await request.formData()
        captured.push({
          projectId: form.get('project_id') as string | null,
          appId: form.get('app_id') as string | null,
        })
        if (captured.length === 1) {
          markStarted()
          await firstBlocked
        }
        return HttpResponse.json(
          {
            id: `doc-${captured.length}`,
            name: (form.get('file') as File).name,
            media_type: 'text/plain',
            size_bytes: 5,
            artifact_key: `documents/doc-${captured.length}`,
            project_id: 'proj-alpha',
            app_id: 'app-checkout',
          },
          { status: 201 },
        )
      }),
    )
    const user = userEvent.setup()
    renderContext('/context?tab=documents')
    const project = await screen.findByLabelText('Filter by project')
    const app = screen.getByLabelText('Filter by application')
    await user.selectOptions(project, 'proj-alpha')
    await user.type(app, 'app-checkout')

    await user.upload(screen.getByLabelText('Upload documents'), [
      new File(['first'], 'first.txt', { type: 'text/plain' }),
      new File(['second'], 'second.txt', { type: 'text/plain' }),
    ])
    await firstStarted

    expect(project).toBeDisabled()
    expect(app).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Uploading…' })).toBeDisabled()

    releaseFirst()
    await waitFor(() => expect(captured).toHaveLength(2))
    expect(captured).toEqual([
      { projectId: 'proj-alpha', appId: 'app-checkout' },
      { projectId: 'proj-alpha', appId: 'app-checkout' },
    ])
    await waitFor(() => expect(project).toBeEnabled())
  })

  it('documents: blocks an identical upload after the tab remounts while it is pending', async () => {
    let uploads = 0
    let markStarted!: () => void
    const started = new Promise<void>((resolve) => {
      markStarted = resolve
    })
    let release!: () => void
    const blocked = new Promise<void>((resolve) => {
      release = resolve
    })
    const documents = documentsListHandler([])
    server.use(
      documents.handler,
      http.post('*/v1/documents', async ({ request }) => {
        const form = await request.formData()
        const file = form.get('file') as File
        uploads += 1
        markStarted()
        await blocked
        return HttpResponse.json(
          {
            id: 'doc-deferred',
            name: file.name,
            media_type: file.type,
            size_bytes: file.size,
            artifact_key: 'documents/doc-deferred',
            project_id: 'proj-alpha',
            app_id: null,
          },
          { status: 201 },
        )
      }),
    )
    const user = userEvent.setup()
    const { queryClient } = renderContext('/context?tab=documents')
    await user.selectOptions(await screen.findByLabelText('Filter by project'), 'proj-alpha')
    const file = new File(['same content'], 'same.txt', {
      type: 'text/plain',
      lastModified: 123,
    })

    await user.upload(screen.getByLabelText('Upload documents'), file)
    await started
    expect(uploads).toBe(1)
    expect(
      queryClient.isMutating({
        exact: true,
        mutationKey: documentUploadMutationKey({ file, projectId: 'proj-alpha' }),
      }),
    ).toBe(1)

    await user.click(screen.getByRole('tab', { name: 'Summaries' }))
    await user.click(screen.getByRole('tab', { name: 'Documents' }))

    expect(await screen.findByRole('button', { name: 'Uploading…' })).toBeDisabled()
    await waitFor(() =>
      expect(documents.captured.at(-1)).toEqual({ project: 'proj-alpha', q: null }),
    )
    const remountedInput = screen.getByLabelText('Upload documents')
    expect(remountedInput).toBeDisabled()

    const duplicate = new File(['same content'], 'same.txt', {
      type: 'text/plain',
      lastModified: 123,
    })
    fireEvent.change(remountedInput, { target: { files: [duplicate] } })
    expect(uploads).toBe(1)
    expect(
      queryClient.isMutating({
        exact: true,
        mutationKey: documentUploadBatchMutationKey(),
      }),
    ).toBe(1)

    await act(async () => {
      release()
      await blocked
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Upload' })).toBeEnabled())
    expect(uploads).toBe(1)
  })

  it('documents: publishes a failed upload after the tab remounts', async () => {
    let markStarted!: () => void
    const started = new Promise<void>((resolve) => {
      markStarted = resolve
    })
    let release!: () => void
    const blocked = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      documentsListHandler([]).handler,
      http.post('*/v1/documents', async () => {
        markStarted()
        await blocked
        return HttpResponse.json({ detail: 'document provider unavailable' }, { status: 503 })
      }),
    )
    const user = userEvent.setup()
    const { queryClient } = renderContext('/context?tab=documents')
    await user.selectOptions(await screen.findByLabelText('Filter by project'), 'proj-alpha')

    await user.upload(
      screen.getByLabelText('Upload documents'),
      new File(['failed body'], 'failed.txt', { type: 'text/plain' }),
    )
    await started

    await user.click(screen.getByRole('tab', { name: 'Summaries' }))
    release()
    await waitFor(() =>
      expect(queryClient.getQueryData(queryKeys.documents.uploadOutcome())).toMatchObject({
        errors: ['Upload of failed.txt failed (503)'],
        projectId: 'proj-alpha',
      }),
    )
    await user.click(screen.getByRole('tab', { name: 'Documents' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Upload of failed.txt failed (503)',
    )
    expect(screen.getByLabelText('Filter by project')).toHaveValue('proj-alpha')
    expect(screen.getByRole('button', { name: 'Upload' })).toBeEnabled()
  })

  it('documents: keeps cached rows visible when a refresh fails', async () => {
    server.use(documentsListHandler().handler)
    const { queryClient } = renderContext('/context?tab=documents')

    expect(await screen.findByTestId(`doc-row-${DOC_SPEC.id}`)).toBeInTheDocument()
    server.use(
      http.get('*/v1/documents', () =>
        HttpResponse.json({ detail: 'documents temporarily unavailable' }, { status: 503 }),
      ),
    )
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.documents.all })
    })

    expect(await screen.findByText(/Showing cached data/)).toHaveTextContent(
      'documents temporarily unavailable',
    )
    expect(screen.getByTestId(`doc-row-${DOC_SPEC.id}`)).toBeInTheDocument()
  })

  it('evidence: exposes project, application and test provenance with persistent filters', async () => {
    const evidence = evidenceHandler()
    server.use(evidence.handler, ...evidenceAssociationHandlers())
    const user = userEvent.setup()
    renderContext('/evidence')

    expect(await screen.findByRole('region', { name: 'Evidence investigation workspace' })).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Project and test scope' })).toHaveTextContent('Proj Alpha')
    expect(screen.getAllByText('Checkout').length).toBeGreaterThan(0)
    expect(screen.getByTestId('evidence-ev-jira-1')).toHaveTextContent('Checkout release test')
    expect(screen.getByRole('complementary', { name: 'Artifact details' })).toHaveTextContent('Checkout release test')
    expect(screen.getByRole('link', { name: 'Open Checkout release test' })).toHaveAttribute('href', '/runs/thread-7')

    const search = screen.getByLabelText('Search evidence')
    await user.type(search, 'unapplied draft')
    await user.click(screen.getByTestId('evidence-ev-jira-2'))
    expect(search).toHaveValue('unapplied draft')
    expect(screen.getByTestId('evidence-ev-jira-2')).toHaveAttribute('aria-selected', 'true')

    await user.clear(search)
    await user.selectOptions(screen.getByLabelText('Filter by project'), 'proj-alpha')
    await user.click(screen.getByRole('button', { name: /Apply filters/ }))
    await waitFor(() =>
      expect(evidence.captured.at(-1)).toEqual({ project: 'proj-alpha', threadId: null }),
    )
    expect(screen.getByRole('option', { name: 'Proj Beta' })).toBeInTheDocument()

    await user.click(screen.getByRole('combobox', { name: 'Filter by run' }))
    await user.type(screen.getByRole('combobox', { name: 'Filter by run' }), 'Checkout release test')
    await user.click(within(screen.getByRole('listbox', { name: 'Filter by run choices' })).getByRole('option', { name: /Checkout release test/ }))
    await user.click(screen.getByRole('button', { name: /Apply filters/ }))
    await waitFor(() =>
      expect(evidence.captured.at(-1)).toEqual({ project: 'proj-alpha', threadId: 'thread-7' }),
    )
    expect(screen.getByLabelText('Filter by run')).toHaveValue('Checkout release test')

    await user.click(screen.getByRole('tab', { name: 'Content' }))
    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'Details' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Evidence ID')
  })

  it('evidence: explains unverifiable application membership beyond the recent run lookup', async () => {
    server.use(evidenceHandler([{ id: 'old-evidence', source: 'document', title: 'Older observed finding', summary: 'Recorded evidence', thread_id: 'old-run', ref: null }]).handler, ...evidenceAssociationHandlers())
    const user = userEvent.setup()
    renderContext('/evidence?app=app-checkout')
    expect(await screen.findByRole('heading', { name: '1 evidence item has unverified application membership' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'No verified evidence matches these filters' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'View evidence without application filter' }))
    expect(await screen.findByTestId('evidence-old-evidence')).toHaveTextContent('Older observed finding')
    expect(screen.getByRole('link', { name: 'Open run' })).toHaveAttribute('href', '/runs/old-run')
  })

  it('evidence: retains unknown deep-linked filter values', async () => {
    const evidence = evidenceHandler()
    server.use(evidence.handler, ...evidenceAssociationHandlers())
    renderContext('/evidence?project=unknown-project&source=custom-source')

    expect(await screen.findByRole('heading', { name: 'No evidence matches these filters' })).toBeInTheDocument()
    expect(screen.getByLabelText('Filter by project')).toHaveValue('unknown-project')
    expect(screen.getByLabelText('Filter by source')).toHaveValue('custom-source')
    expect(evidence.captured.at(-1)).toEqual({ project: 'unknown-project', threadId: null })
  })

  it('evidence: remains usable when association catalogs are unavailable', async () => {
    const evidence = evidenceHandler()
    server.use(
      evidence.handler,
      http.get('*/v1/pipelines', () =>
        HttpResponse.json({ detail: 'pipeline index unavailable' }, { status: 503 }),
      ),
      http.get('*/v1/catalog/applications', () =>
        HttpResponse.json({ detail: 'application index unavailable' }, { status: 503 }),
      ),
    )
    renderContext('/evidence')

    expect(await screen.findByTestId('evidence-ev-jira-1')).toHaveTextContent('thread-7')
    expect(screen.getByRole('status')).toHaveTextContent('association names are unavailable')
    expect(screen.getByRole('link', { name: 'Open run' })).toHaveAttribute('href', '/runs/thread-7')
    expect(screen.getAllByText('Run details unavailable').length).toBeGreaterThan(0)
    await userEvent.setup().click(screen.getByRole('tab', { name: 'Provenance' }))
    expect(within(screen.getByRole('tabpanel')).getAllByText('Run details unavailable')).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled()
  })

  it('hides mutations from viewers in the knowledge library', async () => {
    const documents = documentsListHandler([DOC_SPEC])
    const evidence = evidenceHandler([])
    server.use(documents.handler, evidence.handler)
    const user = userEvent.setup()
    renderContext('/context?tab=summaries', 'viewer')

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

  })
})
