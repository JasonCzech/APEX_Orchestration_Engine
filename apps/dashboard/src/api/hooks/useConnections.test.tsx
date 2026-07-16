import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import type { PropsWithChildren } from 'react'
import { describe, expect, it } from 'vitest'

import { queryKeys } from '@/api/queryKeys'
import { CONN_JIRA } from '@/features/admin/__tests__/adminTestHandlers'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { useDeleteConnection, useUpdateConnection } from './useConnections'

function wrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

describe('connection mutation lifecycle', () => {
  it('serializes delete behind an in-flight update and leaves the detail removed', async () => {
    let markUpdateStarted!: () => void
    const updateStarted = new Promise<void>((resolve) => {
      markUpdateStarted = resolve
    })
    let releaseUpdate!: () => void
    const updateRelease = new Promise<void>((resolve) => {
      releaseUpdate = resolve
    })
    server.use(
      http.patch('*/v1/admin/connections/:id', async () => {
        markUpdateStarted()
        await updateRelease
        return HttpResponse.json({ ...CONN_JIRA, name: 'late-update' })
      }),
      http.delete('*/v1/admin/connections/:id', () => new HttpResponse(null, { status: 204 })),
    )

    const queryClient = createTestQueryClient()
    queryClient.setQueryData(queryKeys.admin.connection(CONN_JIRA.id), CONN_JIRA)
    const { result } = renderHook(
      () => ({
        update: useUpdateConnection(CONN_JIRA.id),
        remove: useDeleteConnection(CONN_JIRA.id),
      }),
      { wrapper: wrapper(queryClient) },
    )

    act(() => {
      result.current.update.mutate({
        connectionId: CONN_JIRA.id,
        body: { name: 'late-update' },
      })
    })
    await updateStarted

    act(() => {
      result.current.remove.mutate(CONN_JIRA.id)
    })
    await waitFor(() => expect(result.current.remove.isPaused).toBe(true))
    expect(queryClient.getQueryData(queryKeys.admin.connection(CONN_JIRA.id))).toEqual(CONN_JIRA)

    releaseUpdate()
    await waitFor(() => expect(result.current.remove.isSuccess).toBe(true))
    expect(queryClient.getQueryData(queryKeys.admin.connection(CONN_JIRA.id))).toBeUndefined()
  })
})
