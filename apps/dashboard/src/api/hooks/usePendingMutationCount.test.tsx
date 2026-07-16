import { act, renderHook, waitFor } from '@testing-library/react'
import {
  QueryClientProvider,
  useMutation,
  type QueryClient,
} from '@tanstack/react-query'
import type { PropsWithChildren } from 'react'
import { describe, expect, it } from 'vitest'

import { createTestQueryClient } from '@/test/render'

import { usePendingMutationCount } from './usePendingMutationCount'

function wrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

describe('usePendingMutationCount', () => {
  it('recomputes the exact pending count when only the requested key changes', async () => {
    let release!: () => void
    const pending = new Promise<void>((resolve) => {
      release = resolve
    })
    const queryClient = createTestQueryClient()
    const queryWrapper = wrapper(queryClient)
    const mutation = renderHook(
      () =>
        useMutation({
          mutationKey: ['resource-write', 'second'],
          mutationFn: () => pending,
        }),
      { wrapper: queryWrapper },
    )
    const count = renderHook(
      ({ id }: { id: string }) => usePendingMutationCount(['resource-write', id]),
      { initialProps: { id: 'first' }, wrapper: queryWrapper },
    )

    act(() => mutation.result.current.mutate())
    await waitFor(() => expect(mutation.result.current.isPending).toBe(true))
    expect(count.result.current).toBe(0)

    count.rerender({ id: 'second' })
    expect(count.result.current).toBe(1)

    count.rerender({ id: 'first' })
    expect(count.result.current).toBe(0)

    await act(async () => {
      release()
      await pending
    })
  })
})
