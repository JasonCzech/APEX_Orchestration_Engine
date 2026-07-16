import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import type { PropsWithChildren } from 'react'
import { describe, expect, it } from 'vitest'

import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import {
  useDeleteSavedQuery,
  useEnrichWorkItem,
  useUpdateSavedQuery,
} from './useWorkTracking'

function wrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

describe('work-tracking mutation lifecycle', () => {
  it('serializes enrichments for the same bound tracker item', async () => {
    let markFirstStarted!: () => void
    const firstStarted = new Promise<void>((resolve) => {
      markFirstStarted = resolve
    })
    let releaseFirst!: () => void
    const firstRelease = new Promise<void>((resolve) => {
      releaseFirst = resolve
    })
    const events: string[] = []
    server.use(
      http.post('*/v1/work-tracking/items/PHX-101/enrich', async ({ request }) => {
        const body = (await request.json()) as { comment: string }
        events.push(`${body.comment}:start`)
        if (body.comment === 'first') {
          markFirstStarted()
          await firstRelease
        }
        events.push(`${body.comment}:end`)
        return HttpResponse.json({
          key: 'PHX-101',
          title: 'Checkout issue',
          kind: 'story',
          status: body.comment,
          description: '',
          url: null,
          connection_id: 'conn-jira',
          provider: 'jira',
        })
      }),
    )

    const queryClient = createTestQueryClient()
    const { result } = renderHook(
      () => ({
        first: useEnrichWorkItem('conn-jira', 'PHX-101'),
        second: useEnrichWorkItem('conn-jira', 'PHX-101'),
      }),
      { wrapper: wrapper(queryClient) },
    )

    act(() => {
      result.current.first.mutate({
        key: 'PHX-101',
        body: { fields: {}, comment: 'first' },
        connectionId: 'conn-jira',
        idempotencyKey: 'enrich-first',
      })
    })
    await firstStarted

    act(() => {
      result.current.second.mutate({
        key: 'PHX-101',
        body: { fields: {}, comment: 'second' },
        connectionId: 'conn-jira',
        idempotencyKey: 'enrich-second',
      })
    })
    await waitFor(() => expect(result.current.second.isPaused).toBe(true))
    expect(events).toEqual(['first:start'])

    releaseFirst()
    await waitFor(() => expect(result.current.second.isSuccess).toBe(true))
    expect(events).toEqual(['first:start', 'first:end', 'second:start', 'second:end'])
  })

  it('serializes saved-query deletion behind an in-flight update', async () => {
    let markUpdateStarted!: () => void
    const updateStarted = new Promise<void>((resolve) => {
      markUpdateStarted = resolve
    })
    let releaseUpdate!: () => void
    const updateRelease = new Promise<void>((resolve) => {
      releaseUpdate = resolve
    })
    const events: string[] = []
    server.use(
      http.patch('*/v1/work-tracking/saved-queries/sq-1', async ({ request }) => {
        const body = (await request.json()) as { name: string }
        events.push('update:start')
        markUpdateStarted()
        await updateRelease
        events.push('update:end')
        return HttpResponse.json({
          id: 'sq-1',
          name: body.name,
          provider: 'jira',
          query: 'project = PHX',
          description: null,
          project_id: 'proj-alpha',
          connection_id: 'conn-jira',
          created_by: 'operator',
          created_at: '2026-06-01T00:00:00Z',
          updated_at: '2026-06-01T00:01:00Z',
        })
      }),
      http.delete('*/v1/work-tracking/saved-queries/sq-1', () => {
        events.push('delete:start')
        return new HttpResponse(null, { status: 204 })
      }),
    )

    const queryClient = createTestQueryClient()
    const { result } = renderHook(
      () => ({
        update: useUpdateSavedQuery('sq-1'),
        remove: useDeleteSavedQuery('sq-1'),
      }),
      { wrapper: wrapper(queryClient) },
    )

    act(() => {
      result.current.update.mutate({
        savedQueryId: 'sq-1',
        body: { name: 'Updated query' },
      })
    })
    await updateStarted

    act(() => {
      result.current.remove.mutate('sq-1')
    })
    await waitFor(() => expect(result.current.remove.isPaused).toBe(true))
    expect(events).toEqual(['update:start'])

    releaseUpdate()
    await waitFor(() => expect(result.current.remove.isSuccess).toBe(true))
    expect(events).toEqual(['update:start', 'update:end', 'delete:start'])
  })
})
