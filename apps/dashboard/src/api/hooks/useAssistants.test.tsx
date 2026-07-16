import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import type { PropsWithChildren } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { queryKeys } from '@/api/queryKeys'
import { createTestQueryClient } from '@/test/render'

import { useUpdateGoldenConfig } from './useAssistants'

const { assistantsUpdate } = vi.hoisted(() => ({
  assistantsUpdate: vi.fn(),
}))

vi.mock('@/api/langgraphClient', () => ({
  getLangGraphClient: () =>
    Promise.resolve({
      assistants: { update: assistantsUpdate },
    }),
}))

const ASSISTANT = {
  assistant_id: 'asst-gold',
  graph_id: 'pipeline',
  name: 'Nightly checkout soak',
  description: 'Pinned engine + custom gates',
  config: { configurable: { engine: 'loadrunner' } },
  context: {},
  metadata: { created_by: 'dash-ops' },
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-10T00:00:00Z',
  version: 3,
}

function wrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

describe('golden-config mutation lifecycle', () => {
  it('serializes a remounted save behind the in-flight save for the same assistant', async () => {
    let markFirstStarted!: () => void
    const firstStarted = new Promise<void>((resolve) => {
      markFirstStarted = resolve
    })
    let releaseFirst!: () => void
    const firstRelease = new Promise<void>((resolve) => {
      releaseFirst = resolve
    })
    const events: string[] = []
    assistantsUpdate
      .mockImplementationOnce(async (_assistantId: string, body: unknown) => {
        events.push('first:start')
        markFirstStarted()
        await firstRelease
        events.push('first:end')
        return {
          ...ASSISTANT,
          config: (body as { config: typeof ASSISTANT.config }).config,
          version: 4,
        }
      })
      .mockImplementationOnce(async (_assistantId: string, body: unknown) => {
        events.push('second:start')
        return {
          ...ASSISTANT,
          config: (body as { config: typeof ASSISTANT.config }).config,
          version: 5,
        }
      })

    const queryClient = createTestQueryClient()
    const first = renderHook(() => useUpdateGoldenConfig(ASSISTANT.assistant_id), {
      wrapper: wrapper(queryClient),
    })

    act(() => {
      first.result.current.mutate({ configurable: { engine: 'first' } })
    })
    await firstStarted
    first.unmount()

    const second = renderHook(() => useUpdateGoldenConfig(ASSISTANT.assistant_id), {
      wrapper: wrapper(queryClient),
    })
    act(() => {
      second.result.current.mutate({ configurable: { engine: 'second' } })
    })

    await waitFor(() => expect(second.result.current.isPaused).toBe(true))
    expect(events).toEqual(['first:start'])
    expect(assistantsUpdate).toHaveBeenCalledTimes(1)

    await act(async () => {
      releaseFirst()
      await firstRelease
    })

    await waitFor(() => expect(second.result.current.isSuccess).toBe(true))
    expect(events).toEqual(['first:start', 'first:end', 'second:start'])
    expect(assistantsUpdate).toHaveBeenNthCalledWith(1, ASSISTANT.assistant_id, {
      config: { configurable: { engine: 'first' } },
    })
    expect(assistantsUpdate).toHaveBeenNthCalledWith(2, ASSISTANT.assistant_id, {
      config: { configurable: { engine: 'second' } },
    })
    expect(
      queryClient.getQueryData(queryKeys.goldenConfigs.detail(ASSISTANT.assistant_id)),
    ).toMatchObject({
      configurable: { engine: 'second' },
      version: 5,
    })
  })
})
