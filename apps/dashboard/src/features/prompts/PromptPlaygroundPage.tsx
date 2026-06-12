/**
 * /prompts/:ns/:name/playground (plan UX 2.e) — stateless prompt test runs.
 * Left: catalog-version selector (default active) OR ad-hoc content editor
 * toggle, plus a validated sample_input JSON editor ({} default). [Run test]
 * POSTs /v1/prompts/{id}/test; the 202 {run_id, thread_id?} renders as an
 * accepted card with a /runs/{thread_id} link plus a session-local history of
 * prior runs. Live playground streaming is a noted follow-up — no polling.
 */
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'

import {
  usePrompt,
  usePromptVersions,
  useTestPrompt,
} from '@/api/hooks/usePrompts'
import { isApiError } from '@/api/errors'
import { RequireRole } from '@/auth/RequireRole'
import { ProblemCard } from '@/components/ProblemCard'
import { formatRelative } from '@/utils/time'

import { PromptEditor } from './PromptEditor'
import { promptPath, usePromptRouteParams } from './promptPaths'
import './prompts.css'

type SourceMode = 'version' | 'adhoc'

interface PlaygroundRun {
  runId: string
  threadId: string | null
  at: string
  label: string
}

function errorMessage(error: unknown, fallback: string): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return fallback
}

/** '' and whitespace count as {}; otherwise must parse to a plain JSON object. */
function parseSampleInput(raw: string): { ok: true; value: Record<string, unknown> } | { ok: false; message: string } {
  const trimmed = raw.trim()
  if (!trimmed) return { ok: true, value: {} }
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { ok: false, message: 'Sample input must be a JSON object.' }
    }
    return { ok: true, value: parsed as Record<string, unknown> }
  } catch {
    return { ok: false, message: 'Sample input is not valid JSON.' }
  }
}

export function PromptPlaygroundPage() {
  const { ns, name } = usePromptRouteParams()
  const detailQuery = usePrompt(ns, name)
  const detail = detailQuery.data
  const versionsQuery = usePromptVersions(ns, name, detail?.id)
  const test = useTestPrompt(detail?.id)

  const [mode, setMode] = useState<SourceMode>('version')
  const [versionId, setVersionId] = useState('')
  const [adhoc, setAdhoc] = useState<string | null>(null)
  const [sampleInput, setSampleInput] = useState('{}')
  const [inputError, setInputError] = useState<string | null>(null)
  const [history, setHistory] = useState<PlaygroundRun[]>([])

  const versions = useMemo(
    () => [...(versionsQuery.data ?? [])].sort((a, b) => b.version - a.version),
    [versionsQuery.data],
  )

  // Default the selector to the active version once the detail lands.
  const activeId = detail?.active_version?.id ?? ''
  useEffect(() => {
    if (!versionId && activeId) setVersionId(activeId)
  }, [versionId, activeId])

  const selectedVersion = versions.find((entry) => entry.id === versionId)
  const adhocContent = adhoc ?? detail?.content ?? ''

  function run() {
    if (!detail || test.isPending) return
    const parsed = parseSampleInput(sampleInput)
    if (!parsed.ok) {
      setInputError(parsed.message)
      return
    }
    setInputError(null)
    const label =
      mode === 'adhoc'
        ? 'ad-hoc content'
        : selectedVersion
          ? `v${selectedVersion.version}`
          : 'active version'
    test.mutate(
      {
        ...(mode === 'adhoc' ? { content: adhocContent } : { version_id: versionId || activeId }),
        sample_input: parsed.value,
      },
      {
        onSuccess: (accepted) => {
          setHistory((prev) => [
            {
              runId: accepted.run_id,
              threadId: accepted.thread_id ?? null,
              at: new Date().toISOString(),
              label,
            },
            ...prev,
          ])
        },
      },
    )
  }

  if (detailQuery.isPending) {
    return (
      <section className="prompts-page animate-enter">
        <div role="status" aria-busy="true" aria-label="Loading prompt" className="prompts-muted">
          Loading prompt…
        </div>
      </section>
    )
  }
  if (detailQuery.isError || !detail) {
    return (
      <section className="prompts-page animate-enter">
        <ProblemCard
          title="Prompt unavailable"
          message={errorMessage(detailQuery.error, 'The prompt could not be loaded.')}
          onRetry={() => detailQuery.refetch()}
        />
      </section>
    )
  }

  const latest = history[0]

  return (
    <section className="prompts-page animate-enter">
      <header className="prompt-detail-header glass-panel">
        <nav className="prompt-breadcrumb" aria-label="Breadcrumb">
          <Link to={`/prompts?ns=${encodeURIComponent(ns)}`}>{ns}</Link>
          <span aria-hidden="true"> / </span>
          <Link to={promptPath(ns, name)}>{detail.key}</Link>
          <span aria-hidden="true"> / </span>
          <span className="strong">playground</span>
        </nav>
      </header>

      <div className="prompt-playground-split">
        <div className="prompt-content-card glass-panel">
          <div className="prompt-source-toggle" role="group" aria-label="Prompt source">
            <button
              type="button"
              className={`prompt-tab${mode === 'version' ? ' active' : ''}`}
              aria-pressed={mode === 'version'}
              onClick={() => setMode('version')}
            >
              Catalog version
            </button>
            <button
              type="button"
              className={`prompt-tab${mode === 'adhoc' ? ' active' : ''}`}
              aria-pressed={mode === 'adhoc'}
              onClick={() => setMode('adhoc')}
            >
              Ad-hoc content
            </button>
          </div>

          {mode === 'version' ? (
            <label className="prompt-field">
              <span className="prompt-field-label">Version</span>
              <select
                className="field-select"
                aria-label="Version to test"
                value={versionId}
                onChange={(event) => setVersionId(event.target.value)}
              >
                {versions.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    v{entry.version}
                    {entry.id === activeId ? ' (active)' : ''}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <div className="prompt-field">
              <span className="prompt-field-label">Content</span>
              <PromptEditor value={adhocContent} onChange={setAdhoc} ariaLabel="Ad-hoc prompt content" />
            </div>
          )}

          <label className="prompt-field">
            <span className="prompt-field-label">Sample input (JSON)</span>
            <textarea
              className="field-input prompt-sample-input"
              rows={6}
              value={sampleInput}
              onChange={(event) => setSampleInput(event.target.value)}
              aria-label="Sample input JSON"
              spellCheck={false}
            />
          </label>
          {inputError && (
            <div className="tonal-card danger" role="alert">
              {inputError}
            </div>
          )}
          {test.isError && (
            <div className="tonal-card danger" role="alert">
              {errorMessage(test.error, 'Test run failed.')}
            </div>
          )}
          <RequireRole
            role="operator"
            fallback={
              <p className="prompts-muted">Viewer role — playground runs are disabled.</p>
            }
          >
            <div className="prompt-modal-actions">
              <button
                type="button"
                className="btn btn-primary"
                onClick={run}
                disabled={test.isPending}
              >
                {test.isPending ? 'Submitting…' : 'Run test'}
              </button>
            </div>
          </RequireRole>
        </div>

        <div className="prompt-content-card glass-panel">
          {latest ? (
            <div className="tonal-card success prompt-run-card" data-testid="playground-accepted">
              <span className="strong">Run accepted</span>
              <span className="prompt-run-id">{latest.runId}</span>
              {latest.threadId ? (
                <Link className="btn btn-secondary btn-sm" to={`/runs/${latest.threadId}`}>
                  Open run
                </Link>
              ) : (
                <span className="prompts-muted">No thread link for this run.</span>
              )}
            </div>
          ) : (
            <div className="dash-empty compact">
              <h2>No runs yet</h2>
              <p className="dash-empty-hint">
                Submit a test run to see it accepted here. Live playground output is a noted
                follow-up.
              </p>
            </div>
          )}

          {history.length > 0 && (
            <>
              <h3 className="prompt-history-title">This session</h3>
              <ul className="prompt-run-history" data-testid="playground-history">
                {history.map((entry) => (
                  <li key={entry.runId} className="prompt-run-history-item">
                    <span className="prompt-run-id">{entry.runId}</span>
                    <span className="prompts-muted">
                      {entry.label} · {formatRelative(entry.at)}
                    </span>
                    {entry.threadId && (
                      <Link className="prompt-run-link" to={`/runs/${entry.threadId}`}>
                        open
                      </Link>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      </div>
    </section>
  )
}
