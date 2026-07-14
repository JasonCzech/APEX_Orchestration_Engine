/**
 * /work-items — the query console (plan Part 2 route table; wizard step 2's
 * translate -> execute pattern, full-page). NL text translates to an editable
 * provider query (confidence chip), or manual mode writes provider/query
 * directly. Saved queries load + execute on pick. Operator+ extras: save the
 * current query, create a new tracker item.
 *
 * Preload contract: /work-items?provider=X&query=Y (search params, NOT
 * location state — survives refresh, copyable) auto-executes once on mount.
 * SavedQueriesPage's Run action links here with that shape.
 */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'

import {
  useCreateSavedQuery,
  useCreateWorkItem,
  useExecuteQuery,
  useSavedQueries,
  useTranslateQuery,
  type WorkItem,
  type WorkItemPage,
} from '@/api/hooks/useWorkTracking'
import { useConsumer } from '@/auth/AuthProvider'
import { roleAtLeast } from '@/auth/RequireRole'
import { Dialog } from '@/components/Dialog'

import { ExternalLink, KindChip, StatusBadge } from './workItemsBits'
import { workItemPath } from './workItemsLogic'
import './work-items.css'

const LIMIT_OPTIONS = [10, 25, 50]
const KIND_OPTIONS = ['story', 'task', 'bug', 'epic']

type ConsoleMode = 'nl' | 'manual'

/** Name + description modal -> POST /v1/work-tracking/saved-queries (operator+). */
function SaveQueryModal({
  provider,
  query,
  project,
  onClose,
}: {
  provider: string
  query: string
  project?: string
  onClose: () => void
}) {
  const create = useCreateSavedQuery()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const canSubmit = name.trim() !== '' && !create.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    create.mutate(
      {
        name: name.trim(),
        description: description.trim() || null,
        provider,
        query,
        project_id: project ?? null,
      },
      { onSuccess: onClose },
    )
  }

  return (
    <Dialog
      overlayClassName="wi-overlay"
      className="wi-modal glass-panel"
      ariaLabel="Save query"
      onClose={onClose}
      closeOnBackdrop={!create.isPending}
      closeOnEscape={!create.isPending}
      panelAs="form"
      onSubmit={submit}
    >
      <h2 className="wi-modal-title">Save query</h2>
      <p className="wi-modal-caption">
        Saves the current <strong>{provider}</strong> query for quick reuse.
      </p>
        <label className="wi-field">
          <span className="wi-field-label">Name</span>
          <input
            type="text"
            className="field-input"
            aria-label="Query name"
            placeholder="Open payment stories"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="wi-field">
          <span className="wi-field-label">Description</span>
          <textarea
            className="field-input"
            aria-label="Query description"
            rows={2}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        {create.isError && (
          <div className="wi-inline-error" role="alert">
            <span>Save failed: {create.error.message}</span>
          </div>
        )}
        <div className="wi-modal-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onClose}
            disabled={create.isPending}
          >
            Cancel
          </button>
          <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
            {create.isPending ? 'Saving…' : 'Save query'}
          </button>
        </div>
    </Dialog>
  )
}

/** Title/kind/description modal -> POST /v1/work-tracking/items (operator+). */
function NewItemModal({
  provider,
  project,
  onClose,
}: {
  provider: string
  project?: string
  onClose: () => void
}) {
  const navigate = useNavigate()
  const create = useCreateWorkItem()
  const [title, setTitle] = useState('')
  const [kind, setKind] = useState<string>('story')
  const [description, setDescription] = useState('')
  const canSubmit = title.trim() !== '' && !create.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    create.mutate(
      { body: { title: title.trim(), kind, description }, ...(project ? { project } : {}) },
      { onSuccess: (item) => void navigate(workItemPath(provider, item.key, project)) },
    )
  }

  return (
    <Dialog
      overlayClassName="wi-overlay"
      className="wi-modal glass-panel"
      ariaLabel="New work item"
      onClose={onClose}
      closeOnBackdrop={!create.isPending}
      closeOnEscape={!create.isPending}
      panelAs="form"
      onSubmit={submit}
    >
      <h2 className="wi-modal-title">New work item</h2>
        <label className="wi-field">
          <span className="wi-field-label">Title</span>
          <input
            type="text"
            className="field-input"
            aria-label="Item title"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
        </label>
        <label className="wi-field">
          <span className="wi-field-label">Kind</span>
          <select
            className="field-select"
            aria-label="Item kind"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
          >
            {KIND_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <label className="wi-field">
          <span className="wi-field-label">Description</span>
          <textarea
            className="field-input"
            aria-label="Item description"
            rows={4}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        {create.isError && (
          <div className="wi-inline-error" role="alert">
            <span>Create failed: {create.error.message}</span>
          </div>
        )}
        <div className="wi-modal-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onClose}
            disabled={create.isPending}
          >
            Cancel
          </button>
          <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
            {create.isPending ? 'Creating…' : 'Create item'}
          </button>
        </div>
    </Dialog>
  )
}

function ResultsTable({
  items,
  provider,
  project,
}: {
  items: WorkItem[]
  provider: string
  project?: string
}) {
  return (
    <div className="data-table-wrap">
      <table className="data-table striped">
        <thead>
          <tr>
            <th>Key</th>
            <th>Title</th>
            <th>Kind</th>
            <th>Status</th>
            <th className="wi-actions-cell">
              <span className="sr-only">Tracker link</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.key} data-testid={`wi-row-${item.key}`}>
              <td>
                <Link className="wi-key-link" to={workItemPath(provider, item.key, project)}>
                  {item.key}
                </Link>
              </td>
              <td className="strong">{item.title}</td>
              <td>
                <KindChip kind={item.kind} />
              </td>
              <td>
                <StatusBadge status={item.status} />
              </td>
              <td className="wi-actions-cell">
                {item.url ? <ExternalLink url={item.url} itemKey={item.key} /> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function WorkItemsPage() {
  const [searchParams] = useSearchParams()
  const consumer = useConsumer()
  const canMutate = consumer ? roleAtLeast(consumer.role, 'operator') : false

  const preloadProvider = searchParams.get('provider')
  const preloadQuery = searchParams.get('query')
  const preloadProject = searchParams.get('project')
  const scopedProjects = Array.from(
    new Set((consumer?.scopes ?? []).map((scope) => scope.project_id)),
  ).sort()
  const requiresProject = scopedProjects.length > 1
  const defaultProject = scopedProjects.length === 1 ? scopedProjects[0] : undefined
  const validPreloadProject =
    preloadProject &&
    (scopedProjects.length === 0 || scopedProjects.includes(preloadProject))
      ? preloadProject
      : undefined

  const [mode, setMode] = useState<ConsoleMode>(preloadQuery ? 'manual' : 'nl')
  const [nlText, setNlText] = useState('')
  const [provider, setProvider] = useState(preloadProvider ?? '')
  const [queryText, setQueryText] = useState(preloadQuery ?? '')
  const [confidence, setConfidence] = useState<number | null>(null)
  const [project, setProject] = useState(validPreloadProject ?? defaultProject ?? '')
  const [limit, setLimit] = useState(25)
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<WorkItemPage | null>(null)
  const [submitted, setSubmitted] = useState<{
    query: { provider: string; query: string; confidence: number }
    limit: number
    project?: string
  } | null>(null)
  const [saving, setSaving] = useState(false)
  const [creating, setCreating] = useState(false)
  const projectRef = useRef(project)
  projectRef.current = project

  const translate = useTranslateQuery()
  const execute = useExecuteQuery()
  const savedQueries = useSavedQueries()

  // A project change invalidates every result and query draft tied to the old
  // tracker scope. Pending callbacks are guarded by the project captured by
  // runQuery below so they cannot repaint the new project.
  useEffect(() => {
    setPage(null)
    setSubmitted(null)
    setOffset(0)
    setProvider('')
    setQueryText('')
    setConfidence(null)
  }, [project])

  function runQuery(
    next: { provider: string; query: string; confidence?: number },
    nextOffset = 0,
    nextLimit = limit,
    nextProject = project || undefined,
  ) {
    const requestProject = nextProject
    const submittedQuery = {
      provider: next.provider.trim(),
      query: next.query.trim(),
      confidence: next.confidence ?? 1,
    }
    execute.mutate(
      {
        query: submittedQuery,
        limit: nextLimit,
        offset: nextOffset,
        ...(nextProject ? { project: nextProject } : {}),
      },
      {
        onSuccess: (result) => {
          if ((requestProject ?? '') !== projectRef.current) return
          setPage(result)
          setOffset(nextOffset)
          setSubmitted({
            query: submittedQuery,
            limit: nextLimit,
            ...(nextProject ? { project: nextProject } : {}),
          })
        },
      },
    )
  }

  // Auto-execute a preloaded query exactly once (SavedQueriesPage Run links here).
  const autoRan = useRef(false)
  const executeMutate = execute.mutate
  useEffect(() => {
    if (autoRan.current) return
    if (!preloadProvider || !preloadQuery) return
    if (requiresProject && !project) return
    autoRan.current = true
    const autoProject = project || undefined
    executeMutate(
      {
        query: { provider: preloadProvider, query: preloadQuery, confidence: 1 },
        limit: 25,
        offset: 0,
        ...(autoProject ? { project: autoProject } : {}),
      },
      {
        onSuccess: (result) => {
          setPage(result)
          setSubmitted({
            query: { provider: preloadProvider, query: preloadQuery, confidence: 1 },
            limit: 25,
            ...(autoProject ? { project: autoProject } : {}),
          })
        },
      },
    )
  }, [preloadProvider, preloadQuery, project, requiresProject, executeMutate])

  function translateNow() {
    translate.mutate(
      { text: nlText, ...(project ? { project } : {}) },
      {
        onSuccess: (result) => {
          setProvider(result.provider)
          setQueryText(result.query)
          setConfidence(result.confidence)
        },
      },
    )
  }

  const projectSelected = !requiresProject || project !== ''
  const canExecute =
    provider.trim() !== '' && queryText.trim() !== '' && projectSelected && !execute.isPending
  const showQueryRow = mode === 'manual' || queryText !== '' || provider !== ''

  const items = page?.items ?? []
  const total = page && page.total > 0 ? page.total : undefined
  const pageLimit = submitted?.limit ?? limit
  const prevDisabled = offset === 0 || execute.isPending
  const nextDisabled =
    execute.isPending ||
    (total !== undefined ? offset + pageLimit >= total : items.length < pageLimit)
  const rangeCaption =
    items.length > 0
      ? `${offset + 1}–${offset + items.length}${total !== undefined ? ` of ${total}` : ''}`
      : 'No items'

  return (
    <section className="wi-page animate-enter">
      <header className="wi-toolbar glass-panel">
        {/* The topbar already renders the route-handle h1; this is a section label. */}
        <h2 className="wi-page-title">Ticket console</h2>
        <Link className="btn btn-ghost btn-sm" to="/work-items/saved">
          Saved queries
        </Link>
        {canMutate && (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!projectSelected}
            onClick={() => setCreating(true)}
          >
            New item
          </button>
        )}
      </header>

      <div className="wi-console glass-panel">
        {scopedProjects.length > 1 && (
          <label className="wi-field">
            <span className="wi-field-label">Project</span>
            <select
              className="field-select"
              aria-label="Work tracking project"
              value={project}
              onChange={(event) => setProject(event.target.value)}
            >
              <option value="">Select a project…</option>
              {scopedProjects.map((projectId) => (
                <option key={projectId} value={projectId}>
                  {projectId}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="wi-mode-toggle" role="group" aria-label="Query mode">
          <button
            type="button"
            className={`wi-tab${mode === 'nl' ? ' active' : ''}`}
            aria-pressed={mode === 'nl'}
            onClick={() => setMode('nl')}
          >
            Natural language
          </button>
          <button
            type="button"
            className={`wi-tab${mode === 'manual' ? ' active' : ''}`}
            aria-pressed={mode === 'manual'}
            onClick={() => {
              setMode('manual')
              setConfidence(null)
            }}
          >
            Manual query
          </button>
        </div>

        {mode === 'nl' && (
          <div className="wi-field">
            <label className="wi-field-label" htmlFor="wi-nl-query">
              Find by description
            </label>
            <div className="wi-row">
              <input
                id="wi-nl-query"
                className="field-input wi-grow"
                placeholder="open payment stories assigned to my team"
                value={nlText}
                onChange={(event) => setNlText(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    if (nlText.trim() && projectSelected && !translate.isPending) translateNow()
                  }
                }}
              />
              <button
                type="button"
                className="btn btn-secondary"
                disabled={nlText.trim() === '' || !projectSelected || translate.isPending}
                onClick={translateNow}
              >
                {translate.isPending ? 'Translating…' : 'Translate'}
              </button>
            </div>
            {translate.isError && (
              <p className="wi-caption wi-caption--danger" role="alert">
                Translate failed: {translate.error.message}
              </p>
            )}
          </div>
        )}

        {showQueryRow && (
          <div className="wi-field" data-testid="provider-query">
            <span className="wi-field-label">Provider query</span>
            <div className="wi-row">
              <input
                className="field-input wi-mono"
                aria-label="Provider"
                placeholder="jira"
                value={provider}
                onChange={(event) => setProvider(event.target.value)}
              />
              {confidence !== null && (
                <span
                  className={`topbar-meta-chip ${confidence >= 0.7 ? 'success' : 'warning'}`}
                  title="Translation confidence"
                >
                  confidence {Math.round(confidence * 100)}%
                </span>
              )}
            </div>
            <div className="wi-row">
              <input
                className="field-input wi-grow wi-mono"
                aria-label="Provider query"
                placeholder='project = PHX AND status = "Open"'
                value={queryText}
                onChange={(event) => setQueryText(event.target.value)}
              />
              <select
                className="field-select"
                aria-label="Page size"
                value={limit}
                onChange={(event) => setLimit(Number(event.target.value))}
              >
                {LIMIT_OPTIONS.map((option) => (
                  <option key={option} value={option}>
                    {option} / page
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!canExecute}
                onClick={() => runQuery({ provider, query: queryText, confidence: confidence ?? 1 })}
              >
                {execute.isPending ? 'Running…' : 'Execute'}
              </button>
              {canMutate && (
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={
                    provider.trim() === '' || queryText.trim() === '' || !projectSelected
                  }
                  onClick={() => setSaving(true)}
                >
                  Save query
                </button>
              )}
            </div>
          </div>
        )}

        {savedQueries.data && savedQueries.data.length > 0 && (
          <div className="wi-field">
            <label className="wi-field-label" htmlFor="wi-saved-query">
              Saved queries
            </label>
            <select
              id="wi-saved-query"
              className="field-select"
              disabled={!projectSelected || execute.isPending}
              value=""
              onChange={(event) => {
                const saved = savedQueries.data.find((entry) => entry.id === event.target.value)
                if (!saved) return
                const savedProject = (saved.project_id ?? project) || undefined
                if (saved.project_id) setProject(saved.project_id)
                setProvider(saved.provider)
                setQueryText(saved.query)
                setConfidence(null)
                runQuery({ provider: saved.provider, query: saved.query }, 0, limit, savedProject)
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
      </div>

      {execute.isError && (
        <div className="wi-inline-error" role="alert">
          <span>Query failed: {execute.error.message}</span>
        </div>
      )}

      {execute.isPending ? (
        <div className="wi-skeleton" role="status" aria-busy="true" aria-label="Running query">
          {Array.from({ length: 3 }, (_, i) => (
            <div key={i} className="glass-panel wi-skeleton-row" />
          ))}
        </div>
      ) : page === null ? (
        <div className="dash-empty">
          <h2>Run a query to see work items</h2>
          <p className="dash-empty-hint">
            Describe what you need and translate it, run a saved query, or write a provider query
            in manual mode.
          </p>
        </div>
      ) : items.length === 0 ? (
        <div className="dash-empty compact">
          <h2>No work items matched</h2>
          <p className="dash-empty-hint">Adjust the query and execute again.</p>
        </div>
      ) : (
        <>
          <ResultsTable
            items={items}
            provider={submitted?.query.provider ?? provider}
            project={submitted?.project}
          />
          <footer className="wi-pagination">
            <span className="wi-pagination-caption">{rangeCaption}</span>
            <div className="wi-pagination-buttons">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={prevDisabled}
                onClick={() =>
                  submitted &&
                  runQuery(
                    submitted.query,
                    Math.max(0, offset - submitted.limit),
                    submitted.limit,
                    submitted.project,
                  )
                }
              >
                Previous
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={nextDisabled}
                onClick={() =>
                  submitted &&
                  runQuery(
                    submitted.query,
                    offset + submitted.limit,
                    submitted.limit,
                    submitted.project,
                  )
                }
              >
                Next
              </button>
            </div>
          </footer>
        </>
      )}

      {saving && (
        <SaveQueryModal
          provider={provider}
          query={queryText}
          project={project || undefined}
          onClose={() => setSaving(false)}
        />
      )}
      {creating && (
        <NewItemModal
          provider={provider}
          project={project || undefined}
          onClose={() => setCreating(false)}
        />
      )}
    </section>
  )
}
