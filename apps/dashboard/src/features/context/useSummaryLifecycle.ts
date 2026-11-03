import { useRef, useState, type FormEvent } from 'react'

import {
  contextSummaryCreateMutationKey,
  useContextSummaryHistory,
  useCreateSummary,
  type ContextSummaryRun,
} from '@/api/hooks/useContextApi'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'

export interface SummaryLifecycle {
  createSummary: ReturnType<typeof useCreateSummary>
  isSubmitting: boolean
  subject: string
  project: string
  keys: string[]
  keyDraft: string
  documentIds: string[]
  docIdDraft: string
  workTrackingConnectionId: string
  history: ContextSummaryRun[]
  setSubject: (value: string) => void
  setProject: (value: string) => void
  setKeyDraft: (value: string) => void
  addKey: () => void
  removeKey: (key: string) => void
  setDocIdDraft: (value: string) => void
  addDocumentId: (explicitId?: string) => void
  removeDocumentId: (id: string) => void
  setWorkTrackingConnectionId: (value: string) => void
  submit: (event: FormEvent) => void
}

/**
 * Form drafts remain page-local, while pending state and accepted handles are
 * read from the shared QueryClient so a route remount cannot duplicate or lose
 * an in-flight summary request.
 */
export function useSummaryLifecycle(): SummaryLifecycle {
  const createSummary = useCreateSummary()
  const pendingCount = usePendingMutationCount(contextSummaryCreateMutationKey())
  const historyQuery = useContextSummaryHistory()
  const [subject, setSubject] = useState('')
  const [project, setProject] = useState('')
  const [keys, setKeys] = useState<string[]>([])
  const [keyDraft, setKeyDraft] = useState('')
  const [documentIds, setDocumentIds] = useState<string[]>([])
  const [docIdDraft, setDocIdDraft] = useState('')
  const [workTrackingConnectionId, setWorkTrackingConnectionId] = useState('')
  const submittingRef = useRef(false)
  const isSubmitting = pendingCount > 0

  function addKey() {
    const key = keyDraft.trim()
    if (!key) return
    setKeys((prev) => (prev.includes(key) ? prev : [...prev, key]))
    setKeyDraft('')
  }

  function removeKey(key: string) {
    setKeys((prev) => prev.filter((existing) => existing !== key))
  }

  function addDocumentId(explicitId?: string) {
    const id = (explicitId ?? docIdDraft).trim()
    if (!id) return
    setDocumentIds((prev) => (prev.includes(id) ? prev : [...prev, id]))
    if (explicitId === undefined) setDocIdDraft('')
  }

  function removeDocumentId(id: string) {
    setDocumentIds((prev) => prev.filter((existing) => existing !== id))
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    const trimmed = subject.trim()
    if (!trimmed || submittingRef.current || isSubmitting) return
    submittingRef.current = true
    const trimmedConnection = workTrackingConnectionId.trim()
    createSummary.mutate(
      {
        subject: trimmed,
        work_item_keys: keys,
        project_id: project.trim() || null,
        ...(documentIds.length > 0 ? { document_ids: documentIds } : {}),
        ...(trimmedConnection ? { work_tracking_connection_id: trimmedConnection } : {}),
      },
      {
        onSettled: () => {
          submittingRef.current = false
        },
      },
    )
  }

  return {
    createSummary,
    isSubmitting,
    subject,
    project,
    keys,
    keyDraft,
    documentIds,
    docIdDraft,
    workTrackingConnectionId,
    history: historyQuery.data ?? [],
    setSubject,
    setProject,
    setKeyDraft,
    addKey,
    removeKey,
    setDocIdDraft,
    addDocumentId,
    removeDocumentId,
    setWorkTrackingConnectionId,
    submit,
  }
}
