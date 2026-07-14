/**
 * Context step: multipart document upload -> removable chip with size; the
 * existing-documents picker adds by id.
 */
import { File as NodeFile } from 'node:buffer'

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { server } from '@/test/server'

import { flushAndUnmountWizard, installWizardHandlers, renderWizard } from './wizardTestUtils'

// Same realm-mismatch problem setup.ts solves for AbortSignal: the fetch stack
// is Node's undici, which brand-checks FormData/File against Node's classes,
// while jsdom installs its own. Swap the globals to Node's for this file so
// the REAL multipart path (component FormData -> fetch -> msw formData())
// runs end-to-end. Node's FormData class isn't importable; recover it by
// parsing a minimal multipart Response through undici itself.
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

const EXISTING_DOC = {
  id: 'doc-old',
  name: 'runbook.pdf',
  media_type: 'application/pdf',
  size_bytes: 2_097_152,
  artifact_key: 'documents/doc-old',
  project_id: 'demo',
}

describe('ContextStep', () => {
  it('uploads multipart with project_id and renders a removable chip with size', async () => {
    installWizardHandlers()
    const uploads: { fileName: string; projectId: string | null }[] = []
    server.use(
      http.get('*/v1/documents', () =>
        HttpResponse.json({ items: [EXISTING_DOC], limit: 50, offset: 0 }),
      ),
      http.post('*/v1/documents', async ({ request }) => {
        const form = await request.formData()
        const file = form.get('file') as File
        uploads.push({ fileName: file.name, projectId: form.get('project_id') as string | null })
        return HttpResponse.json(
          {
            id: 'doc-new',
            name: file.name,
            media_type: 'text/plain',
            size_bytes: 11_264,
            artifact_key: 'documents/doc-new',
            project_id: 'demo',
          },
          { status: 201 },
        )
      }),
    )
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=context')

    const input = await screen.findByLabelText('Upload documents')
    await user.upload(input, new File(['spec body'], 'spec.txt', { type: 'text/plain' }))

    const chips = await screen.findByTestId('attached-documents')
    expect(within(chips).getByText('spec.txt · 11.0 KB')).toBeInTheDocument()
    expect(uploads).toEqual([{ fileName: 'spec.txt', projectId: 'demo' }])

    // Existing-documents picker adds (and the chip carries its size too).
    const existingDocuments = screen.getByText('Existing documents').closest('.wizard-field')
    expect(existingDocuments).not.toBeNull()
    await user.click(within(existingDocuments as HTMLElement).getByRole('button', { name: 'Add' }))
    expect(within(chips).getByText('runbook.pdf · 2.0 MB')).toBeInTheDocument()

    // Chips are removable.
    await user.click(screen.getByRole('button', { name: 'Remove spec.txt' }))
    await waitFor(() =>
      expect(within(chips).queryByText('spec.txt · 11.0 KB')).not.toBeInTheDocument(),
    )
    await flushAndUnmountWizard(rendered)
  })

  it('rejects an unsupported dropped file with a friendly error and does not upload', async () => {
    installWizardHandlers()
    let posted = false
    server.use(
      http.get('*/v1/documents', () =>
        HttpResponse.json({ items: [], limit: 50, offset: 0 }),
      ),
      http.post('*/v1/documents', () => {
        posted = true
        return HttpResponse.json({}, { status: 201 })
      }),
    )
    const rendered = renderWizard('/runs/new?step=context')

    const dropzone = await screen.findByTestId('document-dropzone')
    fireEvent.drop(dropzone, {
      dataTransfer: { files: [new File(['<binary>'], 'diagram.png', { type: 'image/png' })] },
    })

    const errors = await screen.findByTestId('upload-errors')
    expect(errors).toHaveTextContent(/diagram\.png: unsupported type/i)
    expect(posted).toBe(false)
    await flushAndUnmountWizard(rendered)
  })

  it('shows parse status, char count and an expandable preview for a parsed upload', async () => {
    installWizardHandlers()
    server.use(
      http.get('*/v1/documents', () =>
        HttpResponse.json({ items: [], limit: 50, offset: 0 }),
      ),
      http.post('*/v1/documents', () =>
        HttpResponse.json(
          {
            id: 'doc-parsed',
            name: 'story.md',
            media_type: 'text/markdown',
            size_bytes: 2048,
            artifact_key: 'documents/doc-parsed',
            project_id: 'demo',
            parse_status: 'parsed',
            extracted_chars: 1234,
            text_preview: 'Story preview text body',
          },
          { status: 201 },
        ),
      ),
    )
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=context')

    const input = await screen.findByLabelText('Upload documents')
    await user.upload(input, new File(['# Story'], 'story.md', { type: 'text/markdown' }))

    const attached = await screen.findByTestId('attached-documents')
    expect(within(attached).getByText('Parsed')).toBeInTheDocument()
    expect(within(attached).getByText(/1,234 characters extracted/)).toBeInTheDocument()

    // Preview is revealed on demand.
    await user.click(within(attached).getByText('Preview extracted text'))
    expect(within(attached).getByText('Story preview text body')).toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })

  it('surfaces a parse error for a failed upload', async () => {
    installWizardHandlers()
    server.use(
      http.get('*/v1/documents', () =>
        HttpResponse.json({ items: [], limit: 50, offset: 0 }),
      ),
      http.post('*/v1/documents', () =>
        HttpResponse.json(
          {
            id: 'doc-bad',
            name: 'broken.pdf',
            media_type: 'application/pdf',
            size_bytes: 10,
            artifact_key: 'documents/doc-bad',
            project_id: 'demo',
            parse_status: 'failed',
            parse_error: 'PDF is password-protected',
          },
          { status: 201 },
        ),
      ),
    )
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=context')

    const input = await screen.findByLabelText('Upload documents')
    await user.upload(input, new File(['%PDF'], 'broken.pdf', { type: 'application/pdf' }))

    const attached = await screen.findByTestId('attached-documents')
    expect(within(attached).getByText('Parse failed')).toBeInTheDocument()
    expect(within(attached).getByText(/password-protected/)).toBeInTheDocument()
    await flushAndUnmountWizard(rendered)
  })
})
