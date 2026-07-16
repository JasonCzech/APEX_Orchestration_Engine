/**
 * Step 2 — Work items (all optional): NL -> translate -> editable provider
 * query (+ confidence chip) -> execute -> selectable results; direct key add
 * with validate-on-add; saved-query quick picks that execute on pick.
 *
 * Selected items persist their exact provider/connection binding. Legacy
 * drafts that only stored keys remain visible, but must be revalidated before
 * launch so a changed default tracker cannot silently redirect the run.
 */
import { useEffect, useRef, useState } from 'react'

import {
  fetchWorkItem,
  useExecuteQuery,
  useSavedQueries,
  useTranslateQuery,
  type ProviderQuery,
  type WorkItemPage,
} from '@/api/hooks/useWorkTracking'
import { safeExternalHttpUrl } from '@/utils/safeExternalUrl'

import type { StepProps } from '../NewRunWizard'
import type { WizardWorkItemRef } from '../wizardState'

type EditableQuery = ProviderQuery & { connectionId?: string }

function sameWorkItem(left: WizardWorkItemRef, right: WizardWorkItemRef): boolean {
  return left.key === right.key && left.connection_id === right.connection_id
}

export function WorkItemsStep({
  draft,
  onChange,
  draftGeneration = 0,
  isDraftGenerationCurrent = () => true,
  onPendingStart,
}: StepProps) {
  const [text, setText] = useState('')
  const [translated, setTranslated] = useState<EditableQuery | null>(null)
  const [results, setResults] = useState<WorkItemPage | null>(null)
  const [directKey, setDirectKey] = useState('')
  const [translateError, setTranslateError] = useState<string | null>(null)
  const [queryError, setQueryError] = useState<string | null>(null)
  const [addState, setAddState] = useState<{ busy: boolean; error: string | null }>({
    busy: false,
    error: null,
  })
  const projectRef = useRef(draft.scope.project_id.trim())
  const workItemsRef = useRef(draft.work_items)
  const generationRef = useRef(0)
  const translateRequestRef = useRef(0)
  const queryRequestRef = useRef(0)
  const operationRequestRef = useRef(0)
  workItemsRef.current = draft.work_items

  function beginPendingOperation(): () => void {
    return onPendingStart?.() ?? (() => undefined)
  }

  useEffect(() => {
    const nextProject = draft.scope.project_id.trim()
    if (nextProject !== projectRef.current) {
      projectRef.current = nextProject
      generationRef.current += 1
      translateRequestRef.current += 1
      queryRequestRef.current += 1
      operationRequestRef.current += 1
      setTranslated(null)
      setResults(null)
      setText('')
      setDirectKey('')
      setTranslateError(null)
      setQueryError(null)
      setAddState({ busy: false, error: null })
    }
  }, [draft.scope.project_id])

  const translate = useTranslateQuery()
  const execute = useExecuteQuery()
  const savedQueries = useSavedQueries()

  const selected = draft.work_items
  const selectedConnectionIds = Array.from(
    new Set(
      selected
        .map((item) => item.connection_id)
        .filter((connectionId): connectionId is string => Boolean(connectionId)),
    ),
  )
  const selectedConnectionId =
    selectedConnectionIds.length === 1 ? selectedConnectionIds[0] : undefined
  const selectedProvider = selected.find(
    (item) => item.connection_id === selectedConnectionId && item.provider,
  )?.provider

  function bindingConflict(ref: WizardWorkItemRef): string | null {
    const bound = workItemsRef.current.filter(
      (item): item is WizardWorkItemRef & { connection_id: string } =>
        Boolean(item.connection_id),
    )
    if (bound.some((item) => item.connection_id !== ref.connection_id)) {
      return 'Selected work items must use one work-tracking connection.'
    }
    if (
      bound.some(
        (item) =>
          item.connection_id === ref.connection_id &&
          item.provider &&
          item.provider.toLowerCase() !== ref.provider?.toLowerCase(),
      )
    ) {
      return 'The selected connection returned an inconsistent provider binding.'
    }
    return null
  }

  function addWorkItem(ref: WizardWorkItemRef): boolean {
    const conflict = bindingConflict(ref)
    if (conflict) {
      setAddState({ busy: false, error: conflict })
      return false
    }
    onChange((prev) => {
      const conflicting = prev.work_items.some(
        (item) => item.connection_id && item.connection_id !== ref.connection_id,
      )
      if (conflicting) return prev

      let inserted = false
      const workItems: WizardWorkItemRef[] = []
      for (const item of prev.work_items) {
        if (
          item.key === ref.key &&
          (item.connection_id === null || item.connection_id === ref.connection_id)
        ) {
          if (!inserted) {
            workItems.push(ref)
            inserted = true
          }
          continue
        }
        workItems.push(item)
      }
      if (!inserted) workItems.push(ref)
      return {
        ...prev,
        work_items: workItems,
      }
    })
    setAddState({ busy: false, error: null })
    return true
  }

  function removeWorkItem(ref: WizardWorkItemRef) {
    onChange((prev) => ({
      ...prev,
      work_items: prev.work_items.filter((existing) => !sameWorkItem(existing, ref)),
    }))
    setAddState({ busy: false, error: null })
  }

  async function runQuery(query: EditableQuery) {
    if (selectedConnectionIds.length > 1) {
      setAddState({
        busy: false,
        error: 'Remove work items from conflicting connections before running another query.',
      })
      return
    }
    const generation = generationRef.current
    const requestId = ++queryRequestRef.current
    const operationId = ++operationRequestRef.current
    const operationDraftGeneration = draftGeneration
    const requestProject = projectRef.current || undefined
    const connectionId = query.connectionId ?? selectedConnectionId
    const finishPending = beginPendingOperation()
    setResults(null)
    setQueryError(null)
    setAddState({ busy: false, error: null })
    try {
      const page = await execute.mutateAsync({
        query: {
          provider: query.provider,
          query: query.query,
          confidence: query.confidence,
        },
        ...(connectionId ? { connectionId } : {}),
        ...(requestProject ? { project: requestProject } : {}),
      })
      if (
        isDraftGenerationCurrent(operationDraftGeneration) &&
        requestId === queryRequestRef.current &&
        operationId === operationRequestRef.current &&
        generation === generationRef.current &&
        requestProject === (projectRef.current || undefined)
      ) {
        setTranslated({ ...query, connectionId: page.connection_id })
        setResults(page)
      }
    } catch (error) {
      if (
        isDraftGenerationCurrent(operationDraftGeneration) &&
        requestId === queryRequestRef.current &&
        operationId === operationRequestRef.current &&
        generation === generationRef.current &&
        requestProject === (projectRef.current || undefined)
      ) {
        setQueryError(error instanceof Error ? error.message : 'Provider query failed')
      }
    } finally {
      finishPending()
    }
  }

  async function translateNow() {
    if (selectedConnectionIds.length > 1) {
      setAddState({
        busy: false,
        error: 'Remove work items from conflicting connections before translating a query.',
      })
      return
    }
    const generation = generationRef.current
    const requestId = ++translateRequestRef.current
    const operationId = ++operationRequestRef.current
    const operationDraftGeneration = draftGeneration
    const requestProject = projectRef.current || undefined
    const finishPending = beginPendingOperation()
    setTranslated(null)
    setResults(null)
    setTranslateError(null)
    setQueryError(null)
    setAddState({ busy: false, error: null })
    try {
      const query = await translate.mutateAsync({
        text,
        ...(selectedConnectionId ? { connectionId: selectedConnectionId } : {}),
        ...(requestProject ? { project: requestProject } : {}),
      })
      if (
        isDraftGenerationCurrent(operationDraftGeneration) &&
        requestId === translateRequestRef.current &&
        operationId === operationRequestRef.current &&
        generation === generationRef.current &&
        requestProject === (projectRef.current || undefined)
      ) {
        if (
          selectedProvider &&
          query.provider.toLowerCase() !== selectedProvider.toLowerCase()
        ) {
          setAddState({
            busy: false,
            error: 'The selected connection returned an inconsistent provider binding.',
          })
          return
        }
        setTranslated({
          provider: query.provider,
          query: query.query,
          confidence: query.confidence,
          connectionId: query.connection_id,
        })
      }
    } catch (error) {
      if (
        isDraftGenerationCurrent(operationDraftGeneration) &&
        requestId === translateRequestRef.current &&
        operationId === operationRequestRef.current &&
        generation === generationRef.current &&
        requestProject === (projectRef.current || undefined)
      ) {
        setTranslateError(error instanceof Error ? error.message : 'Translate failed')
      }
    } finally {
      finishPending()
    }
  }

  async function resolveKey(
    key: string,
    existing?: WizardWorkItemRef,
  ): Promise<boolean> {
    if (selectedConnectionIds.length > 1 && !existing?.connection_id) {
      setAddState({
        busy: false,
        error: 'Remove work items from conflicting connections before revalidating another key.',
      })
      return false
    }
    const finishPending = beginPendingOperation()
    setAddState({ busy: true, error: null })
    const operationDraftGeneration = draftGeneration
    const requestGeneration = generationRef.current
    const requestProject = projectRef.current || undefined
    const editorQuery = translated
    const connectionId =
      existing?.connection_id ?? selectedConnectionId ?? editorQuery?.connectionId
    const expectedProvider =
      existing?.provider ??
      (connectionId === selectedConnectionId ? selectedProvider : undefined) ??
      (editorQuery && connectionId === editorQuery.connectionId
        ? editorQuery.provider
        : undefined)
    try {
      const item = await fetchWorkItem({
        key,
        ...(requestProject ? { project: requestProject } : {}),
        ...(connectionId ? { connectionId } : {}),
        ...(expectedProvider ? { expectedProvider } : {}),
      })
      if (
        !isDraftGenerationCurrent(operationDraftGeneration) ||
        requestGeneration !== generationRef.current ||
        requestProject !== (projectRef.current || undefined)
      ) {
        return false
      }
      return addWorkItem({
        key: item.key,
        connection_id: item.connection_id,
        provider: item.provider,
      })
    } catch (error) {
      if (
        !isDraftGenerationCurrent(operationDraftGeneration) ||
        requestGeneration !== generationRef.current ||
        requestProject !== (projectRef.current || undefined)
      ) {
        return false
      }
      setAddState({
        busy: false,
        error: error instanceof Error ? error.message : `Work item ${key} not found`,
      })
      return false
    } finally {
      finishPending()
    }
  }

  async function addDirectKey() {
    const key = directKey.trim()
    if (!key) return
    if (await resolveKey(key)) {
      setDirectKey('')
    }
  }

  const resultItems = results?.items ?? []
  const queryOperationPending = translate.isPending || execute.isPending
  const anyOperationPending = queryOperationPending || addState.busy

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
            disabled={anyOperationPending}
            onChange={(event) => setText(event.target.value)}
          />
          <button
            type="button"
            className="btn btn-secondary"
            disabled={text.trim().length === 0 || anyOperationPending}
            onClick={() => void translateNow()}
          >
            {translate.isPending ? 'Translating…' : 'Translate'}
          </button>
        </div>
        {translateError && (
          <p className="wizard-caption wizard-caption--danger" role="alert">
            Translate failed: {translateError}
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
            disabled={translate.isPending || execute.isPending}
            onChange={(event) => {
              const saved = savedQueries.data.find((entry) => entry.id === event.target.value)
              if (!saved) return
              if (saved.project_id && !saved.connection_id) {
                setAddState({
                  busy: false,
                  error: 'This legacy project query must be rebound before it can run.',
                })
                return
              }
              if (
                saved.connection_id &&
                selectedConnectionId &&
                saved.connection_id !== selectedConnectionId
              ) {
                setAddState({
                  busy: false,
                  error: 'This saved query uses a different work-tracking connection.',
                })
                return
              }
              const connectionId = saved.connection_id ?? selectedConnectionId
              const query: EditableQuery = {
                provider: saved.provider,
                query: saved.query,
                confidence: 1,
                ...(connectionId ? { connectionId } : {}),
              }
              setTranslated(query)
              void runQuery(query)
            }}
          >
            <option value="">Run a saved query…</option>
            {savedQueries.data
              .filter((saved) => !saved.project_id || saved.project_id === projectRef.current)
              .map((saved) => (
                <option
                  key={saved.id}
                  value={saved.id}
                  disabled={Boolean(saved.project_id) && !saved.connection_id}
                >
                  {saved.name}
                  {saved.project_id && !saved.connection_id ? ' (rebind required)' : ''}
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
            {translated.connectionId && (
              <span className="topbar-meta-chip" title="Pinned work-tracking connection">
                {translated.connectionId}
              </span>
            )}
          </div>
          <div className="wizard-row">
            <input
              id="wizard-provider-query"
              className="field-input wizard-grow wizard-mono"
              value={translated.query}
              disabled={anyOperationPending}
              onChange={(event) => setTranslated({ ...translated, query: event.target.value })}
            />
            <button
              type="button"
              className="btn btn-secondary"
              disabled={anyOperationPending || translated.query.trim().length === 0}
              onClick={() => void runQuery(translated)}
            >
              {execute.isPending ? 'Running…' : 'Run query'}
            </button>
          </div>
          {queryError && (
            <p className="wizard-caption wizard-caption--danger" role="alert">
              Query failed: {queryError}
            </p>
          )}
        </div>
      )}

      {results !== null && (
        <div className="wizard-field">
          <span className="wizard-label">Results ({resultItems.length})</span>
          {resultItems.length === 0 ? (
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
                  {resultItems.map((item) => {
                    const safeUrl = safeExternalHttpUrl(item.url)
                    const ref: WizardWorkItemRef = {
                      key: item.key,
                      connection_id: results.connection_id,
                      provider: results.provider,
                    }
                    const checked = selected.some((entry) => sameWorkItem(entry, ref))
                    return (
                      <tr key={item.key}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Select ${item.key}`}
                            checked={checked}
                            onChange={(event) => {
                              if (event.target.checked) addWorkItem(ref)
                              else removeWorkItem(ref)
                            }}
                          />
                        </td>
                        <td>
                          {safeUrl ? (
                            <a href={safeUrl} target="_blank" rel="noreferrer">
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
                    )
                  })}
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
            disabled={anyOperationPending}
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
            disabled={directKey.trim().length === 0 || anyOperationPending}
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
            {selected.map((item) => {
              const needsRevalidation = !item.connection_id || !item.provider
              return (
                <span
                  key={`${item.connection_id ?? 'legacy'}:${item.key}`}
                  className="wizard-chip"
                  title={
                    needsRevalidation
                      ? 'Legacy selection requires revalidation'
                      : `${item.provider} · ${item.connection_id}`
                  }
                >
                  {item.key}
                  {needsRevalidation && (
                    <button
                      type="button"
                      className="wizard-chip-revalidate"
                      disabled={anyOperationPending}
                      aria-label={`Revalidate ${item.key}`}
                      onClick={() => void resolveKey(item.key, item)}
                    >
                      revalidate
                    </button>
                  )}
                  <button
                    type="button"
                    className="wizard-chip-remove"
                    aria-label={`Remove ${item.key}`}
                    onClick={() => removeWorkItem(item)}
                  >
                    ×
                  </button>
                </span>
              )
            })}
          </div>
        </div>
      )}
    </section>
  )
}
