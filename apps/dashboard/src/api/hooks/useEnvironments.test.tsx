import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import type { PropsWithChildren } from 'react'
import { describe, expect, it } from 'vitest'

import { queryKeys } from '@/api/queryKeys'
import {
  ENV_STAGING,
  SNAPSHOT_FRESH,
  inventoryOf,
} from '@/features/environments/environmentsTestHandlers'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { useDeleteEnvironment, useUpdateEnvironment } from './useEnvironments'
import { useRescanEnvironment } from './useInventory'

function wrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

describe('environment mutation lifecycle', () => {
  it('serializes delete behind an in-flight update and leaves the detail removed', async () => {
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
      http.patch('*/v1/catalog/environments/:id', async () => {
        events.push('update:start')
        markUpdateStarted()
        await updateRelease
        events.push('update:end')
        return HttpResponse.json({ ...ENV_STAGING, name: 'late-update' })
      }),
      http.delete(
        '*/v1/catalog/environments/:id',
        () => {
          events.push('delete:start')
          return new HttpResponse(null, { status: 204 })
        },
      ),
    )

    const queryClient = createTestQueryClient()
    queryClient.setQueryData(queryKeys.catalog.environment(ENV_STAGING.id), ENV_STAGING)
    const { result } = renderHook(
      () => ({
        update: useUpdateEnvironment(ENV_STAGING.id),
        remove: useDeleteEnvironment(ENV_STAGING.id),
      }),
      { wrapper: wrapper(queryClient) },
    )

    act(() => {
      result.current.update.mutate({
        environmentId: ENV_STAGING.id,
        body: { kind: 'other' },
      })
    })
    await updateStarted

    act(() => {
      result.current.remove.mutate(ENV_STAGING.id)
    })
    await waitFor(() => expect(result.current.remove.isPaused).toBe(true))
    expect(events).toEqual(['update:start'])

    releaseUpdate()
    await waitFor(() => expect(result.current.remove.isSuccess).toBe(true))
    expect(events).toEqual(['update:start', 'update:end', 'delete:start'])
    expect(queryClient.getQueryData(queryKeys.catalog.environment(ENV_STAGING.id))).toBeUndefined()
  })

  it('queues a rescan behind an in-flight edit before committing inventory', async () => {
    let markUpdateStarted!: () => void
    const updateStarted = new Promise<void>((resolve) => {
      markUpdateStarted = resolve
    })
    let releaseUpdate!: () => void
    const updateRelease = new Promise<void>((resolve) => {
      releaseUpdate = resolve
    })
    const events: string[] = []
    const freshInventory = inventoryOf(ENV_STAGING.id, SNAPSHOT_FRESH)
    server.use(
      http.patch('*/v1/catalog/environments/:id', async () => {
        events.push('update:start')
        markUpdateStarted()
        await updateRelease
        events.push('update:end')
        return HttpResponse.json({ ...ENV_STAGING, kind: 'other' })
      }),
      http.post('*/v1/inventory/environments/:id/rescan', () => {
        events.push('rescan:start')
        return HttpResponse.json(freshInventory)
      }),
    )

    const queryClient = createTestQueryClient()
    const { result } = renderHook(
      () => ({
        update: useUpdateEnvironment(ENV_STAGING.id),
        rescan: useRescanEnvironment(ENV_STAGING.id),
      }),
      { wrapper: wrapper(queryClient) },
    )

    act(() => {
      result.current.update.mutate({
        environmentId: ENV_STAGING.id,
        body: { kind: 'other' },
      })
    })
    await updateStarted

    act(() => {
      result.current.rescan.mutate()
    })
    await waitFor(() => expect(result.current.rescan.isPaused).toBe(true))
    expect(events).toEqual(['update:start'])

    releaseUpdate()
    await waitFor(() => expect(result.current.rescan.isSuccess).toBe(true))
    expect(events).toEqual(['update:start', 'update:end', 'rescan:start'])
    expect(
      queryClient.getQueryData(queryKeys.inventory.environment(ENV_STAGING.id)),
    ).toEqual(freshInventory)
  })
})
