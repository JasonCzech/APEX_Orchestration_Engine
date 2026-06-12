/**
 * MSW handlers + fixtures for the approvals inbox tests, registered per-test
 * via server.use(...) (the shared src/test/server.ts default — empty pipelines
 * — stays untouched).
 */
import { http, HttpResponse } from 'msw'

import type { PendingGate, PipelineSummary } from '@/api/hooks/usePipelines'
import type { PipelineDetail } from '@/api/hooks/useThreadState'
import { makeStrip } from '@/features/runs/runsTestHandlers'

export interface GatedRunOptions {
  kind: NonNullable<PendingGate['kind']>
  phase: string
  interruptId: string
  updatedAt: string
  title?: string
  projectId?: string
}

/** A pipelines-list row with a pending gate. */
export function gatedRun(threadId: string, options: GatedRunOptions): PipelineSummary {
  return {
    thread_id: threadId,
    title: options.title ?? `Run ${threadId}`,
    project_id: options.projectId ?? 'proj-alpha',
    app_id: null,
    thread_status: 'interrupted',
    current_phase: options.phase,
    phase_strip: makeStrip(),
    engine: null,
    created_at: '2026-06-11T00:00:00Z',
    updated_at: options.updatedAt,
    pending_gate: {
      interrupt_id: options.interruptId,
      kind: options.kind,
      phase: options.phase,
    },
  }
}

/** Thread-detail facade response matching a gated list row. */
export function gatedDetail(row: PipelineSummary): PipelineDetail {
  const gate = row.pending_gate
  return {
    thread_id: row.thread_id,
    title: row.title,
    project_id: row.project_id,
    app_id: row.app_id,
    thread_status: 'interrupted',
    current_phase: row.current_phase,
    phase_strip: row.phase_strip,
    created_at: row.created_at,
    updated_at: row.updated_at,
    pending_gate: gate,
    values: {},
    interrupts: gate
      ? [
          {
            interrupt_id: gate.interrupt_id,
            kind: gate.kind,
            phase: gate.phase,
            payload:
              gate.kind === 'prompt_review'
                ? {
                    schema_version: 1,
                    kind: 'prompt_review',
                    phase: gate.phase,
                    prompt: {
                      system: 'You are the test planning agent.',
                      user: 'Plan the load test.',
                      source: { origin: 'catalog', ref: 'test_planning@v1' },
                    },
                    context_packets: [],
                    tools: [],
                    editable: true,
                    actions: ['approve', 'modify', 'skip_phase', 'abort'],
                  }
                : {
                    schema_version: 1,
                    kind: 'phase_review',
                    phase: gate.phase,
                    summary: 'Phase output ready for review.',
                    result_preview: { summary: 'Phase output ready for review.' },
                    artifacts: [],
                    warnings: [],
                    dialogue_tail: [],
                    actions: ['approve', 'revise', 'discuss', 'abort'],
                  },
          },
        ]
      : [],
  }
}

/**
 * GET /v1/pipelines answering from a mutable source — reassign `current` (or
 * pass a fresh array) and trigger a refetch to simulate the next poll.
 */
export function mutableListHandler(initial: PipelineSummary[]) {
  const source = { current: initial }
  const handler = http.get('*/v1/pipelines', () =>
    HttpResponse.json({ items: source.current, limit: 100, offset: 0 }),
  )
  return { handler, source }
}

/** GET /v1/pipelines/{thread_id} from a fixed map of details. */
export function detailsHandler(details: Record<string, PipelineDetail>) {
  return http.get('*/v1/pipelines/:threadId', ({ params }) => {
    const detail = details[String(params.threadId)]
    return detail
      ? HttpResponse.json(detail)
      : HttpResponse.json({ detail: 'unknown thread' }, { status: 404 })
  })
}
