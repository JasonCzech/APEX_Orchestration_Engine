import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '@/test/server'
import { useCaptureCycleSource, useCycles, useFreezeReportEvidence } from '../api'

describe('deliverable captures refresh cycle list progress', () => {
  it.each(['report', 'scope'] as const)('returning to a fresh list after %s capture reads the new deliverable state', async (kind) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    let revision = 1
    let reads = 0
    const cycle = () => ({ id: 'cycle', revision, progress: { status: revision === 1 ? 'approved' : 'draft', approved: revision === 1 ? 6 : 5 } })
    server.use(
      http.get('*/v1/cycles', () => { reads += 1; return HttpResponse.json([cycle()]) }),
      http.post(`*/v1/cycles/cycle/${kind === 'report' ? 'reports/version/evidence' : 'scopes/version/sources'}`, () => { revision += 1; return HttpResponse.json(cycle()) }),
    )
    const list = renderHook(() => useCycles(0, true), { wrapper })
    await waitFor(() => expect(list.result.current.data?.[0]?.progress?.approved).toBe(6))
    list.unmount()
    const capture = renderHook(() => ({ report: useFreezeReportEvidence('cycle', 'version'), scope: useCaptureCycleSource('cycle', 'version') }), { wrapper })
    await act(async () => {
      if (kind === 'report') await capture.result.current.report.mutateAsync({ expected_revision: 1, idempotency_key: 'capture', expected_results: { run: 'a'.repeat(64) } })
      else await capture.result.current.scope.mutateAsync({ expected_revision: 1, idempotency_key: 'capture', connection_id: 'confluence', connection_version: '2026-10-17T12:00:00Z', expected_checksum: 'b'.repeat(64), ref: { id: 'page', revision: '1', source: 'confluence' }, title: 'Captured scope' })
    })
    capture.unmount()
    const returned = renderHook(() => useCycles(0, true), { wrapper })
    await waitFor(() => expect(returned.result.current.data?.[0]?.progress).toMatchObject({ status: 'draft', approved: 5 }))
    expect(reads).toBe(2)
    returned.unmount()
    client.clear()
  })
})
