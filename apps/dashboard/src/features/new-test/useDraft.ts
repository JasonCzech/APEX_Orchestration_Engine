/**
 * useDraft — server-side draft persistence for the wizard (plan UX 2.c).
 *
 * Mechanics:
 * - The WizardDraft lives in local state; every change schedules a debounced
 *   autosave (1.5s after the LAST change).
 * - First save creates the draft (POST /v1/drafts, title fallback "Untitled
 *   run"); later saves PUT the full payload verbatim. A saving-in-flight
 *   guard serializes create-then-update so a slow create can never race a
 *   second create or an update without an id.
 * - `?draft=<id>` (initialDraftId) loads once on mount; `loadDraft` powers the
 *   "Resume draft" entry point. `deleteCurrentDraft` is the best-effort
 *   delete-on-launch.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'

import {
  createDraftRequest,
  deleteDraftRequest,
  getDraftRequest,
  updateDraftRequest,
} from '@/api/hooks/useDrafts'
import type { ConsumerInfo } from '@/api/apexClient'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import { hasFullProjectScope, roleAtLeast } from '@/auth/RequireRole'

import { emptyDraft, parseDraftPayload, type WizardDraft } from './wizardState'

export const DRAFT_AUTOSAVE_DEBOUNCE_MS = 1_500
export const UNTITLED_DRAFT_TITLE = 'Untitled run'

export type DraftSaveState = 'idle' | 'pending' | 'saving' | 'saved' | 'error'

export type DraftUpdater = WizardDraft | ((previous: WizardDraft) => WizardDraft)

const draftWriteQueues = new WeakMap<QueryClient, Map<string, Promise<void>>>()
type CreatedDraft = Awaited<ReturnType<typeof createDraftRequest>>

interface FreshDraftHandoff {
  snapshot: WizardDraft
  revision: number
  persistedRevision: number
  created: CreatedDraft | null
  persistence: Promise<CreatedDraft> | null
  discarded: boolean
  discardCleanup: Promise<void> | null
}

const freshDraftHandoffs = new WeakMap<QueryClient, Map<string, FreshDraftHandoff>>()

function draftWriteQueueKey(
  draftId: string,
  keyRevision: number,
  sessionRevision: number,
): string {
  return JSON.stringify([keyRevision, sessionRevision, draftId])
}

function queuesFor(queryClient: QueryClient): Map<string, Promise<void>> {
  const existing = draftWriteQueues.get(queryClient)
  if (existing) return existing
  const created = new Map<string, Promise<void>>()
  draftWriteQueues.set(queryClient, created)
  return created
}

function freshDraftHandoffKey(keyRevision: number, sessionRevision: number): string {
  return JSON.stringify([keyRevision, sessionRevision])
}

function freshHandoffsFor(queryClient: QueryClient): Map<string, FreshDraftHandoff> {
  const existing = freshDraftHandoffs.get(queryClient)
  if (existing) return existing
  const created = new Map<string, FreshDraftHandoff>()
  freshDraftHandoffs.set(queryClient, created)
  return created
}

function getFreshDraftHandoff(
  queryClient: QueryClient,
  keyRevision: number,
  sessionRevision: number,
): FreshDraftHandoff | undefined {
  return freshHandoffsFor(queryClient).get(
    freshDraftHandoffKey(keyRevision, sessionRevision),
  )
}

function publishFreshDraftSnapshot(
  queryClient: QueryClient,
  keyRevision: number,
  sessionRevision: number,
  snapshot: WizardDraft,
): FreshDraftHandoff {
  const handoffs = freshHandoffsFor(queryClient)
  const key = freshDraftHandoffKey(keyRevision, sessionRevision)
  const existing = handoffs.get(key)
  if (existing) {
    existing.snapshot = snapshot
    existing.revision += 1
    return existing
  }
  const created: FreshDraftHandoff = {
    snapshot,
    revision: 1,
    persistedRevision: 0,
    created: null,
    persistence: null,
    discarded: false,
    discardCleanup: null,
  }
  handoffs.set(key, created)
  return created
}

function retireFreshDraftHandoff(
  queryClient: QueryClient,
  keyRevision: number,
  sessionRevision: number,
  draftId?: string,
): void {
  const handoffs = freshHandoffsFor(queryClient)
  const key = freshDraftHandoffKey(keyRevision, sessionRevision)
  const handoff = handoffs.get(key)
  if (!handoff) return
  if (draftId !== undefined && handoff.created?.id !== draftId) return
  handoffs.delete(key)
}

function draftWriteBody(snapshot: WizardDraft) {
  return {
    title: snapshot.title.trim() || UNTITLED_DRAFT_TITLE,
    project_id: snapshot.scope.project_id.trim() || null,
    payload: snapshot as unknown as Record<string, unknown>,
  }
}

function enqueueDraftWrite(
  queryClient: QueryClient,
  draftId: string,
  keyRevision: number,
  sessionRevision: number,
  write: () => Promise<void>,
): Promise<void> {
  const queues = queuesFor(queryClient)
  const key = draftWriteQueueKey(draftId, keyRevision, sessionRevision)
  const previous = queues.get(key)
  const queued = previous ? previous.catch(() => undefined).then(write) : write()
  const stable = queued.catch(() => undefined)
  queues.set(key, stable)
  void stable.then(() => {
    if (queues.get(key) === stable) queues.delete(key)
  })
  return queued
}

function cleanupDiscardedFreshDraft(
  queryClient: QueryClient,
  handoff: FreshDraftHandoff,
  keyRevision: number,
  sessionRevision: number,
): Promise<void> {
  if (!handoff.created) return Promise.resolve()
  if (handoff.discardCleanup) return handoff.discardCleanup
  const draftId = handoff.created.id
  const cleanup = enqueueDraftWrite(
    queryClient,
    draftId,
    keyRevision,
    sessionRevision,
    async () => {
      if (
        keyRevision !== getApiKeyRevision() ||
        sessionRevision !== getSessionRevision()
      ) {
        return
      }
      await deleteDraftRequest(draftId)
      void queryClient.invalidateQueries({ queryKey: ['drafts', 'list'] })
    },
  ).catch(() => undefined)
  handoff.discardCleanup = cleanup
  return cleanup
}

function discardFreshDraftHandoff(
  queryClient: QueryClient,
  keyRevision: number,
  sessionRevision: number,
): void {
  const handoffs = freshHandoffsFor(queryClient)
  const key = freshDraftHandoffKey(keyRevision, sessionRevision)
  const handoff = handoffs.get(key)
  if (!handoff) return
  handoff.discarded = true
  handoffs.delete(key)
  if (handoff.created) {
    void cleanupDiscardedFreshDraft(
      queryClient,
      handoff,
      keyRevision,
      sessionRevision,
    )
  }
}

async function persistFreshDraftHandoff(
  queryClient: QueryClient,
  handoff: FreshDraftHandoff,
  keyRevision: number,
  sessionRevision: number,
): Promise<CreatedDraft> {
  for (;;) {
    let persistence = handoff.persistence
    if (!persistence) {
      persistence = (async () => {
        let created = handoff.created
        if (!created) {
          const createSnapshot = handoff.snapshot
          const createRevision = handoff.revision
          created = await createDraftRequest(draftWriteBody(createSnapshot))
          if (
            keyRevision !== getApiKeyRevision() ||
            sessionRevision !== getSessionRevision()
          ) {
            throw new Error('Draft persistence was superseded by a session change')
          }
          handoff.created = created
          if (handoff.discarded) {
            await cleanupDiscardedFreshDraft(
              queryClient,
              handoff,
              keyRevision,
              sessionRevision,
            )
            throw new Error('Draft persistence was discarded')
          }
          handoff.persistedRevision = createRevision
        }

        while (handoff.persistedRevision < handoff.revision) {
          const updateSnapshot = handoff.snapshot
          const updateRevision = handoff.revision
          await enqueueDraftWrite(
            queryClient,
            created.id,
            keyRevision,
            sessionRevision,
            async () => {
              await updateDraftRequest(created.id, draftWriteBody(updateSnapshot))
            },
          )
          if (
            keyRevision !== getApiKeyRevision() ||
            sessionRevision !== getSessionRevision()
          ) {
            throw new Error('Draft persistence was superseded by a session change')
          }
          if (handoff.discarded) {
            await cleanupDiscardedFreshDraft(
              queryClient,
              handoff,
              keyRevision,
              sessionRevision,
            )
            throw new Error('Draft persistence was discarded')
          }
          handoff.persistedRevision = updateRevision
        }
        if (handoff.discarded) {
          await cleanupDiscardedFreshDraft(
            queryClient,
            handoff,
            keyRevision,
            sessionRevision,
          )
          throw new Error('Draft persistence was discarded')
        }
        return created
      })()
      handoff.persistence = persistence
      void persistence.then(
        () => {
          if (handoff.persistence === persistence) handoff.persistence = null
        },
        () => {
          if (handoff.persistence === persistence) handoff.persistence = null
        },
      )
    }

    const created = await persistence
    if (handoff.persistedRevision >= handoff.revision) return created
  }
}

async function waitForDraftWrites(
  queryClient: QueryClient,
  draftId: string,
  keyRevision: number,
  sessionRevision: number,
): Promise<void> {
  const queues = queuesFor(queryClient)
  const key = draftWriteQueueKey(draftId, keyRevision, sessionRevision)
  for (;;) {
    const pending = queues.get(key)
    if (!pending) return
    await pending
    // A prior hook can append its final unmount flush as the preceding task
    // settles. Loop until the shared record queue is actually empty.
  }
}

export interface DraftLoadFailure {
  requestedDraftId: string | null
  urlDraftId: string | null
  message: string
}

export function canPersistWizardDraft(
  consumer: ConsumerInfo | null | undefined,
  draft: WizardDraft,
): boolean {
  const project = draft.scope.project_id.trim()
  return (
    consumer === undefined ||
    (consumer !== null &&
      roleAtLeast(consumer.role, 'operator') &&
      project.length > 0 &&
      hasFullProjectScope(consumer, project))
  )
}

export interface UseDraftResult {
  draft: WizardDraft
  /** Update the draft and schedule an autosave. */
  setDraft: (updater: DraftUpdater) => void
  /** Server id once created/loaded; null while the draft is purely local. */
  draftId: string | null
  /** Stable logical-record generation; server creation does not rotate it. */
  draftGeneration: number
  isDraftGenerationCurrent: (generation: number) => boolean
  saveState: DraftSaveState
  /** True while any stored draft is being fetched. */
  loading: boolean
  loadFailure: DraftLoadFailure | null
  isLoadFailureCurrent: (failure: DraftLoadFailure) => boolean
  /** Replace local state with a stored draft (Resume draft picker). */
  loadDraft: (id: string) => Promise<boolean>
  /** Flush any pending change immediately (Save draft button). */
  saveNow: () => Promise<void>
  /** Best-effort delete-on-launch; never throws. */
  deleteCurrentDraft: () => Promise<void>
  /** Delete one captured draft without consulting the currently loaded record. */
  deleteDraftById: (id: string) => Promise<void>
}

export function useDraft({
  initialDraftId,
  onDraftCreated,
  canSwitchDraft,
}: {
  initialDraftId: string | null
  /** Called with the new id after the autosave create lands (URL sync). */
  onDraftCreated?: (id: string) => void
  /** Prevent replacing the logical draft while child operations still target it. */
  canSwitchDraft?: () => boolean
}): UseDraftResult {
  const queryClient = useQueryClient()
  const consumer = useOptionalConsumer()
  const mountAuthRevision = getApiKeyRevision()
  const mountSessionRevision = getSessionRevision()
  const initialFreshHandoff =
    initialDraftId === null
      ? getFreshDraftHandoff(queryClient, mountAuthRevision, mountSessionRevision)
      : undefined
  const initialDraft = initialFreshHandoff?.snapshot ?? emptyDraft()
  const initialFreshNeedsPersistence =
    initialFreshHandoff !== undefined &&
    initialFreshHandoff.persistedRevision < initialFreshHandoff.revision
  const [draft, setDraftState] = useState<WizardDraft>(initialDraft)
  const [draftId, setDraftId] = useState<string | null>(initialDraftId)
  const [draftGeneration, setDraftGeneration] = useState(0)
  const [saveState, setSaveState] = useState<DraftSaveState>(
    initialFreshHandoff
      ? initialFreshHandoff.persistence
        ? 'saving'
        : initialFreshNeedsPersistence
          ? 'pending'
          : 'saved'
      : 'idle',
  )
  const [loading, setLoading] = useState<boolean>(initialDraftId !== null)
  const [loadFailure, setLoadFailure] = useState<DraftLoadFailure | null>(null)

  const draftRef = useRef(initialDraft)
  const idRef = useRef<string | null>(initialDraftId)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const dirtyRef = useRef(initialFreshNeedsPersistence)
  const freshRevisionRef = useRef(initialFreshHandoff?.revision ?? 0)
  const saveQueueRef = useRef<Promise<void> | null>(null)
  const loadingRef = useRef(initialDraftId !== null)
  const operationGenerationRef = useRef(0)
  const loadRequestRef = useRef(0)
  const draftGenerationRef = useRef(0)
  const suppressCreatedUrlSyncRef = useRef(initialDraftId !== null)
  const loadFailureRef = useRef<DraftLoadFailure | null>(null)
  const mountedRef = useRef(true)
  const authRevisionRef = useRef(mountAuthRevision)
  const sessionRevisionRef = useRef(mountSessionRevision)
  const consumerRef = useRef(consumer)
  consumerRef.current = consumer
  const persistenceAllowedRef = useRef(canPersistWizardDraft(consumer, draft))
  const onCreatedRef = useRef(onDraftCreated)
  onCreatedRef.current = onDraftCreated
  const canSwitchDraftRef = useRef(canSwitchDraft)
  canSwitchDraftRef.current = canSwitchDraft

  const advanceDraftGeneration = useCallback(() => {
    const next = draftGenerationRef.current + 1
    draftGenerationRef.current = next
    if (mountedRef.current) setDraftGeneration(next)
  }, [])

  const isDraftGenerationCurrent = useCallback(
    (generation: number) => mountedRef.current && draftGenerationRef.current === generation,
    [],
  )

  const clearLoadFailure = useCallback(() => {
    loadFailureRef.current = null
    if (mountedRef.current) setLoadFailure(null)
  }, [])

  const publishLoadFailure = useCallback((failure: DraftLoadFailure) => {
    loadFailureRef.current = failure
    if (mountedRef.current) setLoadFailure(failure)
  }, [])

  const isLoadFailureCurrent = useCallback(
    (failure: DraftLoadFailure) => loadFailureRef.current === failure,
    [],
  )

  const flush = useCallback(async () => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    const saveLatest = async () => {
      const operationGeneration = operationGenerationRef.current
      if (
        authRevisionRef.current !== getApiKeyRevision() ||
        sessionRevisionRef.current !== getSessionRevision()
      ) return
      // A queued save ahead of us may already have persisted the same state.
      if (!dirtyRef.current) return
      const snapshot = draftRef.current
      const canPersist = canPersistWizardDraft(consumerRef.current, snapshot)
      persistenceAllowedRef.current = canPersist
      if (!canPersist) {
        dirtyRef.current = false
        if (mountedRef.current) setSaveState('idle')
        return
      }
      dirtyRef.current = false
      if (mountedRef.current) setSaveState('saving')
      try {
        const body = draftWriteBody(snapshot)
        if (
          operationGeneration !== operationGenerationRef.current ||
          authRevisionRef.current !== getApiKeyRevision() ||
          sessionRevisionRef.current !== getSessionRevision()
        ) {
          return
        }
        if (idRef.current === null) {
          const handoff =
            getFreshDraftHandoff(
              queryClient,
              authRevisionRef.current,
              sessionRevisionRef.current,
            ) ??
            publishFreshDraftSnapshot(
              queryClient,
              authRevisionRef.current,
              sessionRevisionRef.current,
              snapshot,
            )
          const created = await persistFreshDraftHandoff(
            queryClient,
            handoff,
            authRevisionRef.current,
            sessionRevisionRef.current,
          )
          if (
            operationGeneration !== operationGenerationRef.current ||
            authRevisionRef.current !== getApiKeyRevision() ||
            sessionRevisionRef.current !== getSessionRevision()
          ) return
          idRef.current = created.id
          dirtyRef.current = handoff.persistedRevision < freshRevisionRef.current
          if (mountedRef.current) {
            setDraftId(created.id)
            if (!suppressCreatedUrlSyncRef.current) {
              onCreatedRef.current?.(created.id)
            }
          }
          void queryClient.invalidateQueries({ queryKey: ['drafts', 'list'] })
        } else {
          await updateDraftRequest(idRef.current, body)
          if (
            operationGeneration !== operationGenerationRef.current ||
            authRevisionRef.current !== getApiKeyRevision() ||
            sessionRevisionRef.current !== getSessionRevision()
          ) return
          void queryClient.invalidateQueries({ queryKey: ['drafts', 'list'] })
        }
        if (mountedRef.current) {
          setSaveState(
            !persistenceAllowedRef.current
              ? 'idle'
              : dirtyRef.current
                ? 'pending'
                : 'saved',
          )
        }
      } catch {
        if (
          operationGeneration !== operationGenerationRef.current ||
          authRevisionRef.current !== getApiKeyRevision() ||
          sessionRevisionRef.current !== getSessionRevision()
        ) return
        if (persistenceAllowedRef.current) {
          // Keep the dirty bit so Save Draft (or a later edit) can retry.
          dirtyRef.current = true
          if (mountedRef.current) setSaveState('error')
        } else {
          dirtyRef.current = false
          if (mountedRef.current) setSaveState('idle')
        }
      }
    }
    const targetId = idRef.current
    // Existing records share one queue across hook lifetimes. Register the
    // task immediately so a replacement wizard's initial load cannot slip
    // between an active save and this instance's final unmount flush.
    const queued =
      targetId === null
        ? saveQueueRef.current
          ? saveQueueRef.current.then(saveLatest)
          : saveLatest()
        : enqueueDraftWrite(
            queryClient,
            targetId,
            authRevisionRef.current,
            sessionRevisionRef.current,
            saveLatest,
          )
    // Keep the queue usable even if an unexpected exception escapes the task.
    const stable = queued.catch(() => undefined)
    saveQueueRef.current = stable
    await queued
    if (saveQueueRef.current === stable) saveQueueRef.current = null
  }, [queryClient])

  const scheduleSave = useCallback(() => {
    if (timerRef.current !== null) clearTimeout(timerRef.current)
    setSaveState('pending')
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      void flush()
    }, DRAFT_AUTOSAVE_DEBOUNCE_MS)
  }, [flush])

  const setDraft = useCallback(
    (updater: DraftUpdater) => {
      if (!mountedRef.current) return
      // A resume operation is authoritative. The wizard is disabled while it
      // loads, and this also prevents programmatic updates from being saved
      // against the draft that is about to be replaced.
      if (loadingRef.current) return
      const next = typeof updater === 'function' ? updater(draftRef.current) : updater
      draftRef.current = next
      setDraftState(next)
      const canPersist = canPersistWizardDraft(consumerRef.current, next)
      persistenceAllowedRef.current = canPersist
      dirtyRef.current = canPersist
      if (canPersist) {
        if (idRef.current === null) {
          const handoff = publishFreshDraftSnapshot(
            queryClient,
            authRevisionRef.current,
            sessionRevisionRef.current,
            next,
          )
          freshRevisionRef.current = handoff.revision
        }
        scheduleSave()
      } else {
        if (timerRef.current !== null) {
          clearTimeout(timerRef.current)
          timerRef.current = null
        }
        if (idRef.current === null) {
          operationGenerationRef.current += 1
          freshRevisionRef.current = 0
          discardFreshDraftHandoff(
            queryClient,
            authRevisionRef.current,
            sessionRevisionRef.current,
          )
        }
        setSaveState('idle')
      }
    },
    [queryClient, scheduleSave],
  )

  const loadDraft = useCallback(
    async (id: string): Promise<boolean> => {
      if (canSwitchDraftRef.current?.() === false) {
        publishLoadFailure({
          requestedDraftId: id,
          urlDraftId: initialDraftId,
          message: 'Wait for in-progress wizard operations before switching drafts.',
        })
        return false
      }
      advanceDraftGeneration()
      const loadRequest = ++loadRequestRef.current
      const urlDraftId = initialDraftId
      suppressCreatedUrlSyncRef.current = true
      loadingRef.current = true
      clearLoadFailure()
      if (mountedRef.current) {
        setLoading(true)
      }

      // Preserve pending local work before switching records. flush() also
      // serializes behind any create/update already in flight.
      await flush()
      if (
        loadRequest !== loadRequestRef.current ||
        dirtyRef.current ||
        authRevisionRef.current !== getApiKeyRevision() ||
        sessionRevisionRef.current !== getSessionRevision()
      ) {
        if (loadRequest === loadRequestRef.current) {
          loadingRef.current = false
          if (mountedRef.current) {
            setLoading(false)
            if (dirtyRef.current) {
              publishLoadFailure({
                requestedDraftId: id,
                urlDraftId,
                message: 'Current draft could not be saved; staying on it.',
              })
            }
          }
          suppressCreatedUrlSyncRef.current = false
        }
        return false
      }

      const operationGeneration = ++operationGenerationRef.current
      const previousId = idRef.current
      try {
        await waitForDraftWrites(
          queryClient,
          id,
          authRevisionRef.current,
          sessionRevisionRef.current,
        )
        if (
          loadRequest !== loadRequestRef.current ||
          operationGeneration !== operationGenerationRef.current ||
          authRevisionRef.current !== getApiKeyRevision() ||
          sessionRevisionRef.current !== getSessionRevision()
        ) return false
        const stored = await getDraftRequest(id)
        if (
          loadRequest !== loadRequestRef.current ||
          operationGeneration !== operationGenerationRef.current ||
          authRevisionRef.current !== getApiKeyRevision() ||
          sessionRevisionRef.current !== getSessionRevision()
        ) return false

        const parsed = parseDraftPayload(stored.payload)
        draftRef.current = parsed
        dirtyRef.current = false
        persistenceAllowedRef.current = canPersistWizardDraft(consumerRef.current, parsed)
        if (previousId !== null && previousId !== stored.id) {
          retireFreshDraftHandoff(
            queryClient,
            authRevisionRef.current,
            sessionRevisionRef.current,
            previousId,
          )
        }
        idRef.current = stored.id
        if (mountedRef.current) {
          setDraftState(parsed)
          setDraftId(stored.id)
          setSaveState('saved')
          clearLoadFailure()
        }
        return true
      } catch (error) {
        if (
          loadRequest !== loadRequestRef.current ||
          operationGeneration !== operationGenerationRef.current ||
          authRevisionRef.current !== getApiKeyRevision() ||
          sessionRevisionRef.current !== getSessionRevision()
        ) return false

        // A stale/deleted URL id must fall back to a local draft so autosave can
        // create a new record instead of retrying PUT forever. When switching
        // away from another valid draft, preserve that draft and its id.
        if (previousId === id) {
          idRef.current = null
          dirtyRef.current = false
          if (mountedRef.current) setDraftId(null)
        }
        publishLoadFailure({
          requestedDraftId: id,
          urlDraftId,
          message: error instanceof Error ? error.message : 'Draft could not be loaded',
        })
        return false
      } finally {
        if (loadRequest === loadRequestRef.current) {
          suppressCreatedUrlSyncRef.current = false
          loadingRef.current = false
          if (mountedRef.current) setLoading(false)
        }
      }
    },
    [
      advanceDraftGeneration,
      clearLoadFailure,
      flush,
      initialDraftId,
      publishLoadFailure,
      queryClient,
    ],
  )

  const startFresh = useCallback(async (): Promise<boolean> => {
    if (canSwitchDraftRef.current?.() === false) {
      publishLoadFailure({
        requestedDraftId: null,
        urlDraftId: initialDraftId,
        message: 'Wait for in-progress wizard operations before switching drafts.',
      })
      return false
    }
    advanceDraftGeneration()
    const loadRequest = ++loadRequestRef.current
    suppressCreatedUrlSyncRef.current = true
    loadingRef.current = true
    clearLoadFailure()
    if (mountedRef.current) {
      setLoading(true)
    }
    await flush()
    if (
      loadRequest !== loadRequestRef.current ||
      dirtyRef.current ||
      authRevisionRef.current !== getApiKeyRevision() ||
      sessionRevisionRef.current !== getSessionRevision()
    ) {
      if (loadRequest === loadRequestRef.current) {
        loadingRef.current = false
        if (mountedRef.current) {
          setLoading(false)
          if (dirtyRef.current) {
            publishLoadFailure({
              requestedDraftId: null,
              urlDraftId: initialDraftId,
              message: 'Current draft could not be saved; staying on it.',
            })
          }
        }
        suppressCreatedUrlSyncRef.current = false
      }
      return false
    }

    operationGenerationRef.current += 1
    retireFreshDraftHandoff(
      queryClient,
      authRevisionRef.current,
      sessionRevisionRef.current,
      idRef.current ?? undefined,
    )
    const fresh = emptyDraft()
    draftRef.current = fresh
    dirtyRef.current = false
    freshRevisionRef.current = 0
    persistenceAllowedRef.current = canPersistWizardDraft(consumerRef.current, fresh)
    idRef.current = null
    loadingRef.current = false
    if (mountedRef.current) {
      setDraftState(fresh)
      setDraftId(null)
      setSaveState('idle')
      setLoading(false)
      clearLoadFailure()
    }
    suppressCreatedUrlSyncRef.current = false
    return true
  }, [
    advanceDraftGeneration,
    clearLoadFailure,
    flush,
    initialDraftId,
    publishLoadFailure,
    queryClient,
  ])

  // Keep local identity aligned with the URL. A create updates idRef before
  // onDraftCreated writes ?draft=, so that internal null -> id transition is a
  // no-op here and edits queued during the create stay in the same hook.
  const observedUrlDraftIdRef = useRef<string | null | undefined>(undefined)
  useEffect(() => {
    const previousUrlDraftId = observedUrlDraftIdRef.current
    const firstObservation = previousUrlDraftId === undefined
    if (!firstObservation && previousUrlDraftId === initialDraftId) return
    observedUrlDraftIdRef.current = initialDraftId
    if (firstObservation) {
      if (initialDraftId !== null) void loadDraft(initialDraftId)
      return
    }
    if (initialDraftId === idRef.current) {
      if (initialDraftId !== null) {
        retireFreshDraftHandoff(
          queryClient,
          authRevisionRef.current,
          sessionRevisionRef.current,
          initialDraftId,
        )
      }
      if (loadingRef.current) {
        loadRequestRef.current += 1
        advanceDraftGeneration()
        suppressCreatedUrlSyncRef.current = false
        loadingRef.current = false
        clearLoadFailure()
        if (mountedRef.current) {
          setLoading(false)
        }
      }
      return
    }
    if (initialDraftId === null) void startFresh()
    else void loadDraft(initialDraftId)
  }, [
    advanceDraftGeneration,
    clearLoadFailure,
    initialDraftId,
    loadDraft,
    queryClient,
    startFresh,
  ])

  // A fresh route remount adopts the session-owned logical record immediately.
  // Calling flush joins (or starts) its one shared persistence attempt, so
  // StrictMode and route lifetimes cannot mint parallel POSTs.
  useEffect(() => {
    if (initialDraftId !== null || idRef.current !== null) return
    const handoff = getFreshDraftHandoff(
      queryClient,
      authRevisionRef.current,
      sessionRevisionRef.current,
    )
    if (!handoff) return
    // Even a fully persisted handoff still needs one local adoption pass to
    // copy its server id into this hook and synchronize the replacement URL.
    dirtyRef.current = true
    void flush()
  }, [flush, initialDraftId, queryClient])

  // Flush a pending debounce on unmount. The request is deliberately allowed
  // to settle after React unmounts, but UI callbacks are suppressed.
  useEffect(() => {
    const mountRevision = authRevisionRef.current
    const mountSessionRevision = sessionRevisionRef.current
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current)
        timerRef.current = null
        if (
          mountRevision === getApiKeyRevision() &&
          mountSessionRevision === getSessionRevision()
        ) void flush()
      }
    }
  }, [flush])

  const saveNow = useCallback(async () => {
    if (!mountedRef.current || loadingRef.current) return
    await flush()
  }, [flush])

  const deleteDraftById = useCallback(
    async (id: string) => {
      const queued = saveQueueRef.current
      if (queued) await queued
      // The hook belongs to the principal/session that mounted it. A launch
      // response or queued save may settle after sign-out; never issue cleanup
      // with credentials from the replacement session.
      if (
        authRevisionRef.current !== getApiKeyRevision() ||
        sessionRevisionRef.current !== getSessionRevision()
      ) {
        return
      }
      try {
        await enqueueDraftWrite(
          queryClient,
          id,
          authRevisionRef.current,
          sessionRevisionRef.current,
          () => deleteDraftRequest(id),
        )
        if (
          authRevisionRef.current !== getApiKeyRevision() ||
          sessionRevisionRef.current !== getSessionRevision()
        ) {
          return
        }
        if (idRef.current === id) {
          idRef.current = null
          dirtyRef.current = false
          freshRevisionRef.current = 0
          if (mountedRef.current) setDraftId(null)
        }
        retireFreshDraftHandoff(
          queryClient,
          authRevisionRef.current,
          sessionRevisionRef.current,
          id,
        )
        void queryClient.invalidateQueries({ queryKey: ['drafts', 'list'] })
      } catch {
        // Best-effort: an orphaned draft is harmless; the run is already launched.
      }
    },
    [queryClient],
  )

  const deleteCurrentDraft = useCallback(async () => {
    // Wait behind any create/update before reading idRef. This prevents a slow
    // first autosave from creating an orphan after a successful launch.
    await flush()
    const id = idRef.current
    if (id === null) return
    await deleteDraftById(id)
  }, [deleteDraftById, flush])

  return {
    draft,
    setDraft,
    draftId,
    draftGeneration,
    isDraftGenerationCurrent,
    saveState,
    loading,
    loadFailure,
    isLoadFailureCurrent,
    loadDraft,
    saveNow,
    deleteCurrentDraft,
    deleteDraftById,
  }
}
