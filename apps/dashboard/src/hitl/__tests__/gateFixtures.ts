/**
 * D3 HITL fixtures + msw handlers. Payloads are contract-valid against
 * @apex/pipeline-events interrupts.ts (schema_version 1); handlers are
 * registered per-test via server.use(...).
 */
import { http, HttpResponse } from 'msw'

import { parseGateInterrupt } from '@apex/pipeline-events'

import type { GateInterrupt, PipelineDetail } from '@/api/hooks/useThreadState'
import type { GateInstance } from '@/hitl/gateMachine'

export const PROMPT_PAYLOAD = {
  schema_version: 1,
  kind: 'prompt_review',
  phase: 'test_planning',
  prompt: {
    system: 'You are the planning agent.',
    user: 'Plan load coverage for APEX-101.',
    application: 'Checkout must preserve carts during payment retries.',
    source: { origin: 'catalog', ref: 'test_planning@v2' },
  },
  context_packets: [
    { id: 'cp-1', source: 'jira', title: 'APEX-101', summary: 'Checkout latency story' },
  ],
  tools: ['jira.search', 'k8s.inventory'],
  editable: true,
  actions: ['approve', 'modify', 'skip_phase', 'abort'],
} as const

export const PHASE_PAYLOAD = {
  schema_version: 1,
  kind: 'phase_review',
  phase: 'test_planning',
  summary: 'Planned 4 scenarios against staging.\n\nRamp tuned to 5 minutes.',
  result_preview: { summary: 'Planned 4 scenarios', reasoning_digest: 'balanced read/write mix' },
  artifacts: [{ id: 'art-plan', kind: 'plan', name: 'plan.md' }],
  warnings: ['Latency budget is tight on checkout.'],
  dialogue_tail: [
    {
      id: 'dlg-1',
      phase: 'test_planning',
      role: 'operator',
      content: 'Tighten the ramp.',
      at: '2026-06-12T10:00:00+00:00',
    },
    {
      id: 'dlg-2',
      phase: 'test_planning',
      role: 'agent',
      content: 'Ramp tightened to 5m.',
      at: '2026-06-12T10:00:20+00:00',
    },
  ],
  actions: ['approve', 'revise', 'discuss', 'abort'],
} as const

export function promptInterrupt(id = 'int-p1'): GateInterrupt {
  return {
    interrupt_id: id,
    kind: 'prompt_review',
    phase: 'test_planning',
    payload: structuredClone(PROMPT_PAYLOAD) as unknown as Record<string, unknown>,
  }
}

export function phaseInterrupt(id = 'int-r1'): GateInterrupt {
  return {
    interrupt_id: id,
    kind: 'phase_review',
    phase: 'test_planning',
    payload: structuredClone(PHASE_PAYLOAD) as unknown as Record<string, unknown>,
  }
}

/** Typed GateInstance straight from a fixture interrupt (payload pre-parsed). */
export function gateInstanceOf(interrupt: GateInterrupt): GateInstance {
  return {
    interrupt_id: interrupt.interrupt_id ?? 'int-?',
    kind: interrupt.kind as GateInstance['kind'],
    phase: interrupt.phase ?? 'unknown',
    payload: parseGateInterrupt(interrupt.payload),
  }
}

/** Minimal interrupted-thread facade response carrying the given interrupts. */
export function gatedDetail(threadId: string, interrupts: GateInterrupt[]): PipelineDetail {
  const first = interrupts[0]
  return {
    thread_id: threadId,
    title: 'Gated run',
    project_id: 'proj-alpha',
    app_id: null,
    thread_status: interrupts.length > 0 ? 'interrupted' : 'idle',
    current_phase: first?.phase ?? null,
    phase_strip: [],
    created_at: '2026-06-12T09:00:00+00:00',
    updated_at: '2026-06-12T10:00:00+00:00',
    pending_gate: first
      ? { interrupt_id: first.interrupt_id, kind: first.kind, phase: first.phase }
      : null,
    values: {},
    interrupts,
  }
}

export interface CapturedResume {
  calls: Array<{ threadId: string; interruptId: string; body: Record<string, unknown> }>
  last: () => { threadId: string; interruptId: string; body: Record<string, unknown> } | undefined
}

/**
 * Handler for POST /v1/pipelines/:threadId/gates/:interruptId/resume.
 * status 202 -> {run_id}, 409 -> RFC-7807 gate_superseded problem, anything
 * else -> FastAPI-style {detail} error. Returns the capture for body asserts.
 */
export function resumeHandler(
  status: 202 | 409 | 500 = 202,
  options: { runId?: string } = {},
): { handler: ReturnType<typeof http.post>; captured: CapturedResume } {
  const captured: CapturedResume = {
    calls: [],
    last: () => captured.calls[captured.calls.length - 1],
  }
  const handler = http.post(
    '*/v1/pipelines/:threadId/gates/:interruptId/resume',
    async ({ request, params }) => {
      captured.calls.push({
        threadId: String(params['threadId']),
        interruptId: String(params['interruptId']),
        body: (await request.json()) as Record<string, unknown>,
      })
      if (status === 202) {
        return HttpResponse.json({ run_id: options.runId ?? 'run-99' }, { status: 202 })
      }
      if (status === 409) {
        return HttpResponse.json(
          {
            type: 'about:blank',
            title: 'gate_superseded',
            status: 409,
            detail: 'interrupt is no longer pending; re-fetch the pipeline',
            pending_gate: null,
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }
      return HttpResponse.json({ detail: 'resume exploded' }, { status: 500 })
    },
  )
  return { handler, captured }
}

/** GET pipelines/{threadId} handler whose payload can be swapped mid-test. */
export function mutableDetailHandler(threadId: string, initial: PipelineDetail) {
  const ref = { current: initial, requests: 0 }
  const handler = http.get(`*/v1/pipelines/${threadId}`, () => {
    ref.requests += 1
    return HttpResponse.json(ref.current)
  })
  return { handler, ref }
}
