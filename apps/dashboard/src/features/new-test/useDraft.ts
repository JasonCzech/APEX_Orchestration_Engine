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

import {
  createDraftRequest,
  deleteDraftRequest,
  getDraftRequest,
  updateDraftRequest,
} from '@/api/hooks/useDrafts'

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
  const [draft, setDraftState] = useState<WizardDraft>(emptyDraft)
  const [draftId, setDraftId] = useState<string | null>(initialDraftId)
  const [saveState, setSaveState] = useState<DraftSaveState>('idle')
  const [loading, setLoading] = useState<boolean>(initialDraftId !== null)
  const [loadError, setLoadError] = useState<string | null>(null)

  const draftRef = useRef(draft)
  const idRef = useRef<string | null>(initialDraftId)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const savingRef = useRef(false)
  const dirtyWhileSavingRef = useRef(false)
  const onCreatedRef = useRef(onDraftCreated)
  onCreatedRef.current = onDraftCreated

  const flush = useCallback(async () => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    if (savingRef.current) {
      // A save is mid-flight; remember to run another one with the newer state.
      dirtyWhileSavingRef.current = true
      return
    }
    savingRef.current = true
    setSaveState('saving')
    try {
      const snapshot = draftRef.current
      const body = {
        title: snapshot.title.trim() || UNTITLED_DRAFT_TITLE,
        payload: snapshot as unknown as Record<string, unknown>,
      }
      if (idRef.current === null) {
        const created = await createDraftRequest({
          ...body,
          project_id: snapshot.scope.project_id.trim() || null,
        })
        idRef.current = created.id
        setDraftId(created.id)
        onCreatedRef.current?.(created.id)
      } else {
        await updateDraftRequest(idRef.current, body)
      }
      setSaveState('saved')
    } catch {
      setSaveState('error')
    } finally {
      savingRef.current = false
      if (dirtyWhileSavingRef.current) {
        dirtyWhileSavingRef.current = false
        void flush()
      }
    }
  }, [])

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
      idRef.current = stored.id
      setDraftState(parsed)
      setDraftId(stored.id)
      setSaveState('saved')
    } catch (error) {
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

  // Clear any pending timer on unmount (the in-flight fetch may still settle).
  useEffect(
    () => () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current)
    },
    [],
  )

  const saveNow = useCallback(async () => {
    await flush()
  }, [flush])

  const deleteCurrentDraft = useCallback(async () => {
    const id = idRef.current
    if (id === null) return
    try {
      await deleteDraftRequest(id)
    } catch {
      // Best-effort: an orphaned draft is harmless; the run is already launched.
    }
  }, [])

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
