import { act, renderHook, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { StrictMode, type ReactNode } from 'react'
import { describe, expect, it } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { bumpSessionRevision } from '@/auth/keyStorage'
import { AuthProvider, type AuthState } from '@/auth/AuthProvider'
import { authenticatedState, createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { useDraft } from '../useDraft'
import { draftRead } from './wizardTestUtils'

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((next) => {
    resolve = next
  })
  return { promise, resolve }
}

function createWrapper({
  strict = false,
  authState,
}: {
  strict?: boolean
  authState?: AuthState
} = {}) {
  const queryClient = createTestQueryClient()
  return function Wrapper({ children }: { children: ReactNode }) {
    const content = authState ? (
      <AuthProvider staticState={authState}>{children}</AuthProvider>
    ) : (
      children
    )
    const provider = <QueryClientProvider client={queryClient}>{content}</QueryClientProvider>
    return strict ? <StrictMode>{provider}</StrictMode> : provider
  }
}

describe('useDraft operation ordering', () => {
  it('discards a slower load when a newer resume request wins', async () => {
    const firstStarted = deferred<void>()
    const releaseFirst = deferred<void>()
    server.use(
      http.get('*/v1/drafts/:id', async ({ params }) => {
        const id = String(params['id'])
        if (id === 'draft-a') {
          firstStarted.resolve()
          await releaseFirst.promise
        }
        return HttpResponse.json(
          draftRead({
            id,
            title: id,
            payload: { title: id === 'draft-a' ? 'Older draft' : 'Latest draft' },
          }),
        )
      }),
    )
    const { result } = renderHook(
      () => useDraft({ initialDraftId: null }),
      { wrapper: createWrapper() },
    )

    let firstLoad!: Promise<boolean>
    act(() => {
      firstLoad = result.current.loadDraft('draft-a')
    })
    await firstStarted.promise
    expect(result.current.loading).toBe(true)

    let secondResult = false
    await act(async () => {
      secondResult = await result.current.loadDraft('draft-b')
    })
    expect(secondResult).toBe(true)
    expect(result.current.draftId).toBe('draft-b')
    expect(result.current.draft.title).toBe('Latest draft')

    let firstResult = true
    await act(async () => {
      releaseFirst.resolve()
      firstResult = await firstLoad
    })
    expect(firstResult).toBe(false)
    expect(result.current.draftId).toBe('draft-b')
    expect(result.current.draft.title).toBe('Latest draft')
  })

  it('cancels a URL-driven load when navigation returns to the loaded draft', async () => {
    const loadStarted = deferred<void>()
    const releaseLoad = deferred<void>()
    server.use(
      http.get('*/v1/drafts/:id', async ({ params }) => {
        const id = String(params['id'])
        if (id === 'draft-b') {
          loadStarted.resolve()
          await releaseLoad.promise
        }
        return HttpResponse.json(
          draftRead({ id, payload: { title: id === 'draft-a' ? 'Draft A' : 'Draft B' } }),
        )
      }),
    )
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useDraft({ initialDraftId: id }),
      { initialProps: { id: 'draft-a' }, wrapper: createWrapper() },
    )
    await waitFor(() => expect(result.current.draft.title).toBe('Draft A'))

    act(() => rerender({ id: 'draft-b' }))
    await loadStarted.promise
    expect(result.current.loading).toBe(true)

    act(() => rerender({ id: 'draft-a' }))
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(async () => {
      releaseLoad.resolve()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })

    expect(result.current.draftId).toBe('draft-a')
    expect(result.current.draft.title).toBe('Draft A')
  })

  it('allows a failed URL draft to be retried after rolling back to the current draft', async () => {
    let draftBAttempts = 0
    server.use(
      http.get('*/v1/drafts/:id', ({ params }) => {
        const id = String(params['id'])
        if (id === 'draft-b' && ++draftBAttempts === 1) {
          return HttpResponse.json({ detail: 'draft B temporarily unavailable' }, { status: 503 })
        }
        return HttpResponse.json(
          draftRead({ id, payload: { title: id === 'draft-a' ? 'Draft A' : 'Draft B' } }),
        )
      }),
    )
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useDraft({ initialDraftId: id }),
      { initialProps: { id: 'draft-a' }, wrapper: createWrapper() },
    )
    await waitFor(() => expect(result.current.draft.title).toBe('Draft A'))

    act(() => rerender({ id: 'draft-b' }))
    await waitFor(() =>
      expect(result.current.loadFailure?.requestedDraftId).toBe('draft-b'),
    )
    expect(result.current.draftId).toBe('draft-a')

    act(() => rerender({ id: 'draft-a' }))
    act(() => rerender({ id: 'draft-b' }))
    await waitFor(() => expect(result.current.draft.title).toBe('Draft B'))

    expect(result.current.loadFailure).toBeNull()
    expect(draftBAttempts).toBe(2)
  })

  it('does not publish a stale load failure over a newer URL draft request', async () => {
    const bStarted = deferred<void>()
    const releaseB = deferred<void>()
    const cStarted = deferred<void>()
    const releaseC = deferred<void>()
    server.use(
      http.get('*/v1/drafts/:id', async ({ params }) => {
        const id = String(params['id'])
        if (id === 'draft-b') {
          bStarted.resolve()
          await releaseB.promise
          return HttpResponse.json({ detail: 'draft B failed' }, { status: 503 })
        }
        if (id === 'draft-c') {
          cStarted.resolve()
          await releaseC.promise
        }
        return HttpResponse.json(
          draftRead({ id, payload: { title: id === 'draft-a' ? 'Draft A' : 'Draft C' } }),
        )
      }),
    )
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useDraft({ initialDraftId: id }),
      { initialProps: { id: 'draft-a' }, wrapper: createWrapper() },
    )
    await waitFor(() => expect(result.current.draft.title).toBe('Draft A'))

    act(() => rerender({ id: 'draft-b' }))
    await bStarted.promise
    act(() => rerender({ id: 'draft-c' }))
    await cStarted.promise
    await act(async () => {
      releaseB.resolve()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })

    expect(result.current.loadFailure).toBeNull()
    expect(result.current.loading).toBe(true)

    await act(async () => {
      releaseC.resolve()
    })
    await waitFor(() => expect(result.current.draft.title).toBe('Draft C'))
    expect(result.current.draftId).toBe('draft-c')
  })

  it('serializes a resume behind an in-flight create without letting the create overwrite it', async () => {
    const createStarted = deferred<void>()
    const releaseCreate = deferred<void>()
    const requestOrder: string[] = []
    const createdIds: string[] = []
    server.use(
      http.post('*/v1/drafts', async ({ request }) => {
        requestOrder.push('create:start')
        createStarted.resolve()
        const body = (await request.json()) as { payload: Record<string, unknown> }
        await releaseCreate.promise
        requestOrder.push('create:end')
        return HttpResponse.json(
          draftRead({ id: 'draft-created', payload: body.payload }),
          { status: 201 },
        )
      }),
      http.get('*/v1/drafts/:id', ({ params }) => {
        requestOrder.push('load:start')
        return HttpResponse.json(
          draftRead({
            id: String(params['id']),
            payload: { title: 'Stored draft' },
          }),
        )
      }),
    )
    const { result } = renderHook(
      () =>
        useDraft({
          initialDraftId: null,
          onDraftCreated: (id) => createdIds.push(id),
        }),
      { wrapper: createWrapper() },
    )

    act(() => {
      result.current.setDraft((previous) => ({ ...previous, title: 'Local draft' }))
    })
    let save!: Promise<void>
    act(() => {
      save = result.current.saveNow()
    })
    await createStarted.promise

    let resume!: Promise<boolean>
    act(() => {
      resume = result.current.loadDraft('draft-stored')
    })
    expect(requestOrder).toEqual(['create:start'])

    let resumed = false
    await act(async () => {
      releaseCreate.resolve()
      await save
      resumed = await resume
    })

    expect(resumed).toBe(true)
    expect(requestOrder).toEqual(['create:start', 'create:end', 'load:start'])
    // The create preserves local work, but a resume already superseded its URL
    // target, so the old create must not rewrite ?draft= when it completes.
    expect(createdIds).toEqual([])
    expect(result.current.draftId).toBe('draft-stored')
    expect(result.current.draft.title).toBe('Stored draft')
  })

  it('does not discard local work when the pre-resume save fails', async () => {
    let loadCount = 0
    server.use(
      http.post('*/v1/drafts', () =>
        HttpResponse.json({ detail: 'draft store unavailable' }, { status: 503 }),
      ),
      http.get('*/v1/drafts/:id', ({ params }) => {
        loadCount += 1
        return HttpResponse.json(
          draftRead({ id: String(params['id']), payload: { title: 'Stored draft' } }),
        )
      }),
    )
    const { result } = renderHook(
      () => useDraft({ initialDraftId: null }),
      { wrapper: createWrapper() },
    )
    act(() => {
      result.current.setDraft((previous) => ({ ...previous, title: 'Unsaved local draft' }))
    })

    let resumed = true
    await act(async () => {
      resumed = await result.current.loadDraft('draft-stored')
    })

    expect(resumed).toBe(false)
    expect(loadCount).toBe(0)
    expect(result.current.loading).toBe(false)
    expect(result.current.saveState).toBe('error')
    expect(result.current.draft.title).toBe('Unsaved local draft')
    expect(result.current.draftId).toBeNull()
  })

  it('loads an initial URL draft only once under React Strict Mode', async () => {
    let loadCount = 0
    server.use(
      http.get('*/v1/drafts/:id', ({ params }) => {
        loadCount += 1
        return HttpResponse.json(
          draftRead({ id: String(params['id']), payload: { title: 'Strict draft' } }),
        )
      }),
    )
    const { result } = renderHook(
      () => useDraft({ initialDraftId: 'draft-strict' }),
      { wrapper: createWrapper({ strict: true }) },
    )

    await waitFor(() => expect(result.current.draft.title).toBe('Strict draft'))
    expect(loadCount).toBe(1)
  })

  it('serializes an unmount flush before a remounted writer and retires old callbacks', async () => {
    const firstPutStarted = deferred<void>()
    const releaseFirstPut = deferred<void>()
    const savedTitles: string[] = []
    let storedPayload: Record<string, unknown> = { title: 'Stored draft' }
    let putCount = 0
    server.use(
      http.get('*/v1/drafts/draft-shared', () =>
        HttpResponse.json(
          draftRead({ id: 'draft-shared', payload: storedPayload }),
        ),
      ),
      http.put('*/v1/drafts/draft-shared', async ({ request }) => {
        const body = (await request.json()) as { payload: Record<string, unknown> }
        putCount += 1
        if (putCount === 1) {
          firstPutStarted.resolve()
          await releaseFirstPut.promise
        }
        storedPayload = body.payload
        savedTitles.push(String(body.payload['title'] ?? ''))
        return HttpResponse.json(
          draftRead({ id: 'draft-shared', payload: body.payload }),
        )
      }),
    )
    const sharedWrapper = createWrapper()
    const first = renderHook(
      () => useDraft({ initialDraftId: 'draft-shared' }),
      { wrapper: sharedWrapper },
    )
    await waitFor(() => expect(first.result.current.draft.title).toBe('Stored draft'))

    act(() => {
      first.result.current.setDraft((previous) => ({
        ...previous,
        title: 'Unmounted writer',
      }))
    })
    const retiredSetDraft = first.result.current.setDraft
    const retiredSaveNow = first.result.current.saveNow
    first.unmount()
    await firstPutStarted.promise

    const second = renderHook(
      () => useDraft({ initialDraftId: 'draft-shared' }),
      { wrapper: sharedWrapper },
    )
    expect(second.result.current.loading).toBe(true)

    await act(async () => {
      releaseFirstPut.resolve()
    })
    await waitFor(() => expect(second.result.current.draft.title).toBe('Unmounted writer'))

    act(() => {
      second.result.current.setDraft((previous) => ({
        ...previous,
        title: 'Remounted writer',
      }))
      retiredSetDraft((previous) => ({
        ...previous,
        title: 'Retired callback',
      }))
    })
    await act(async () => {
      await Promise.all([second.result.current.saveNow(), retiredSaveNow()])
    })

    expect(savedTitles).toEqual(['Unmounted writer', 'Remounted writer'])
    expect(second.result.current.draft.title).toBe('Remounted writer')
  })

  it('hands a deferred fresh create to a StrictMode remount without duplicating the POST', async () => {
    const createStarted = deferred<void>()
    const releaseCreate = deferred<void>()
    const createdByRetiredHook: string[] = []
    const createdByReplacement: string[] = []
    const creates: Record<string, unknown>[] = []
    const updates: Record<string, unknown>[] = []
    let storedPayload: Record<string, unknown> = {}
    server.use(
      http.post('*/v1/drafts', async ({ request }) => {
        const body = (await request.json()) as {
          title: string
          payload: Record<string, unknown>
        }
        creates.push(body)
        createStarted.resolve()
        await releaseCreate.promise
        storedPayload = body.payload
        return HttpResponse.json(
          draftRead({ id: 'draft-created', title: body.title, payload: body.payload }),
          { status: 201 },
        )
      }),
      http.put('*/v1/drafts/draft-created', async ({ request }) => {
        const body = (await request.json()) as {
          title: string
          payload: Record<string, unknown>
        }
        updates.push(body)
        storedPayload = body.payload
        return HttpResponse.json(
          draftRead({ id: 'draft-created', title: body.title, payload: body.payload }),
        )
      }),
    )
    const sharedWrapper = createWrapper({ strict: true })
    const first = renderHook(
      () =>
        useDraft({
          initialDraftId: null,
          onDraftCreated: (id) => createdByRetiredHook.push(id),
        }),
      { wrapper: sharedWrapper },
    )

    act(() => {
      first.result.current.setDraft((previous) => ({
        ...previous,
        title: 'Unmounted fresh writer',
      }))
    })
    first.unmount()
    await createStarted.promise

    const second = renderHook(
      () =>
        useDraft({
          initialDraftId: null,
          onDraftCreated: (id) => createdByReplacement.push(id),
        }),
      { wrapper: sharedWrapper },
    )
    expect(second.result.current.draft.title).toBe('Unmounted fresh writer')

    act(() => {
      second.result.current.setDraft((previous) => ({
        ...previous,
        title: 'Remounted latest writer',
      }))
    })
    let save!: Promise<void>
    act(() => {
      save = second.result.current.saveNow()
    })

    await act(async () => {
      releaseCreate.resolve()
      await save
    })
    await waitFor(() => expect(second.result.current.draftId).toBe('draft-created'))

    expect(creates).toHaveLength(1)
    expect(updates).toHaveLength(1)
    expect(storedPayload['title']).toBe('Remounted latest writer')
    expect(second.result.current.draft.title).toBe('Remounted latest writer')
    expect(second.result.current.saveState).toBe('saved')
    expect(createdByRetiredHook).toEqual([])
    expect(createdByReplacement).toEqual(['draft-created'])
  })

  it('adopts a fresh create that completed after the retired hook unmounted', async () => {
    const createStarted = deferred<void>()
    const releaseCreate = deferred<void>()
    const createdByRetiredHook: string[] = []
    const createdByReplacement: string[] = []
    let createCount = 0
    server.use(
      http.post('*/v1/drafts', async ({ request }) => {
        createCount += 1
        const body = (await request.json()) as {
          title: string
          payload: Record<string, unknown>
        }
        createStarted.resolve()
        await releaseCreate.promise
        return HttpResponse.json(
          draftRead({ id: 'draft-completed', title: body.title, payload: body.payload }),
          { status: 201 },
        )
      }),
    )
    const sharedWrapper = createWrapper()
    const first = renderHook(
      () =>
        useDraft({
          initialDraftId: null,
          onDraftCreated: (id) => createdByRetiredHook.push(id),
        }),
      { wrapper: sharedWrapper },
    )

    act(() => {
      first.result.current.setDraft((previous) => ({
        ...previous,
        title: 'Completed while away',
      }))
    })
    let retiredSave!: Promise<void>
    act(() => {
      retiredSave = first.result.current.saveNow()
    })
    await createStarted.promise
    first.unmount()
    await act(async () => {
      releaseCreate.resolve()
      await retiredSave
    })
    expect(createdByRetiredHook).toEqual([])

    const second = renderHook(
      () =>
        useDraft({
          initialDraftId: null,
          onDraftCreated: (id) => createdByReplacement.push(id),
        }),
      { wrapper: sharedWrapper },
    )

    expect(second.result.current.draft.title).toBe('Completed while away')
    await waitFor(() => expect(second.result.current.draftId).toBe('draft-completed'))
    expect(second.result.current.saveState).toBe('saved')
    expect(createCount).toBe(1)
    expect(createdByReplacement).toEqual(['draft-completed'])
  })

  it('does not restore an older persistable snapshot after the latest fresh edit becomes invalid', async () => {
    let createCount = 0
    server.use(
      http.post('*/v1/drafts', () => {
        createCount += 1
        return HttpResponse.json(
          draftRead({ id: 'draft-stale', payload: { title: 'Stale snapshot' } }),
          { status: 201 },
        )
      }),
    )
    const wrapper = createWrapper({
      authState: authenticatedState('operator', 'Scoped operator', [
        { project_id: 'proj-alpha', app_id: null },
      ]),
    })
    const first = renderHook(
      () => useDraft({ initialDraftId: null }),
      { wrapper },
    )

    act(() => {
      first.result.current.setDraft((previous) => ({
        ...previous,
        title: 'Must not return',
        scope: { ...previous.scope, project_id: 'proj-alpha' },
      }))
      first.result.current.setDraft((previous) => ({
        ...previous,
        scope: { ...previous.scope, project_id: '' },
      }))
    })
    first.unmount()

    const second = renderHook(
      () => useDraft({ initialDraftId: null }),
      { wrapper },
    )
    expect(second.result.current.draft.title).toBe('')
    expect(second.result.current.draft.scope.project_id).not.toBe('proj-alpha')
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    expect(createCount).toBe(0)
  })

  it('deletes a fresh create that completes after its latest edit becomes invalid', async () => {
    const createStarted = deferred<void>()
    const releaseCreate = deferred<void>()
    let deleteCount = 0
    server.use(
      http.post('*/v1/drafts', async ({ request }) => {
        const body = (await request.json()) as {
          title: string
          payload: Record<string, unknown>
        }
        createStarted.resolve()
        await releaseCreate.promise
        return HttpResponse.json(
          draftRead({ id: 'draft-discarded', title: body.title, payload: body.payload }),
          { status: 201 },
        )
      }),
      http.delete('*/v1/drafts/draft-discarded', () => {
        deleteCount += 1
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const wrapper = createWrapper({
      authState: authenticatedState('operator', 'Scoped operator', [
        { project_id: 'proj-alpha', app_id: null },
      ]),
    })
    const first = renderHook(
      () => useDraft({ initialDraftId: null }),
      { wrapper },
    )
    act(() => {
      first.result.current.setDraft((previous) => ({
        ...previous,
        title: 'Discard in flight',
        scope: { ...previous.scope, project_id: 'proj-alpha' },
      }))
    })
    let save!: Promise<void>
    act(() => {
      save = first.result.current.saveNow()
    })
    await createStarted.promise

    act(() => {
      first.result.current.setDraft((previous) => ({
        ...previous,
        scope: { ...previous.scope, project_id: '' },
      }))
    })
    first.unmount()
    await act(async () => {
      releaseCreate.resolve()
      await save
    })

    expect(deleteCount).toBe(1)
    const second = renderHook(
      () => useDraft({ initialDraftId: null }),
      { wrapper },
    )
    expect(second.result.current.draft.title).toBe('')
    expect(second.result.current.draft.scope.project_id).not.toBe('proj-alpha')
  })

  it('does not delete a draft after a queued save crosses a session transition', async () => {
    const saveStarted = deferred<void>()
    const releaseSave = deferred<void>()
    let deleteCount = 0
    server.use(
      http.get('*/v1/drafts/draft-a', () =>
        HttpResponse.json(
          draftRead({ id: 'draft-a', payload: { title: 'Principal A draft' } }),
        ),
      ),
      http.put('*/v1/drafts/draft-a', async ({ request }) => {
        const body = (await request.json()) as { payload: Record<string, unknown> }
        saveStarted.resolve()
        await releaseSave.promise
        return HttpResponse.json(draftRead({ id: 'draft-a', payload: body.payload }))
      }),
      http.delete('*/v1/drafts/draft-a', () => {
        deleteCount += 1
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { result } = renderHook(
      () => useDraft({ initialDraftId: 'draft-a' }),
      { wrapper: createWrapper() },
    )
    await waitFor(() => expect(result.current.draft.title).toBe('Principal A draft'))

    act(() => {
      result.current.setDraft((previous) => ({ ...previous, title: 'Queued save' }))
    })
    let save!: Promise<void>
    act(() => {
      save = result.current.saveNow()
    })
    await saveStarted.promise

    let remove!: Promise<void>
    act(() => {
      remove = result.current.deleteDraftById('draft-a')
    })
    bumpSessionRevision()
    await act(async () => {
      releaseSave.resolve()
      await Promise.all([save, remove])
    })

    expect(deleteCount).toBe(0)
  })
})
