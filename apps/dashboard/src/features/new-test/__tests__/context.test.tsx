/**
 * Context step: multipart document upload -> removable chip with size; the
 * existing-documents picker adds by id.
 */
import { File as NodeFile } from 'node:buffer'

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { server } from '@/test/server'

import { installWizardHandlers, renderWizard } from './wizardTestUtils'

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
    renderWizard('/runs/new?step=context')

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
  })
})
