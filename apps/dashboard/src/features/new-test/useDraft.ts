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
import { useQueryClient } from '@tanstack/react-query'

import {
  createDraftRequest,
  deleteDraftRequest,
  getDraftRequest,
  updateDraftRequest,
} from '@/api/hooks/useDrafts'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import { hasFullProjectScope, roleAtLeast } from '@/auth/RequireRole'

import { emptyDraft, parseDraftPayload, type WizardDraft } from './wizardState'

export const DRAFT_AUTOSAVE_DEBOUNCE_MS = 1_500
export const UNTITLED_DRAFT_TITLE = 'Untitled run'

export type DraftSaveState = 'idle' | 'pending' | 'saving' | 'saved' | 'error'

export type DraftUpdater = WizardDraft | ((previous: WizardDraft) => WizardDraft)

export interface UseDraftResult {
  draft: WizardDraft
  /** Update the draft and schedule an autosave. */
  setDraft: (updater: DraftUpdater) => void
  /** Server id once created/loaded; null while the draft is purely local. */
  draftId: string | null
  saveState: DraftSaveState
  /** True while the initial ?draft= payload is being fetched. */
  loading: boolean
  loadError: string | null
  /** Replace local state with a stored draft (Resume draft picker). */
  loadDraft: (id: string) => Promise<void>
  /** Flush any pending change immediately (Save draft button). */
  saveNow: () => Promise<void>
  /** Best-effort delete-on-launch; never throws. */
  deleteCurrentDraft: () => Promise<void>
}

export function useDraft({
  initialDraftId,
  onDraftCreated,
}: {
  initialDraftId: string | null
  /** Called with the new id after the autosave create lands (URL sync). */
  onDraftCreated?: (id: string) => void
}): UseDraftResult {
  const queryClient = useQueryClient()
  const consumer = useOptionalConsumer()
  const [draft, setDraftState] = useState<WizardDraft>(emptyDraft)
  const [draftId, setDraftId] = useState<string | null>(initialDraftId)
  const [saveState, setSaveState] = useState<DraftSaveState>('idle')
  const [loading, setLoading] = useState<boolean>(initialDraftId !== null)
  const [loadError, setLoadError] = useState<string | null>(null)

  const draftRef = useRef(draft)
  const idRef = useRef<string | null>(initialDraftId)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const dirtyRef = useRef(false)
  const saveQueueRef = useRef<Promise<void> | null>(null)
  const mountedRef = useRef(true)
  const authRevisionRef = useRef(getApiKeyRevision())
  const sessionRevisionRef = useRef(getSessionRevision())
  const consumerRef = useRef(consumer)
  consumerRef.current = consumer
  const onCreatedRef = useRef(onDraftCreated)
  onCreatedRef.current = onDraftCreated

  const flush = useCallback(async () => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    const saveLatest = async () => {
      if (
        authRevisionRef.current !== getApiKeyRevision() ||
        sessionRevisionRef.current !== getSessionRevision()
      ) return
      // A queued save ahead of us may already have persisted the same state.
      if (!dirtyRef.current) return
      const snapshot = draftRef.current
      const project = snapshot.scope.project_id.trim()
      const currentConsumer = consumerRef.current
      const canPersist =
        currentConsumer === undefined ||
        (currentConsumer !== null &&
          roleAtLeast(currentConsumer.role, 'operator') &&
          project.length > 0 &&
          hasFullProjectScope(currentConsumer, project))
      if (!canPersist) {
        dirtyRef.current = false
        if (mountedRef.current) setSaveState('idle')
        return
      }
      dirtyRef.current = false
      if (mountedRef.current) setSaveState('saving')
      try {
        const body = {
          title: snapshot.title.trim() || UNTITLED_DRAFT_TITLE,
          project_id: snapshot.scope.project_id.trim() || null,
          payload: snapshot as unknown as Record<string, unknown>,
        }
        if (
          authRevisionRef.current !== getApiKeyRevision() ||
          sessionRevisionRef.current !== getSessionRevision()
        ) {
          dirtyRef.current = true
          return
        }
        if (idRef.current === null) {
          const created = await createDraftRequest(body)
          idRef.current = created.id
          if (mountedRef.current) {
            setDraftId(created.id)
            onCreatedRef.current?.(created.id)
          }
          void queryClient.invalidateQueries({ queryKey: ['drafts', 'list'] })
        } else {
          await updateDraftRequest(idRef.current, body)
          void queryClient.invalidateQueries({ queryKey: ['drafts', 'list'] })
        }
        if (mountedRef.current) setSaveState('saved')
      } catch {
        // Keep the dirty bit so Save Draft (or a later edit) can retry.
        dirtyRef.current = true
        if (mountedRef.current) setSaveState('error')
      }
    }
    // Start immediately when idle (important during page unmount); otherwise
    // serialize behind the active create/update.
    const queued = saveQueueRef.current ? saveQueueRef.current.then(saveLatest) : saveLatest()
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
      setDraftState((previous) => {
        const next = typeof updater === 'function' ? updater(previous) : updater
        draftRef.current = next
        dirtyRef.current = true
        return next
      })
      scheduleSave()
    },
    [scheduleSave],
  )

  const loadDraft = useCallback(async (id: string) => {
    setLoading(true)
    setLoadError(null)
    try {
      const stored = await getDraftRequest(id)
      const parsed = parseDraftPayload(stored.payload)
      draftRef.current = parsed
      dirtyRef.current = false
      idRef.current = stored.id
      setDraftState(parsed)
      setDraftId(stored.id)
      setSaveState('saved')
    } catch (error) {
      // A stale/deleted URL id must fall back to a local draft so autosave can
      // create a new record instead of retrying PUT forever.
      idRef.current = null
      dirtyRef.current = false
      setDraftId(null)
      setLoadError(error instanceof Error ? error.message : 'Draft could not be loaded')
    } finally {
      setLoading(false)
    }
  }, [])

  // Load the URL's draft exactly once on mount (later id changes come from us).
  const initialIdRef = useRef(initialDraftId)
  useEffect(() => {
    if (initialIdRef.current !== null) void loadDraft(initialIdRef.current)
  }, [loadDraft])

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
    await flush()
  }, [flush])

  const deleteCurrentDraft = useCallback(async () => {
    // Wait behind any create/update before reading idRef. This prevents a slow
    // first autosave from creating an orphan after a successful launch.
    await flush()
    const id = idRef.current
    if (id === null) return
    try {
      await deleteDraftRequest(id)
      idRef.current = null
      dirtyRef.current = false
      if (mountedRef.current) setDraftId(null)
      void queryClient.invalidateQueries({ queryKey: ['drafts', 'list'] })
    } catch {
      // Best-effort: an orphaned draft is harmless; the run is already launched.
    }
  }, [flush, queryClient])

  return {
    draft,
    setDraft,
    draftId,
    saveState,
    loading,
    loadError,
    loadDraft,
    saveNow,
    deleteCurrentDraft,
  }
}
