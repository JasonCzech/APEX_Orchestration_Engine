/**
 * useActiveRun: active run discovery via the SDK runs.list surface with
 * busy-gated polling.
 */
import type { ReactNode } from 'react'

import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { ACTIVE_RUN_POLL_MS, useActiveRun } from '../useActiveRun'
import { FakeLangGraphClient } from './fakeLangGraph'

const clientHolder = vi.hoisted(() => ({ current: null as unknown }))

vi.mock('@/api/langgraphClient', () => ({
  getLangGraphClient: () => Promise.resolve(clientHolder.current),
  resetLangGraphClient: () => {},
}))

let fake: FakeLangGraphClient

beforeEach(() => {
  vi.useFakeTimers()
  fake = new FakeLangGraphClient()
  clientHolder.current = fake.asClient()
})

afterEach(() => {
  vi.useRealTimers()
})

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

async function pump(ms = 10): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('useActiveRun', () => {
  it('returns the running run id for a busy thread (running preferred over pending)', async () => {
    fake.listRuns = [
      { run_id: 'r-pending', status: 'pending' },
      { run_id: 'r-running', status: 'running' },
      { run_id: 'r-done', status: 'success' },
    ]
    const { result } = renderHook(() => useActiveRun('t1', { threadStatus: 'busy' }), {
      wrapper: makeWrapper(),
    })
    await pump()
    expect(result.current).toBe('r-running')
    expect(fake.listCalls[0]?.threadId).toBe('t1')
  })

  it('falls back to a pending run when nothing is running yet', async () => {
    fake.listRuns = [
      { run_id: 'r-queued', status: 'pending' },
      { run_id: 'r-old', status: 'interrupted' },
    ]
    const { result } = renderHook(() => useActiveRun('t1', { threadStatus: 'busy' }), {
      wrapper: makeWrapper(),
    })
    await pump()
    expect(result.current).toBe('r-queued')
  })

  it('polls while the thread is busy', async () => {
    fake.listRuns = [{ run_id: 'r-running', status: 'running' }]
    renderHook(() => useActiveRun('t1', { threadStatus: 'busy' }), { wrapper: makeWrapper() })
    await pump()
    expect(fake.listCalls).toHaveLength(1)
    await pump(ACTIVE_RUN_POLL_MS + 100)
    expect(fake.listCalls).toHaveLength(2)
  })

  it('is disabled (null, zero requests) when the thread is known not-busy', async () => {
    const { result } = renderHook(() => useActiveRun('t1', { threadStatus: 'idle' }), {
      wrapper: makeWrapper(),
    })
    await pump()
    expect(result.current).toBeNull()
    expect(fake.listCalls).toHaveLength(0)
  })

  it('unknown thread status: probes once, returns null, and stops polling when nothing is live', async () => {
    fake.listRuns = [{ run_id: 'r-done', status: 'success' }]
    const { result } = renderHook(() => useActiveRun('t1'), { wrapper: makeWrapper() })
    await pump()
    expect(result.current).toBeNull()
    expect(fake.listCalls).toHaveLength(1)
    await pump(3 * ACTIVE_RUN_POLL_MS)
    expect(fake.listCalls).toHaveLength(1)
  })
})
