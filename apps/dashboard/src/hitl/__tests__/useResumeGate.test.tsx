/**
 * useResumeGate mapping tests: 202 -> onAccepted + cache invalidations,
 * 409 problem(title gate_superseded) -> conflict rejection, 5xx -> plain
 * rejection. msw plays the backend.
 */
import type { ReactNode } from 'react'

import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { queryKeys } from '@/api/queryKeys'
import { isGateSupersededError, useResumeGate } from '@/hitl/useResumeGate'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { resumeHandler } from './gateFixtures'

function setup() {
  const queryClient = createTestQueryClient()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { queryClient, wrapper }
}

const VARS = {
  threadId: 'th-1',
  interruptId: 'int-1',
  body: { action: 'approve' as const, note: 'lgtm' },
}

describe('useResumeGate', () => {
  it('202 -> onAccepted(run_id) + invalidates threads.state and pipelines lists', async () => {
    const { handler, captured } = resumeHandler(202, { runId: 'run-42' })
    server.use(handler)
    const { queryClient, wrapper } = setup()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const onAccepted = vi.fn()
    const onRejected = vi.fn()
    const { result } = renderHook(() => useResumeGate({ onAccepted, onRejected }), { wrapper })

    result.current.mutate(VARS)
    await waitFor(() => expect(onAccepted).toHaveBeenCalledWith('run-42', VARS))
    expect(onRejected).not.toHaveBeenCalled()
    expect(captured.last()).toEqual({
      threadId: 'th-1',
      interruptId: 'int-1',
      body: { action: 'approve', note: 'lgtm' },
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.threads.state('th-1') })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.pipelines.lists() })
  })

  it('409 problem (title gate_superseded) -> onRejected with conflict: true', async () => {
    server.use(resumeHandler(409).handler)
    const { wrapper } = setup()
    const onAccepted = vi.fn()
    const onRejected = vi.fn()
    const { result } = renderHook(() => useResumeGate({ onAccepted, onRejected }), { wrapper })

    result.current.mutate(VARS)
    await waitFor(() => expect(onRejected).toHaveBeenCalled())
    expect(onAccepted).not.toHaveBeenCalled()
    const [rejection] = onRejected.mock.calls[0] as [{ error: Error; conflict: boolean }]
    expect(rejection.conflict).toBe(true)
    expect(isGateSupersededError(rejection.error)).toBe(true)
    // The problem detail surfaces as the error message.
    expect(rejection.error.message).toContain('no longer pending')
  })

  it('5xx -> onRejected with conflict: false', async () => {
    server.use(resumeHandler(500).handler)
    const { wrapper } = setup()
    const onRejected = vi.fn()
    const { result } = renderHook(() => useResumeGate({ onRejected }), { wrapper })

    result.current.mutate(VARS)
    await waitFor(() => expect(onRejected).toHaveBeenCalled())
    const [rejection] = onRejected.mock.calls[0] as [{ error: Error; conflict: boolean }]
    expect(rejection.conflict).toBe(false)
    expect(rejection.error.message).toContain('resume exploded')
  })
})
