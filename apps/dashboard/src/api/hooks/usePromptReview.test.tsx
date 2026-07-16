import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import type { PropsWithChildren } from 'react'
import { describe, expect, it } from 'vitest'

import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { useUpdatePromptReview } from './usePromptReview'

function wrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

const REVIEW_BODY = {
  system: 'System',
  phase_prompt: 'Phase',
  application: 'Application',
  additional_context: '',
}

describe('prompt-review mutation lifecycle', () => {
  it('serializes writes for different phases of the same thread', async () => {
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
      http.patch('*/v1/pipelines/thread-1/phases/:phase/prompt-review', async ({ params }) => {
        const phase = String(params['phase'])
        events.push(`${phase}:start`)
        if (phase === 'story_analysis') {
          markFirstStarted()
          await firstRelease
        }
        events.push(`${phase}:end`)
        return HttpResponse.json({
          ...REVIEW_BODY,
          source: { origin: 'run_override' },
          updated_at: '2026-06-01T00:01:00Z',
          updated_by: 'operator',
        })
      }),
    )

    const queryClient = createTestQueryClient()
    const { result } = renderHook(
      () => ({
        first: useUpdatePromptReview('thread-1'),
        second: useUpdatePromptReview('thread-1'),
      }),
      { wrapper: wrapper(queryClient) },
    )

    act(() => {
      result.current.first.mutate({
        threadId: 'thread-1',
        phase: 'story_analysis',
        body: REVIEW_BODY,
      })
    })
    await firstStarted

    act(() => {
      result.current.second.mutate({
        threadId: 'thread-1',
        phase: 'test_planning',
        body: REVIEW_BODY,
      })
    })
    await waitFor(() => expect(result.current.second.isPaused).toBe(true))
    expect(events).toEqual(['story_analysis:start'])

    releaseFirst()
    await waitFor(() => expect(result.current.second.isSuccess).toBe(true))
    expect(events).toEqual([
      'story_analysis:start',
      'story_analysis:end',
      'test_planning:start',
      'test_planning:end',
    ])
  })
})
