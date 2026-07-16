/**
 * msw fixtures + handlers for the /context screen (mirrors
 * environmentsTestHandlers.ts). Handlers capture payloads/params so tests can
 * assert exact wire shapes.
 */
import { http, HttpResponse } from 'msw'

import type { ContextSummaryRequest, EvidencePacket } from '@/api/hooks/useContextApi'
import type { DocumentOut } from '@/api/hooks/useDocuments'

const NOW = '2026-06-12T12:00:00Z'

export const DOC_SPEC: DocumentOut = {
  id: 'doc-spec',
  name: 'checkout-spec.pdf',
  media_type: 'application/pdf',
  size_bytes: 2_097_152,
  artifact_key: 'documents/doc-spec',
  project_id: 'proj-alpha',
  app_id: null,
  summary: null,
  uploaded_by: 'ops',
  created_at: NOW,
}

export const DOC_RUNBOOK: DocumentOut = {
  id: 'doc-runbook',
  name: 'perf-runbook.md',
  media_type: 'text/markdown',
  size_bytes: 11_264,
  artifact_key: 'documents/doc-runbook',
  project_id: 'proj-alpha',
  app_id: null,
  summary: 'Perf test runbook',
  uploaded_by: null,
  created_at: NOW,
}

export const EV_JIRA_CONTEXT: EvidencePacket = {
  id: 'ev-jira-1',
  source: 'jira',
  title: 'PHX-101 acceptance criteria',
  summary: 'Checkout must absorb gateway retries without dropping carts.',
  ref: 'jira:PHX-101',
  thread_id: 'thread-7',
}

export const EV_JIRA_COMMENTS: EvidencePacket = {
  id: 'ev-jira-2',
  source: 'jira',
  title: 'PHX-102 triage notes',
  summary: null,
  ref: 'jira:PHX-102#comments',
  thread_id: null,
}

export const EV_ELK_SPIKE: EvidencePacket = {
  id: 'ev-elk-1',
  source: 'elk',
  title: '502 spike during ramp',
  summary: 'Error rate crossed 2% at 400 vusers.',
  ref: null,
  thread_id: 'thread-8',
}

/**
 * POST /v1/context/summaries — captures bodies, answers 202 with incrementing
 * run ids so session-history assertions get unique keys. The stream URL
 * carries the thread id the accepted card deep-links to.
 */
export function summaryAcceptedHandler(threadId = 'thread-99') {
  const captured: ContextSummaryRequest[] = []
  const handler = http.post('*/v1/context/summaries', async ({ request }) => {
    captured.push((await request.json()) as ContextSummaryRequest)
    const runId = `run-${captured.length}`
    return HttpResponse.json(
      { run_id: runId, stream_url: `/threads/${threadId}/runs/${runId}/stream` },
      { status: 202 },
    )
  })
  return { handler, captured }
}

/** GET /v1/documents — captures ?project/?q params per call. */
export function documentsListHandler(items: DocumentOut[] = [DOC_SPEC, DOC_RUNBOOK]) {
  const captured: { project: string | null; q: string | null }[] = []
  const handler = http.get('*/v1/documents', ({ request }) => {
    const url = new URL(request.url)
    captured.push({ project: url.searchParams.get('project'), q: url.searchParams.get('q') })
    return HttpResponse.json({ items, limit: 50, offset: 0 })
  })
  return { handler, captured }
}

/** POST /v1/documents — captures multipart fields, answers 201. */
export function uploadDocumentHandler(id = 'doc-new') {
  const captured: { fileName: string; projectId: string | null; appId: string | null }[] = []
  const handler = http.post('*/v1/documents', async ({ request }) => {
    const form = await request.formData()
    const file = form.get('file') as File
    captured.push({
      fileName: file.name,
      projectId: form.get('project_id') as string | null,
      appId: form.get('app_id') as string | null,
    })
    const created: DocumentOut = {
      id,
      name: file.name,
      media_type: 'text/plain',
      size_bytes: 512,
      artifact_key: `documents/${id}`,
      project_id: null,
      app_id: null,
      summary: null,
      uploaded_by: 'ops',
      created_at: NOW,
    }
    return HttpResponse.json(created, { status: 201 })
  })
  return { handler, captured }
}

/** DELETE /v1/documents/{id} — captures ids, answers 204. */
export function deleteDocumentHandler() {
  const captured: string[] = []
  const handler = http.delete('*/v1/documents/:id', ({ params }) => {
    captured.push(String(params.id))
    return new HttpResponse(null, { status: 204 })
  })
  return { handler, captured }
}

/** GET /v1/context/evidence — captures ?project/?thread_id params per call. */
export function evidenceHandler(
  packets: EvidencePacket[] = [EV_JIRA_CONTEXT, EV_JIRA_COMMENTS, EV_ELK_SPIKE],
) {
  const captured: { project: string | null; threadId: string | null }[] = []
  const handler = http.get('*/v1/context/evidence', ({ request }) => {
    const url = new URL(request.url)
    captured.push({
      project: url.searchParams.get('project'),
      threadId: url.searchParams.get('thread_id'),
    })
    return HttpResponse.json(packets)
  })
  return { handler, captured }
}
