/**
 * Step 2 — Work items (all optional): NL -> translate -> editable provider
 * query (+ confidence chip) -> execute -> selectable results; direct key add
 * with validate-on-add (getWorkItem); saved-query quick picks that execute on
 * pick. Only the selected KEYS persist in the draft; query text and results
 * are transient UI state.
 */
import { useEffect, useRef, useState } from 'react'

import {
  fetchWorkItem,
  useExecuteQuery,
  useSavedQueries,
  useTranslateQuery,
  type TranslatedQuery,
  type WorkItem,
} from '@/api/hooks/useWorkTracking'

import type { StepProps } from '../NewRunWizard'

export function WorkItemsStep({ draft, onChange }: StepProps) {
  const [text, setText] = useState('')
  const [translated, setTranslated] = useState<TranslatedQuery | null>(null)
  const [results, setResults] = useState<WorkItem[] | null>(null)
  const [directKey, setDirectKey] = useState('')
  const [addState, setAddState] = useState<{ busy: boolean; error: string | null }>({
    busy: false,
    error: null,
  })
  const projectRef = useRef(draft.scope.project_id.trim())
  const generationRef = useRef(0)
  useEffect(() => {
    const nextProject = draft.scope.project_id.trim()
    if (nextProject !== projectRef.current) {
      projectRef.current = nextProject
      generationRef.current += 1
      setTranslated(null)
      setResults(null)
      setText('')
      setDirectKey('')
    }
  }, [draft.scope.project_id])

  const translate = useTranslateQuery()
  const execute = useExecuteQuery()
  const savedQueries = useSavedQueries()

  const selected = draft.work_item_keys

  function addKey(key: string) {
    onChange((prev) =>
      prev.work_item_keys.includes(key)
        ? prev
        : { ...prev, work_item_keys: [...prev.work_item_keys, key] },
    )
  }

  function removeKey(key: string) {
    onChange((prev) => ({
      ...prev,
      work_item_keys: prev.work_item_keys.filter((existing) => existing !== key),
    }))
  }

  function runQuery(query: TranslatedQuery) {
    const generation = generationRef.current
    const requestProject = projectRef.current || undefined
    execute.mutate(
      { query, ...(requestProject ? { project: requestProject } : {}) },
      {
        onSuccess: (page) => {
          if (generation === generationRef.current && requestProject === (projectRef.current || undefined)) {
            setResults(page.items ?? [])
          }
        },
      },
    )
  }

  async function addDirectKey() {
    const key = directKey.trim()
    if (!key) return
    setAddState({ busy: true, error: null })
    try {
      const requestGeneration = generationRef.current
      const requestProject = projectRef.current || undefined
      const item = await fetchWorkItem(key, requestProject)
      if (requestGeneration !== generationRef.current || requestProject !== (projectRef.current || undefined)) return
      addKey(item.key)
      setDirectKey('')
      setAddState({ busy: false, error: null })
    } catch (error) {
      setAddState({
        busy: false,
        error: error instanceof Error ? error.message : `Work item ${key} not found`,
      })
    }
  }

  return (
    <section className="wizard-step" aria-label="Work items">
      <p className="wizard-step-hint">
        Link the stories or defects this run covers — or skip; work items are optional.
      </p>

      <div className="wizard-field">
        <label className="wizard-label" htmlFor="wizard-nl-query">
          Find by description
        </label>
        <div className="wizard-row">
          <input
            id="wizard-nl-query"
            className="field-input wizard-grow"
            value={text}
            placeholder="open payment stories assigned to my team"
            onChange={(event) => setText(event.target.value)}
          />
          <button
            type="button"
            className="btn btn-secondary"
            disabled={text.trim().length === 0 || translate.isPending}
            onClick={() =>
              (() => {
                const generation = generationRef.current
                const requestProject = projectRef.current || undefined
                translate.mutate(
                  { text, ...(requestProject ? { project: requestProject } : {}) },
                  {
                    onSuccess: (query) => {
                      if (generation === generationRef.current && requestProject === (projectRef.current || undefined)) {
                        setTranslated(query)
                      }
                    },
                  },
                )
              })()
            }
          >
            {translate.isPending ? 'Translating…' : 'Translate'}
          </button>
        </div>
        {translate.isError && (
          <p className="wizard-caption wizard-caption--danger" role="alert">
            Translate failed: {translate.error.message}
          </p>
        )}
      </div>

      {savedQueries.data && savedQueries.data.length > 0 && (
        <div className="wizard-field">
          <label className="wizard-label" htmlFor="wizard-saved-query">
            Saved queries
          </label>
          <select
            id="wizard-saved-query"
            className="field-select"
            value=""
            onChange={(event) => {
              const saved = savedQueries.data.find((entry) => entry.id === event.target.value)
              if (!saved) return
              const query = { provider: saved.provider, query: saved.query, confidence: 1 }
              setTranslated(query)
              runQuery(query)
            }}
          >
            <option value="">Run a saved query…</option>
            {savedQueries.data.map((saved) => (
              <option key={saved.id} value={saved.id}>
                {saved.name}
              </option>
            ))}
          </select>
        </div>
      )}

      {translated && (
        <div className="wizard-field" data-testid="translated-query">
          <label className="wizard-label" htmlFor="wizard-provider-query">
            Provider query
          </label>
          <div className="wizard-row">
            <span className="topbar-meta-chip accent">{translated.provider}</span>
            <span
              className={`topbar-meta-chip ${translated.confidence >= 0.7 ? 'success' : 'warning'}`}
              title="Translation confidence"
            >
              confidence {Math.round(translated.confidence * 100)}%
            </span>
          </div>
          <div className="wizard-row">
            <input
              id="wizard-provider-query"
              className="field-input wizard-grow wizard-mono"
              value={translated.query}
              onChange={(event) => setTranslated({ ...translated, query: event.target.value })}
            />
            <button
              type="button"
              className="btn btn-secondary"
              disabled={execute.isPending || translated.query.trim().length === 0}
              onClick={() => runQuery(translated)}
            >
              {execute.isPending ? 'Running…' : 'Run query'}
            </button>
          </div>
          {execute.isError && (
            <p className="wizard-caption wizard-caption--danger" role="alert">
              Query failed: {execute.error.message}
            </p>
          )}
        </div>
      )}

      {results !== null && (
        <div className="wizard-field">
          <span className="wizard-label">Results ({results.length})</span>
          {results.length === 0 ? (
            <p className="wizard-caption">No work items matched.</p>
          ) : (
            <div className="data-table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th aria-label="Select" />
                    <th>Key</th>
                    <th>Title</th>
                    <th>Kind</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((item) => (
                    <tr key={item.key}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`Select ${item.key}`}
                          checked={selected.includes(item.key)}
                          onChange={(event) =>
                            event.target.checked ? addKey(item.key) : removeKey(item.key)
                          }
                        />
                      </td>
                      <td>
                        {item.url ? (
                          <a href={item.url} target="_blank" rel="noreferrer">
                            {item.key}
                          </a>
                        ) : (
                          item.key
                        )}
                      </td>
                      <td>{item.title}</td>
                      <td>{item.kind}</td>
                      <td>{item.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <div className="wizard-field">
        <label className="wizard-label" htmlFor="wizard-direct-key">
          Add by key
        </label>
        <div className="wizard-row">
          <input
            id="wizard-direct-key"
            className="field-input wizard-mono"
            value={directKey}
            placeholder="PHX-241"
            onChange={(event) => setDirectKey(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void addDirectKey()
              }
            }}
          />
          <button
            type="button"
            className="btn btn-ghost"
            disabled={directKey.trim().length === 0 || addState.busy}
            onClick={() => void addDirectKey()}
          >
            {addState.busy ? 'Checking…' : 'Add'}
          </button>
        </div>
        {addState.error && (
          <p className="wizard-caption wizard-caption--danger" role="alert">
            {addState.error}
          </p>
        )}
      </div>

      {selected.length > 0 && (
        <div className="wizard-field">
          <span className="wizard-label">Selected ({selected.length})</span>
          <div className="wizard-chip-row" data-testid="selected-work-items">
            {selected.map((key) => (
              <span key={key} className="wizard-chip">
                {key}
                <button
                  type="button"
                  className="wizard-chip-remove"
                  aria-label={`Remove ${key}`}
                  onClick={() => removeKey(key)}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        </div>
      )}
    </section>
  )
}
