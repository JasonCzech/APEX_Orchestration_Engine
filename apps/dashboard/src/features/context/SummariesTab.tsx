/**
 * Summaries tab — subject + work-item key chips + project -> POST
 * /v1/context/summaries (202). The accepted card and session-local history
 * follow the prompt playground pattern (D5): no polling, deep-link to
 * /runs/{thread_id} when the stream URL carries one.
 */
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router'

import { threadIdFromStreamUrl, useCreateSummary } from '@/api/hooks/useContextApi'
import { RequireRole } from '@/auth/RequireRole'
import { formatRelative } from '@/utils/time'

interface SummaryRun {
  runId: string
  threadId: string | null
  at: string
  subject: string
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
              <span className="ctx-caption">No thread link for this run.</span>
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
