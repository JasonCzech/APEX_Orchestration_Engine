/**
 * useGate binding tests: snapshot discovery/clear/new-instance, the stream
 * hint accelerator, and the resume wiring (202 / 409-conflict / 500) against
 * msw — the machine itself is covered by gateMachine.test.ts.
 */
import type { ReactNode } from 'react'

import { renderHook, waitFor, act } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { server } from '@/test/server'
import { createTestQueryClient } from '@/test/render'
import { useGate, type GateHintLike } from '@/hitl/useGate'
import { queryKeys } from '@/api/queryKeys'

import {
  gatedDetail,
  engineRetryInterrupt,
  mutableDetailHandler,
  phaseInterrupt,
  promptInterrupt,
  resumeHandler,
} from './gateFixtures'

function harness() {
  const queryClient = createTestQueryClient()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { queryClient, wrapper }
}

describe('useGate discovery', () => {
  it('discovers an engine recovery interrupt instead of dropping the provider lease gate', async () => {
    const threadId = 'th-recovery'
    const { handler } = mutableDetailHandler(
      threadId,
      gatedDetail(threadId, [engineRetryInterrupt('int-recovery')]),
    )
    server.use(handler)
    const { wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    expect(result.current.gate).toMatchObject({
      interrupt_id: 'int-recovery',
      kind: 'engine_cleanup_retry',
      phase: 'execution',
    })
    expect(result.current.gate?.payload?.actions).toEqual(['retry'])
  })

  it('discovers the pending interrupt, clears to superseded, opens a NEW instance on a fresh id', async () => {
    const threadId = 'th-disc'
    const { handler, ref } = mutableDetailHandler(threadId, gatedDetail(threadId, [promptInterrupt('int-1')]))
    server.use(handler)
    const { queryClient, wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    expect(result.current.gate?.interrupt_id).toBe('int-1')
    expect(result.current.gate?.payload?.kind).toBe('prompt_review')

    // Draft seeded; an edit survives unrelated refetches (same id no-op).
    act(() => result.current.edit({ prompt: { system: 'EDITED' } }))
    await waitFor(() => {
      const state = result.current.state
      expect(state.tag === 'open' && state.dirty).toBe(true)
    })

    // The interrupt vanishes -> actioned elsewhere.
    ref.current = gatedDetail(threadId, [])
    await act(async () => {
      await queryClient.invalidateQueries()
    })
    await waitFor(() => expect(result.current.state.tag).toBe('superseded'))
    expect(result.current.state).toMatchObject({ by: 'cleared' })

    // A NEW interrupt id replaces it -> fresh open instance, draft reset.
    ref.current = gatedDetail(threadId, [phaseInterrupt('int-2')])
    await act(async () => {
      await queryClient.invalidateQueries()
    })
    await waitFor(() => expect(result.current.gate?.interrupt_id).toBe('int-2'))
    const state = result.current.state
    expect(state.tag).toBe('open')
    if (state.tag === 'open') expect(state.draft).toEqual({})
  })

  it('hint with no hydrated detail triggers one snapshot refetch per hint identity', async () => {
    const threadId = 'th-hint'
    const { handler, ref } = mutableDetailHandler(threadId, gatedDetail(threadId, []))
    server.use(handler)
    const { wrapper } = harness()
    const { result, rerender } = renderHook(
      ({ gateHint }: { gateHint: GateHintLike }) => useGate(threadId, { gateHint }),
      { wrapper, initialProps: { gateHint: null as GateHintLike } },
    )

    await waitFor(() => expect(ref.requests).toBe(1))
    expect(result.current.state.tag).toBe('no_gate')

    // gate_opened heard on the stream before the poll: accelerate the detail.
    ref.current = gatedDetail(threadId, [promptInterrupt('int-h1')])
    const hint = { gate: 'prompt_review', phase: 'test_planning' }
    rerender({ gateHint: hint })
    await waitFor(() => expect(ref.requests).toBe(2))
    await waitFor(() => expect(result.current.gate?.interrupt_id).toBe('int-h1'))

    // Same hint object re-rendered -> no extra refetch.
    rerender({ gateHint: hint })
    await new Promise((resolve) => setTimeout(resolve, 30))
    expect(ref.requests).toBe(2)
  })

  it('resets handled stream hints when the thread changes', async () => {
    const { handler: handlerA, ref: refA } = mutableDetailHandler(
      'th-hint-a',
      gatedDetail('th-hint-a', []),
    )
    const { handler: handlerB, ref: refB } = mutableDetailHandler(
      'th-hint-b',
      gatedDetail('th-hint-b', []),
    )
    server.use(handlerA, handlerB)
    const { queryClient, wrapper } = harness()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const hint = { gate: 'prompt_review', phase: 'test_planning' }
    const { rerender } = renderHook(
      ({ threadId, gateHint }: { threadId: string; gateHint: GateHintLike }) =>
        useGate(threadId, { gateHint }),
      { wrapper, initialProps: { threadId: 'th-hint-a', gateHint: null as GateHintLike } },
    )

    await waitFor(() => expect(refA.requests).toBe(1))
    rerender({ threadId: 'th-hint-a', gateHint: hint })
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: queryKeys.threads.state('th-hint-a'),
      }),
    )

    rerender({ threadId: 'th-hint-b', gateHint: hint })
    await waitFor(() => expect(refB.requests).toBeGreaterThanOrEqual(1))
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: queryKeys.threads.state('th-hint-b'),
      }),
    )
  })
})

describe('useGate resume wiring', () => {
  it('fails closed when the payload kind does not match the interrupt envelope', async () => {
    const threadId = 'th-envelope-drift'
    const mismatched = promptInterrupt('int-envelope-drift')
    mismatched.kind = 'phase_review'
    ;(mismatched.payload as Record<string, unknown>)['sensitive'] = 'payload-secret-canary'
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [mismatched]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const { wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    expect(result.current.gate).toMatchObject({ kind: 'phase_review', payload: null })
    act(() => result.current.submit('approve'))

    expect(resume.captured.calls).toEqual([])
    expect(JSON.stringify(warn.mock.calls)).not.toContain('payload-secret-canary')
  })

  it('rejects an advertised action that is invalid for the gate kind', async () => {
    const threadId = 'th-action-drift'
    const drifted = promptInterrupt('int-action-drift')
    const payload = drifted.payload as Record<string, unknown>
    payload['actions'] = ['approve', 'revise']
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [drifted]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const { wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    act(() => result.current.submit('revise'))

    expect(result.current.state.tag).toBe('open')
    expect(resume.captured.calls).toEqual([])
  })

  it('does not submit an action when the pending payload failed contract parsing', async () => {
    const threadId = 'th-drift'
    const malformed = promptInterrupt('int-drift')
    malformed.payload = {
      schema_version: 1,
      kind: 'prompt_review',
      phase: 'test_planning',
      actions: ['approve', 'modify', 'skip_phase', 'abort'],
    }
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [malformed]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const { wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    expect(result.current.gate?.payload).toBeNull()
    act(() => result.current.submit('approve'))

    expect(result.current.state.tag).toBe('open')
    expect(resume.captured.calls).toEqual([])
  })

  it('terminal 202 suppresses a stale same-id snapshot echo', async () => {
    const threadId = 'th-202'
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [promptInterrupt('int-a')]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const { wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    act(() => result.current.submit('approve'))
    expect(result.current.state.tag).toBe('submitting')

    await waitFor(() => expect(result.current.state.tag).toBe('no_gate'))
    expect(resume.captured.last()).toEqual({
      threadId,
      interruptId: 'int-a',
      body: { action: 'approve' },
    })
    // The invalidated refetch still echoes int-a; the settled-gate suppression
    // must NOT re-open it.
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(result.current.state.tag).toBe('no_gate')
  })

  it('modify reopens review from a refreshed snapshot even when LangGraph reuses the id', async () => {
    const threadId = 'th-modify-same-id'
    const initial = promptInterrupt('int-same')
    const { handler, ref } = mutableDetailHandler(threadId, gatedDetail(threadId, [initial]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const { wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    act(() => result.current.edit({ prompt: { system: 'EDITED SYSTEM' } }))

    const reviewed = promptInterrupt('int-same')
    if (reviewed.payload && typeof reviewed.payload === 'object') {
      const prompt = reviewed.payload['prompt'] as Record<string, unknown>
      prompt['system'] = 'EDITED SYSTEM'
    }
    ref.current = gatedDetail(threadId, [reviewed])
    act(() => result.current.submit('modify'))

    await waitFor(() => expect(ref.requests).toBeGreaterThanOrEqual(2))
    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    expect(result.current.gate?.interrupt_id).toBe('int-same')
    const reopened = result.current.state
    if (reopened.tag !== 'open') throw new Error('expected same-id gate to reopen')
    expect(reopened.draft.prompt?.system).toBe('EDITED SYSTEM')
    expect(reopened.dirty).toBe(false)
  })

  it('uses the pre-submit generation when a same-id retry reopens before the 202 response', async () => {
    const threadId = 'th-retry-pre-202'
    const interrupt = engineRetryInterrupt('int-reused')
    const { handler, ref } = mutableDetailHandler(
      threadId,
      gatedDetail(threadId, [interrupt]),
    )
    let releaseResponse!: () => void
    const waitForResponse = new Promise<void>((resolve) => {
      releaseResponse = resolve
    })
    const resume = resumeHandler(202, { waitForResponse })
    server.use(handler, resume.handler)
    const { queryClient, wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    act(() => result.current.submit('retry'))
    await waitFor(() => expect(resume.captured.calls).toHaveLength(1))

    // The engine re-interrupts with the same deterministic id/payload before
    // the resume endpoint returns. This must count as the next generation.
    const refreshed = gatedDetail(threadId, [engineRetryInterrupt('int-reused')])
    refreshed.updated_at = '2026-06-12T10:00:01+00:00'
    ref.current = refreshed
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.threads.state(threadId) })
    })
    expect(result.current.state.tag).toBe('submitting')

    act(() => releaseResponse())
    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    expect(result.current.gate?.interrupt_id).toBe('int-reused')
  })

  it('coalesces same-tick double submits into one resume request', async () => {
    const threadId = 'th-double-submit'
    const { handler } = mutableDetailHandler(
      threadId,
      gatedDetail(threadId, [promptInterrupt('int-double')]),
    )
    let releaseResponse!: () => void
    const waitForResponse = new Promise<void>((resolve) => {
      releaseResponse = resolve
    })
    const resume = resumeHandler(202, { waitForResponse })
    server.use(handler, resume.handler)
    const { wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    act(() => {
      result.current.submit('approve')
      result.current.submit('approve')
    })
    await waitFor(() => expect(resume.captured.calls).toHaveLength(1))
    expect(result.current.state.tag).toBe('submitting')

    act(() => releaseResponse())
    await waitFor(() => expect(result.current.state.tag).toBe('no_gate'))
  })

  it('leaves awaiting_agent when a refreshed run settles without reopening a gate', async () => {
    const threadId = 'th-modify-terminal'
    const { handler, ref } = mutableDetailHandler(
      threadId,
      gatedDetail(threadId, [promptInterrupt('int-terminal')]),
    )
    server.use(handler, resumeHandler(202).handler)
    const { wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    ref.current = gatedDetail(threadId, [])
    act(() => result.current.submit('modify'))

    await waitFor(() => expect(ref.requests).toBeGreaterThanOrEqual(2))
    await waitFor(() => expect(result.current.state.tag).toBe('no_gate'))
  })

  it('409 gate_superseded -> superseded(by conflict)', async () => {
    const threadId = 'th-409'
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [promptInterrupt('int-b')]))
    server.use(handler, resumeHandler(409).handler)
    const { wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    act(() => result.current.submit('approve'))
    await waitFor(() => expect(result.current.state.tag).toBe('superseded'))
    expect(result.current.state).toMatchObject({ by: 'conflict' })
  })

  it('5xx -> failed with the draft preserved; retry resubmits the same action+draft', async () => {
    const threadId = 'th-500'
    const { handler, ref } = mutableDetailHandler(
      threadId,
      gatedDetail(threadId, [promptInterrupt('int-c')]),
    )
    const failing = resumeHandler(500)
    server.use(handler, failing.handler)
    const { wrapper } = harness()
    const { result } = renderHook(() => useGate(threadId), { wrapper })

    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    act(() => result.current.edit({ prompt: { user: 'EDITED USER' } }))
    act(() => result.current.submit('modify'))
    await waitFor(() => expect(result.current.state.tag).toBe('failed'))
    const failed = result.current.state
    if (failed.tag !== 'failed') throw new Error('expected failed')
    expect(failed.action).toBe('modify')
    expect(failed.draft.prompt?.user).toBe('EDITED USER')

    // Retry goes back through submitting with the identical body.
    const ok = resumeHandler(202)
    server.use(ok.handler)
    const reviewed = promptInterrupt('int-c')
    if (reviewed.payload && typeof reviewed.payload === 'object') {
      const prompt = reviewed.payload['prompt'] as Record<string, unknown>
      prompt['user'] = 'EDITED USER'
    }
    ref.current = gatedDetail(threadId, [reviewed])
    act(() => result.current.submit('modify'))
    await waitFor(() => expect(result.current.state.tag).toBe('open'))
    expect(ok.captured.last()?.body).toEqual({
      action: 'modify',
      prompt: {
        system: 'You are the planning agent.',
        user: 'EDITED USER',
        application: 'Checkout must preserve carts during payment retries.',
      },
    })
  })
})
