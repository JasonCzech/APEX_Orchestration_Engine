/**
 * Summaries tab — subject + work-item key chips + project -> POST
 * /v1/context/summaries (202). The accepted card and session-local history
 * follow the prompt playground pattern (D5): no polling, deep-link to
 * /runs/{thread_id} when the stream URL carries one.
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router'

import { notifyUnauthorized } from '@/api/apexClient'
import { getApiKey, getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import { resolveLanggraphBaseUrl } from '@/config/runtimeConfig'
import { threadIdFromStreamUrl, useCreateSummary } from '@/api/hooks/useContextApi'
import { RequireRole } from '@/auth/RequireRole'
import { formatRelative } from '@/utils/time'

interface SummaryRun {
  runId: string
  threadId: string | null
  streamUrl: string
  at: string
  subject: string
}

function SummaryStreamButton({ streamUrl }: { streamUrl: string }) {
  const [state, setState] = useState<{ status: 'idle' | 'loading' | 'done' | 'error'; text?: string }>({
    status: 'idle',
  })
  const controllerRef = useRef<AbortController | null>(null)
  useEffect(() => () => controllerRef.current?.abort(), [])

  const open = useCallback(async () => {
    setState({ status: 'loading' })
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const apiKey = getApiKey()
    const keyRevision = getApiKeyRevision()
    const sessionRevision = getSessionRevision()
    try {
      const base = new URL(resolveLanggraphBaseUrl() || window.location.origin, window.location.origin)
      const url = new URL(streamUrl, base)
      if (url.origin !== base.origin) throw new Error('Summary stream URL must use the LangGraph origin')
      const response = await fetch(url, {
        headers: { ...(apiKey ? { 'x-api-key': apiKey } : {}) },
        signal: controller.signal,
      })
      if (response.status === 401 && keyRevision === getApiKeyRevision()) notifyUnauthorized()
      if (!response.ok || !response.body) throw new Error(`Stream request failed (${response.status})`)
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let summary = ''
      const consume = (block: string) => {
          const lines = block.split(/\r?\n/)
          const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim()
          const data = lines
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trim())
            .join('\n')
          if (!data) return
          if (event === 'error') throw new Error(data)
          try {
            const parsed: unknown = JSON.parse(data)
            const record = parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : {}
            const candidate = record['data'] ?? record['value'] ?? parsed
            const value = candidate && typeof candidate === 'object' ? candidate as Record<string, unknown> : {}
            const nested = value['values'] && typeof value['values'] === 'object' ? value['values'] as Record<string, unknown> : value
            if (typeof nested['summary'] === 'string') summary = nested['summary']
          } catch {
            // Ignore non-JSON keep-alives and let the stream continue.
          }
      }
      while (true) {
        const chunk = await reader.read()
        if (chunk.done) break
        buffer += decoder.decode(chunk.value, { stream: true })
        for (;;) {
          const boundary = /\r?\n\r?\n/.exec(buffer)
          if (!boundary || boundary.index === undefined) break
          consume(buffer.slice(0, boundary.index))
          buffer = buffer.slice(boundary.index + boundary[0].length)
        }
      }
      buffer += decoder.decode()
      if (buffer.trim()) consume(buffer)
      if (
        controller.signal.aborted ||
        keyRevision !== getApiKeyRevision() ||
        sessionRevision !== getSessionRevision()
      ) return
      setState({ status: 'done', text: summary || 'Stream completed without a summary payload.' })
    } catch (error) {
      if (controller.signal.aborted) return
      setState({ status: 'error', text: error instanceof Error ? error.message : 'Unable to read the summary stream.' })
    }
  }, [streamUrl])

  return (
    <span className="ctx-stream-result">
      <button type="button" className="btn btn-secondary btn-sm" onClick={() => void open()} disabled={state.status === 'loading'}>
        {state.status === 'loading' ? 'Loading…' : 'Open live stream'}
      </button>
      {state.text && <pre className={`ctx-stream-output${state.status === 'error' ? ' danger' : ''}`}>{state.text}</pre>}
    </span>
  )
}

export function SummariesTab() {
  const createSummary = useCreateSummary()

  const [subject, setSubject] = useState('')
  const [project, setProject] = useState('')
  const [keys, setKeys] = useState<string[]>([])
  const [keyDraft, setKeyDraft] = useState('')
  const [history, setHistory] = useState<SummaryRun[]>([])

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
    if (!trimmed || createSummary.isPending) return
    createSummary.mutate(
      {
        subject: trimmed,
        work_item_keys: keys,
        project_id: project.trim() || null,
      },
      {
        onSuccess: (accepted) => {
          setHistory((prev) => [
            {
              runId: accepted.run_id,
              threadId: threadIdFromStreamUrl(accepted.stream_url),
              streamUrl: accepted.stream_url,
              at: new Date().toISOString(),
              subject: trimmed,
            },
            ...prev,
          ])
        },
      },
    )
  }

  const latest = history[0]

  return (
    <div className="ctx-split">
      <form
        className="ctx-card glass-panel ctx-grow"
        aria-label="Generate summary"
        onSubmit={submit}
      >
        <h3 className="ctx-card-title">Generate summary</h3>
        <label className="ctx-field">
          <span className="ctx-field-label">Subject</span>
          <input
            type="text"
            className="field-input"
            aria-label="Summary subject"
            placeholder="Checkout latency regression — context for the perf run"
            value={subject}
            onChange={(event) => setSubject(event.target.value)}
          />
        </label>
        <div className="ctx-field">
          <label className="ctx-field-label" htmlFor="ctx-key-draft">
            Work item keys
          </label>
          <div className="ctx-row">
            <input
              id="ctx-key-draft"
              type="text"
              className="field-input"
              placeholder="PHX-241"
              value={keyDraft}
              onChange={(event) => setKeyDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  addKey()
                }
              }}
            />
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={keyDraft.trim() === ''}
              onClick={addKey}
            >
              Add key
            </button>
          </div>
          {keys.length > 0 && (
            <div className="ctx-chip-row" data-testid="summary-keys">
              {keys.map((key) => (
                <span key={key} className="ctx-chip">
                  {key}
                  <button
                    type="button"
                    className="ctx-chip-remove"
                    aria-label={`Remove ${key}`}
                    onClick={() => removeKey(key)}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
        <label className="ctx-field">
          <span className="ctx-field-label">Project</span>
          <input
            type="text"
            className="field-input"
            aria-label="Project id"
            placeholder="proj-alpha"
            value={project}
            onChange={(event) => setProject(event.target.value)}
          />
        </label>
        {createSummary.isError && (
          <div className="ctx-inline-error" role="alert">
            <span>Summary request failed: {createSummary.error.message}</span>
          </div>
        )}
        <RequireRole
          role="operator"
          fallback={<p className="ctx-caption">Viewer role — summary generation is disabled.</p>}
        >
          <div className="ctx-actions">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={subject.trim() === '' || createSummary.isPending}
            >
              {createSummary.isPending ? 'Submitting…' : 'Generate summary'}
            </button>
          </div>
        </RequireRole>
      </form>

      <div className="ctx-card glass-panel ctx-grow">
        {latest ? (
          <div className="tonal-card success ctx-run-card" data-testid="summary-accepted">
            <span className="strong">Summary run accepted</span>
            <span className="ctx-run-id">{latest.runId}</span>
            {latest.threadId ? (
              <Link className="btn btn-secondary btn-sm" to={`/runs/${latest.threadId}`}>
                Open run
              </Link>
            ) : (
              <SummaryStreamButton streamUrl={latest.streamUrl} />
            )}
          </div>
        ) : (
          <div className="dash-empty compact">
            <h2>No summary runs yet</h2>
            <p className="dash-empty-hint">
              Submit a subject to kick off a summary run. Results land on the run&apos;s thread —
              this panel only tracks this session&apos;s submissions.
            </p>
          </div>
        )}

        {history.length > 0 && (
          <>
            <h4 className="ctx-history-title">This session</h4>
            <ul className="ctx-history" data-testid="summary-history">
              {history.map((entry) => (
                <li key={entry.runId} className="ctx-history-item">
                    <span className="ctx-run-id">{entry.runId}</span>
                    {!entry.threadId && (
                      <SummaryStreamButton streamUrl={entry.streamUrl} />
                    )}
                  <span className="ctx-caption">
                    {entry.subject} · {formatRelative(entry.at)}
                  </span>
                  {entry.threadId && <Link to={`/runs/${entry.threadId}`}>open</Link>}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  )
}
