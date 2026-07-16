import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import type { PropsWithChildren } from 'react'
import { describe, expect, it } from 'vitest'

import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { useRollbackPrompt, useSaveVersion, useTestPrompt } from './usePrompts'

function wrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

describe('prompt mutation lifecycle', () => {
  it('serializes rollback behind an in-flight version save for the same prompt', async () => {
    let markSaveStarted!: () => void
    const saveStarted = new Promise<void>((resolve) => {
      markSaveStarted = resolve
    })
    let releaseSave!: () => void
    const saveRelease = new Promise<void>((resolve) => {
      releaseSave = resolve
    })
    const events: string[] = []
    server.use(
      http.post('*/v1/prompts/p-story/versions', async ({ request }) => {
        const body = (await request.json()) as { content: string }
        events.push('save:start')
        markSaveStarted()
        await saveRelease
        events.push('save:end')
        return HttpResponse.json(
          {
            id: 'v-3',
            version: 3,
            content: body.content,
            note: null,
            created_by: 'dash-ops',
            created_at: '2026-06-11T11:00:00Z',
            parent_version_id: 'v-2',
          },
          { status: 201 },
        )
      }),
      http.post('*/v1/prompts/p-story/rollback', () => {
        events.push('rollback:start')
        return HttpResponse.json({
          id: 'p-story',
          namespace: 'phase',
          key: 'story_analysis/system',
          description: 'System prompt for story analysis',
          active_version: { id: 'v-1', version: 1 },
          content: 'Be terse.',
          note: 'initial draft',
          archived_at: null,
          updated_at: '2026-06-11T11:01:00Z',
        })
      }),
    )

    const queryClient = createTestQueryClient()
    const { result } = renderHook(
      () => ({
        save: useSaveVersion('p-story'),
        rollback: useRollbackPrompt('phase', 'story_analysis/system', 'p-story'),
      }),
      { wrapper: wrapper(queryClient) },
    )

    act(() => {
      result.current.save.mutate({ content: 'Newest content' })
    })
    await saveStarted

    act(() => {
      result.current.rollback.mutate('v-1')
    })
    await waitFor(() => expect(result.current.rollback.isPaused).toBe(true))
    expect(events).toEqual(['save:start'])

    releaseSave()
    await waitFor(() => expect(result.current.rollback.isSuccess).toBe(true))
    expect(events).toEqual(['save:start', 'save:end', 'rollback:start'])
  })

  it('serializes prompt tests for the same prompt across mutation observers', async () => {
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
      http.post('*/v1/prompts/p-story/test', async () => {
        const attempt = events.filter((event) => event.endsWith(':start')).length + 1
        events.push(`${attempt}:start`)
        if (attempt === 1) {
          markFirstStarted()
          await firstRelease
        }
        events.push(`${attempt}:end`)
        return HttpResponse.json(
          { run_id: `run-${attempt}`, thread_id: `thread-${attempt}` },
          { status: 202 },
        )
      }),
    )

    const queryClient = createTestQueryClient()
    const { result } = renderHook(
      () => ({
        first: useTestPrompt('p-story'),
        second: useTestPrompt('p-story'),
      }),
      { wrapper: wrapper(queryClient) },
    )
    const submission = {
      request: { version_id: 'v-2', sample_input: {}, project_id: 'proj-alpha' },
      history: {
        promptId: 'p-story',
        projectId: 'proj-alpha',
        appId: '',
        label: 'v2',
      },
    }

    act(() => {
      result.current.first.mutate(submission)
    })
    await firstStarted

    act(() => {
      result.current.second.mutate(submission)
    })
    await waitFor(() => expect(result.current.second.isPaused).toBe(true))
    expect(events).toEqual(['1:start'])

    releaseFirst()
    await waitFor(() => expect(result.current.second.isSuccess).toBe(true))
    expect(events).toEqual(['1:start', '1:end', '2:start', '2:end'])
  })
})
