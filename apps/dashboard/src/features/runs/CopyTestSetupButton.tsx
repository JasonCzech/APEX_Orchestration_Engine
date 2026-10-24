import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { createDraftRequest } from '@/api/hooks/useDrafts'

import { loadRunSetupDraft } from './runSetup'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'

/** Copy frozen selection references into an ordinary draft; launch revalidates them. */
export function CopyTestSetupButton({ threadId, label = 'Create new test from this run', primary = false }: {
  threadId: string
  label?: string
  primary?: boolean
}) {
  const navigate = useNavigate()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mounted = useRef(true)
  const currentThread = useRef(threadId)
  const threadGeneration = useRef(0)
  if (currentThread.current !== threadId) threadGeneration.current += 1
  currentThread.current = threadId
  useEffect(() => { setPending(false); setError(null) }, [threadId])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  async function copy() {
    if (pending) return
    setPending(true)
    setError(null)
    const generation = threadGeneration.current
    const keyRevision = getApiKeyRevision()
    const sessionRevision = getSessionRevision()
    const sameView = () => mounted.current && currentThread.current === threadId && threadGeneration.current === generation
    const current = () => sameView() && keyRevision === getApiKeyRevision() && sessionRevision === getSessionRevision()
    try {
      const draft = await loadRunSetupDraft(threadId)
      if (!current()) return
      const saved = await createDraftRequest({ title: draft.title, project_id: draft.scope.project_id || null, payload: draft as unknown as Record<string, unknown> })
      if (current()) navigate(`/runs/new?draft=${encodeURIComponent(saved.id)}&step=scope`)
    } catch (failure) {
      if (current()) setError(failure instanceof Error ? failure.message : 'Could not copy this test setup.')
    } finally {
      if (sameView()) setPending(false)
    }
  }
  return <span>
    <button type="button" className={primary ? 'btn btn-primary' : 'btn btn-secondary btn-sm'} disabled={pending} onClick={() => void copy()}>{pending ? 'Copying setup…' : label}</button>
    {error && <span role="alert">{error}</span>}
  </span>
}
