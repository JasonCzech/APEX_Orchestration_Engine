import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { createDraftRequest } from '@/api/hooks/useDrafts'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import { isRecord, parseDraftPayload } from '@/features/new-test/wizardState'
import { PHASE_NAMES } from '@apex/pipeline-events'

/** A copy keeps its source's name visibly distinct so two runs never share one
 * title: "Name" -> "Name (copy)" -> "Name (copy 2)". Fits the 500-char limit. */
function copyTitle(title: string) {
  const trimmed = title.trim()
  const match = /^(.*) \(copy(?: (\d+))?\)$/.exec(trimmed)
  const base = match ? match[1]! : trimmed
  const suffix = match ? ` (copy ${Number(match[2] ?? 1) + 1})` : ' (copy)'
  return Array.from(base).slice(0, 500 - suffix.length).join('') + suffix
}

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
      const { data, error: failure, response } = await getApexClient().GET('/v1/pipelines/{thread_id}/setup', { params: { path: { thread_id: threadId } } })
      if (!response.ok || !data) throw new ApiError(response.status, errorMessageOf(failure, 'Could not copy this test setup.'))
      if (!current()) return
      const cfg = data.configurable
      const owners = isRecord(cfg.phase_execution) && isRecord(cfg.phase_execution.owners) ? cfg.phase_execution.owners : null
      const draft = parseDraftPayload({
        title: copyTitle(data.title), request: data.request,
        scope: { test_scope: data.test_scope ?? cfg.test_scope, project_id: data.project_id, app_id: data.app_id, environment_id: data.environment_id },
        config: { engine: cfg.engine, phases: owners ? PHASE_NAMES.filter(phase => owners[phase] === 'langgraph') : cfg.phases, ...(owners ? { schema_version: 2 } : {}), analysis: cfg.analysis, gates_mode: 'custom', gates_custom: cfg.gates, golden_configurable: cfg },
        prompt_selections: cfg.prompt_selections, prompt_overrides: cfg.prompt_overrides,
      })
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
