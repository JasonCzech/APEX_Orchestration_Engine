import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import type { PropsWithChildren } from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { queryKeys } from '@/api/queryKeys'
import {
  getConsumerKeyHandoffSnapshot,
  invalidateConsumerKeyHandoffLifecycle,
} from '@/auth/consumerKeyHandoff'
import { CONSUMER_CI } from '@/features/admin/__tests__/adminTestHandlers'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { useDeleteConsumer, useRotateConsumerKey, useUpdateConsumer } from './useConsumers'

function wrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

describe('consumer mutation lifecycle', () => {
  beforeEach(invalidateConsumerKeyHandoffLifecycle)
  afterEach(invalidateConsumerKeyHandoffLifecycle)

  it('does not let late update or rotation responses resurrect a deleted consumer or key', async () => {
    let markUpdateStarted!: () => void
    const updateStarted = new Promise<void>((resolve) => {
      markUpdateStarted = resolve
    })
    let releaseUpdate!: () => void
    const updateRelease = new Promise<void>((resolve) => {
      releaseUpdate = resolve
    })
    let markRotateStarted!: () => void
    const rotateStarted = new Promise<void>((resolve) => {
      markRotateStarted = resolve
    })
    let releaseRotate!: () => void
    const rotateRelease = new Promise<void>((resolve) => {
      releaseRotate = resolve
    })

    server.use(
      http.patch('*/v1/admin/consumers/:id', async () => {
        markUpdateStarted()
        await updateRelease
        return HttpResponse.json({ ...CONSUMER_CI, name: 'late-update' })
      }),
      http.post('*/v1/admin/consumers/:id/rotate', async () => {
        markRotateStarted()
        await rotateRelease
        return HttpResponse.json({
          ...CONSUMER_CI,
          key_fingerprint: 'late-rotation',
          api_key: 'apex_key_must_not_surface',
        })
      }),
      http.delete(
        '*/v1/admin/consumers/:id',
        () => new HttpResponse(null, { status: 204 }),
      ),
    )

    const queryClient = createTestQueryClient()
    queryClient.setQueryData(queryKeys.admin.consumer(CONSUMER_CI.id), CONSUMER_CI)
    const { result } = renderHook(
      () => ({
        update: useUpdateConsumer(CONSUMER_CI.id),
        rotate: useRotateConsumerKey(CONSUMER_CI.id),
        remove: useDeleteConsumer(CONSUMER_CI.id),
      }),
      { wrapper: wrapper(queryClient) },
    )

    act(() => {
      result.current.update.mutate({
        consumerId: CONSUMER_CI.id,
        body: { enabled: false },
      })
      result.current.rotate.mutate({
        consumerId: CONSUMER_CI.id,
        consumerName: CONSUMER_CI.name,
        isCurrentConsumer: false,
      })
    })
    await Promise.all([updateStarted, rotateStarted])
    expect(getConsumerKeyHandoffSnapshot().pending).toHaveLength(1)

    await act(async () => {
      await result.current.remove.mutateAsync(CONSUMER_CI.id)
    })
    expect(queryClient.getQueryData(queryKeys.admin.consumer(CONSUMER_CI.id))).toBeUndefined()

    act(() => {
      releaseUpdate()
      releaseRotate()
    })
    await waitFor(() => {
      expect(result.current.update.isSuccess).toBe(true)
      expect(result.current.rotate.isSuccess).toBe(true)
    })

    expect(queryClient.getQueryData(queryKeys.admin.consumer(CONSUMER_CI.id))).toBeUndefined()
    expect(getConsumerKeyHandoffSnapshot()).toEqual({ pending: [], handoffs: [] })
  })
})
