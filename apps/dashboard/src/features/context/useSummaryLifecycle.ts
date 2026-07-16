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
  history: ContextSummaryRun[]
  setSubject: (value: string) => void
  setProject: (value: string) => void
  setKeyDraft: (value: string) => void
  addKey: () => void
  removeKey: (key: string) => void
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

  function submit(event: FormEvent) {
    event.preventDefault()
    const trimmed = subject.trim()
    if (!trimmed || submittingRef.current || isSubmitting) return
    submittingRef.current = true
    createSummary.mutate(
      {
        subject: trimmed,
        work_item_keys: keys,
        project_id: project.trim() || null,
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
    history: historyQuery.data ?? [],
    setSubject,
    setProject,
    setKeyDraft,
    addKey,
    removeKey,
    submit,
  }
}
