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
  fetchWorkTrackingBinding,
  useCreateSavedQuery,
  useCreateWorkItem,
  useExecuteQuery,
  useSavedQueries,
  useTranslateQuery,
  createWorkItemMutationKey,
  type WorkItem,
  type WorkItemPage,
  type WorkTrackingBinding,
} from '@/api/hooks/useWorkTracking'
import { useConsumer } from '@/auth/AuthProvider'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import { canMutateAudience } from '@/auth/RequireRole'
import { Dialog } from '@/components/Dialog'

import { ExternalLink, KindChip, StatusBadge } from './workItemsBits'
import {
  bindDurableMutationDraft,
  clearDurableMutationDraft,
  initializeDurableMutationDraft,
  SafeRetryStorageError,
  scopedMutationStorageKey,
  stableMutationFingerprint,
  type DurableMutationDraft,
  updateDurableMutationDraft,
} from './durableMutationDraft'
import { workItemPath } from './workItemsLogic'
import './work-items.css'

const LIMIT_OPTIONS = [10, 25, 50]
const KIND_OPTIONS = ['story', 'task', 'bug', 'epic']
const WORK_ITEMS_MAX_WINDOW = 1_000

type ConsoleMode = 'nl' | 'manual'

interface NewItemDraftState {
  title: string
  kind: string
  description: string
}

function isNewItemDraftState(value: unknown): value is NewItemDraftState {
  if (!value || typeof value !== 'object') return false
  const draft = value as Partial<NewItemDraftState>
  return (
    typeof draft.title === 'string' &&
    typeof draft.kind === 'string' &&
    typeof draft.description === 'string'
  )
}

function newItemFingerprint(draft: NewItemDraftState): string {
  return stableMutationFingerprint({
    title: draft.title.trim(),
    kind: draft.kind,
    description: draft.description,
  })
}

/** Name + description modal -> POST /v1/work-tracking/saved-queries (operator+). */
function SaveQueryModal({
  provider,
  query,
  project,
  connectionId,
  onClose,
}: {
  provider: string
  query: string
  project?: string
  connectionId?: string
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
        ...(connectionId ? { connection_id: connectionId } : {}),
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
  connectionId,
  onClose,
}: {
  provider: string
  project?: string
  connectionId: string
  onClose: () => void
}) {
  const navigate = useNavigate()
  const create = useCreateWorkItem()
  const mountedRef = useRef(true)
  const storageKey = scopedMutationStorageKey(
    'apex.work-items.create.v2',
    project,
    connectionId,
  )
  const [attempt, setAttempt] = useState(() =>
    initializeDurableMutationDraft(
      storageKey,
      { title: '', kind: 'story', description: '' },
      isNewItemDraftState,
      newItemFingerprint,
      () => createWorkItemMutationKey('create'),
    ),
  )
  const [safeRetryError, setSafeRetryError] = useState<string | null>(null)
  const { title, kind, description } = attempt.draft
  const canSubmit = title.trim() !== '' && !create.isPending && safeRetryError === null

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  function updateDraft(changes: Partial<NewItemDraftState>) {
    if (create.isPending) return
    setSafeRetryError(null)
    setAttempt((previous) =>
      updateDurableMutationDraft(
        storageKey,
        previous,
        { ...previous.draft, ...changes },
        newItemFingerprint,
        () => createWorkItemMutationKey('create'),
      ),
    )
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    let submittedAttempt: DurableMutationDraft<NewItemDraftState>
    try {
      submittedAttempt = bindDurableMutationDraft(storageKey, attempt, newItemFingerprint)
    } catch (error) {
      if (error instanceof SafeRetryStorageError) {
        setSafeRetryError(error.message)
        return
      }
      throw error
    }
    setAttempt(submittedAttempt)
    const keyRevision = getApiKeyRevision()
    const sessionRevision = getSessionRevision()
    try {
      const item = await create.mutateAsync({
        body: { title: title.trim(), kind, description },
        connectionId,
        idempotencyKey: submittedAttempt.idempotencyKey,
        ...(project ? { project } : {}),
      })
      // This continuation belongs to the request promise, not the mounted
      // MutationObserver, so successful attempts are retired after route
      // unmounts as well.
      clearDurableMutationDraft(storageKey, submittedAttempt.idempotencyKey)
      if (
        mountedRef.current &&
        keyRevision === getApiKeyRevision() &&
        sessionRevision === getSessionRevision()
      ) {
        void navigate(
          workItemPath(item.provider || provider, item.key, project, item.connection_id),
        )
      }
    } catch {
      // The mutation's mounted observer renders the request error inline.
    }
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
          disabled={create.isPending}
          onChange={(event) => updateDraft({ title: event.target.value })}
        />
      </label>
      <label className="wi-field">
        <span className="wi-field-label">Kind</span>
        <select
          className="field-select"
          aria-label="Item kind"
          value={kind}
          disabled={create.isPending}
          onChange={(event) => updateDraft({ kind: event.target.value })}
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
          disabled={create.isPending}
          onChange={(event) => updateDraft({ description: event.target.value })}
        />
      </label>
      {create.isError && (
        <div className="wi-inline-error" role="alert">
          <span>Create failed: {create.error.message}</span>
        </div>
      )}
      {safeRetryError && (
        <div className="wi-inline-error" role="alert">
          <span>Create blocked: {safeRetryError}</span>
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
  connectionId,
}: {
  items: WorkItem[]
  provider: string
  project?: string
  connectionId: string
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
                <Link
                  className="wi-key-link"
                  to={workItemPath(provider, item.key, project, connectionId)}
                >
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

  const preloadProvider = searchParams.get('provider')
  const preloadQuery = searchParams.get('query')
  const preloadProject = searchParams.get('project')
  const preloadConnectionId = searchParams.get('connection_id')
  const scopedProjects = Array.from(
    new Set((consumer?.scopes ?? []).map((scope) => scope.project_id)),
  ).sort()
  const requiresProject = scopedProjects.length > 1
  const defaultProject = scopedProjects.length === 1 ? scopedProjects[0] : undefined
  const validPreloadProject =
    preloadProject && (scopedProjects.length === 0 || scopedProjects.includes(preloadProject))
      ? preloadProject
      : undefined
  const routeProject = validPreloadProject ?? defaultProject ?? ''
  const routeIdentity = JSON.stringify([
    preloadProvider ?? '',
    preloadQuery ?? '',
    preloadProject ?? '',
    preloadConnectionId ?? '',
    routeProject,
  ])

  const [mode, setMode] = useState<ConsoleMode>(preloadQuery ? 'manual' : 'nl')
  const [nlText, setNlText] = useState('')
  const [provider, setProvider] = useState(preloadProvider ?? '')
  const [queryText, setQueryText] = useState(preloadQuery ?? '')
  const [confidence, setConfidence] = useState<number | null>(null)
  const [editorConnectionId, setEditorConnectionId] = useState<string | null>(
    preloadConnectionId,
  )
  const [project, setProject] = useState(routeProject)
  const canMutate = canMutateAudience(consumer, project || null, null)
  const [limit, setLimit] = useState(25)
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<WorkItemPage | null>(null)
  const [submitted, setSubmitted] = useState<{
    query: { provider: string; query: string; confidence: number }
    limit: number
    project?: string
    connectionId?: string
  } | null>(null)
  const [availableBinding, setAvailableBinding] = useState<WorkTrackingBinding | null>(null)
  const [saving, setSaving] = useState(false)
  const [creating, setCreating] = useState(false)
  const projectRef = useRef(project)
  projectRef.current = project
  const requestGenerationRef = useRef(0)
  const activeRouteIdentityRef = useRef(routeIdentity)
  activeRouteIdentityRef.current = routeIdentity
  const hydratedRouteIdentityRef = useRef(routeIdentity)
  const queryOperationRef = useRef(0)
  const pendingSavedRunRef = useRef<{
    provider: string
    query: string
    project: string
    connectionId?: string
  } | null>(null)
  const bindingRequestRef = useRef(0)

  const translate = useTranslateQuery()
  const execute = useExecuteQuery()
  const savedQueries = useSavedQueries()
  const resetTranslate = translate.reset
  const resetExecute = execute.reset

  // React Router keeps this page mounted when only search params change. Treat
  // each deep-linked provider/query/project tuple as a fresh console session,
  // and invalidate callbacks from requests started for the previous URL.
  useEffect(() => {
    if (hydratedRouteIdentityRef.current === routeIdentity) return
    hydratedRouteIdentityRef.current = routeIdentity
    requestGenerationRef.current += 1
    queryOperationRef.current += 1
    projectRef.current = routeProject
    pendingSavedRunRef.current = null
    setProject(routeProject)
    setMode(preloadQuery ? 'manual' : 'nl')
    setNlText('')
    setProvider(preloadProvider ?? '')
    setQueryText(preloadQuery ?? '')
    setConfidence(null)
    setEditorConnectionId(preloadConnectionId)
    setAvailableBinding(null)
    setLimit(25)
    setPage(null)
    setSubmitted(null)
    setOffset(0)
    setSaving(false)
    setCreating(false)
    resetTranslate()
    resetExecute()
  }, [
    routeIdentity,
    routeProject,
    preloadProvider,
    preloadQuery,
    preloadConnectionId,
    resetTranslate,
    resetExecute,
  ])

  function changeProject(nextProject: string) {
    if (nextProject === projectRef.current) return
    requestGenerationRef.current += 1
    queryOperationRef.current += 1
    projectRef.current = nextProject
    setProject(nextProject)
    setPage(null)
    setSubmitted(null)
    setOffset(0)
    setProvider('')
    setQueryText('')
    setConfidence(null)
    setEditorConnectionId(null)
    setAvailableBinding(null)
    setSaving(false)
    setCreating(false)
    resetTranslate()
    resetExecute()
  }

  useEffect(() => {
    const requestId = ++bindingRequestRef.current
    const requestProject = project || undefined
    if (requiresProject && !requestProject) {
      setAvailableBinding(null)
      return
    }
    const routeConnectionId = project === routeProject ? preloadConnectionId : null
    void fetchWorkTrackingBinding({
      ...(requestProject ? { project: requestProject } : {}),
      ...(routeConnectionId ? { connectionId: routeConnectionId } : {}),
    })
      .then((resolved) => {
        if (
          requestId !== bindingRequestRef.current ||
          requestProject !== (projectRef.current || undefined)
        )
          return
        setAvailableBinding(resolved)
      })
      .catch(() => {
        if (requestId === bindingRequestRef.current) setAvailableBinding(null)
      })
  }, [project, preloadConnectionId, requiresProject, routeProject])

  function runQuery(
    next: { provider: string; query: string; confidence?: number },
    nextOffset = 0,
    nextLimit = limit,
    nextProject = project || undefined,
    nextConnectionId?: string,
  ) {
    if (nextConnectionId) bindingRequestRef.current += 1
    const operationId = ++queryOperationRef.current
    const requestProject = nextProject
    const requestGeneration = requestGenerationRef.current
    const requestRouteIdentity = activeRouteIdentityRef.current
    const submittedQuery = {
      provider: next.provider.trim(),
      query: next.query.trim(),
      confidence: next.confidence ?? 1,
    }
    const nextSubmission = {
      query: submittedQuery,
      limit: nextLimit,
      ...(nextProject ? { project: nextProject } : {}),
      ...(nextConnectionId ? { connectionId: nextConnectionId } : {}),
    }
    if (nextOffset === 0) {
      // A newly executed query owns a new result set. Retire the previous
      // table before the request starts so a failed replacement cannot leave
      // unrelated rows displayed under the edited query controls.
      setPage(null)
      setOffset(0)
      setSubmitted(nextSubmission)
    }
    execute.mutate(
      {
        query: submittedQuery,
        limit: nextLimit,
        offset: nextOffset,
        ...(nextProject ? { project: nextProject } : {}),
        ...(nextConnectionId ? { connectionId: nextConnectionId } : {}),
      },
      {
        onSuccess: (result) => {
          if (
            operationId !== queryOperationRef.current ||
            requestGeneration !== requestGenerationRef.current ||
            requestRouteIdentity !== activeRouteIdentityRef.current ||
            (requestProject ?? '') !== projectRef.current
          )
            return
          setPage(result)
          setOffset(nextOffset)
          bindingRequestRef.current += 1
          setEditorConnectionId(result.connection_id)
          setAvailableBinding({
            connection_id: result.connection_id,
            provider: result.provider,
          })
          setSubmitted({
            ...nextSubmission,
            connectionId: result.connection_id,
          })
        },
      },
    )
  }

  const runQueryRef = useRef(runQuery)
  runQueryRef.current = runQuery
  useEffect(() => {
    const pending = pendingSavedRunRef.current
    if (!pending || pending.project !== project) return
    pendingSavedRunRef.current = null
    setProvider(pending.provider)
    setQueryText(pending.query)
    setConfidence(null)
    setEditorConnectionId(pending.connectionId ?? null)
    runQueryRef.current(
      { provider: pending.provider, query: pending.query },
      0,
      limit,
      project,
      pending.connectionId,
    )
  }, [project, limit])

  // Auto-execute once per URL identity (SavedQueriesPage Run links here and
  // browser back/forward can switch between multiple saved-query URLs).
  const autoRanIdentityRef = useRef('')
  useEffect(() => {
    if (autoRanIdentityRef.current === routeIdentity) return
    if (!preloadProvider || !preloadQuery) {
      autoRanIdentityRef.current = routeIdentity
      return
    }
    if (project !== projectRef.current) return
    if (requiresProject && !project) return
    autoRanIdentityRef.current = routeIdentity
    setMode('manual')
    setProvider(preloadProvider)
    setQueryText(preloadQuery)
    setConfidence(null)
    runQueryRef.current(
      { provider: preloadProvider, query: preloadQuery, confidence: 1 },
      0,
      25,
      project || undefined,
      preloadConnectionId || undefined,
    )
  }, [
    routeIdentity,
    routeProject,
    preloadProvider,
    preloadQuery,
    preloadConnectionId,
    project,
    requiresProject,
  ])

  function translateNow() {
    const operationId = ++queryOperationRef.current
    const requestProject = project || undefined
    const requestGeneration = requestGenerationRef.current
    const requestRouteIdentity = activeRouteIdentityRef.current
    setProvider('')
    setQueryText('')
    setConfidence(null)
    setEditorConnectionId(null)
    setPage(null)
    setSubmitted(null)
    setOffset(0)
    translate.mutate(
      {
        text: nlText,
        ...(availableBinding ? { connectionId: availableBinding.connection_id } : {}),
        ...(requestProject ? { project: requestProject } : {}),
      },
      {
        onSuccess: (result) => {
          if (
            operationId !== queryOperationRef.current ||
            requestGeneration !== requestGenerationRef.current ||
            requestRouteIdentity !== activeRouteIdentityRef.current ||
            requestProject !== (projectRef.current || undefined)
          )
            return
          setProvider(result.provider)
          setQueryText(result.query)
          setConfidence(result.confidence)
          bindingRequestRef.current += 1
          setEditorConnectionId(result.connection_id)
          setAvailableBinding({
            connection_id: result.connection_id,
            provider: result.provider,
          })
        },
      },
    )
  }

  const projectSelected = !requiresProject || project !== ''
  const canExecute =
    provider.trim() !== '' &&
    queryText.trim() !== '' &&
    projectSelected &&
    !translate.isPending &&
    !execute.isPending
  const showQueryRow = mode === 'manual' || queryText !== '' || provider !== ''
  const compatibleConnectionId =
    editorConnectionId ??
    (availableBinding?.provider.toLowerCase() === provider.trim().toLowerCase()
      ? availableBinding.connection_id
      : undefined)
  const saveConnectionId =
    page?.provider.toLowerCase() === provider.trim().toLowerCase()
      ? page.connection_id
      : undefined
  const canSaveCurrentQuery = !project || Boolean(saveConnectionId)
  const normalizedProvider = provider.trim().toLowerCase()
  const validatedEditorBinding =
    editorConnectionId &&
    availableBinding?.connection_id === editorConnectionId &&
    (!normalizedProvider ||
      availableBinding.provider.toLowerCase() === normalizedProvider)
      ? availableBinding
      : null
  const mutationBinding =
    validatedEditorBinding ??
    (page &&
          (!normalizedProvider || page.provider.toLowerCase() === normalizedProvider)
      ? { connection_id: page.connection_id, provider: page.provider }
        : availableBinding &&
            (!normalizedProvider ||
              availableBinding.provider.toLowerCase() === normalizedProvider)
          ? availableBinding
          : null)
  const mutationBindingIdentity = mutationBinding
    ? `${mutationBinding.provider.toLowerCase()}:${mutationBinding.connection_id}`
    : ''

  useEffect(() => {
    setCreating(false)
  }, [mutationBindingIdentity, project])

  const items = page?.items ?? []
  const total = page && page.total > 0 ? page.total : undefined
  const pageLimit = submitted?.limit ?? limit
  const prevDisabled = offset === 0 || execute.isPending
  const nextDisabled =
    execute.isPending ||
    offset + pageLimit >= WORK_ITEMS_MAX_WINDOW ||
    (total !== undefined ? offset + pageLimit >= total : items.length < pageLimit)
  const reachedResultWindow =
    offset + pageLimit >= WORK_ITEMS_MAX_WINDOW &&
    total !== undefined &&
    offset + items.length < total
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
            disabled={!projectSelected || !mutationBinding}
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
              onChange={(event) => changeProject(event.target.value)}
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
            disabled={translate.isPending || execute.isPending}
            onClick={() => setMode('nl')}
          >
            Natural language
          </button>
          <button
            type="button"
            className={`wi-tab${mode === 'manual' ? ' active' : ''}`}
            aria-pressed={mode === 'manual'}
            disabled={translate.isPending || execute.isPending}
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
                disabled={translate.isPending || execute.isPending}
                onChange={(event) => setNlText(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    if (
                      nlText.trim() &&
                      projectSelected &&
                      !translate.isPending &&
                      !execute.isPending
                    ) {
                      translateNow()
                    }
                  }
                }}
              />
              <button
                type="button"
                className="btn btn-secondary"
                disabled={
                  nlText.trim() === '' ||
                  !projectSelected ||
                  translate.isPending ||
                  execute.isPending
                }
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
                disabled={translate.isPending || execute.isPending}
                onChange={(event) => {
                  const nextProvider = event.target.value
                  setProvider(nextProvider)
                  if (
                    availableBinding?.provider.toLowerCase() !==
                    nextProvider.trim().toLowerCase()
                  ) {
                    setEditorConnectionId(null)
                  }
                }}
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
                disabled={translate.isPending || execute.isPending}
                onChange={(event) => setQueryText(event.target.value)}
              />
              <select
                className="field-select"
                aria-label="Page size"
                value={limit}
                disabled={translate.isPending || execute.isPending}
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
                onClick={() =>
                  runQuery({
                    provider,
                    query: queryText,
                    confidence: confidence ?? 1,
                  }, 0, limit, project || undefined, compatibleConnectionId)
                }
              >
                {execute.isPending ? 'Running…' : 'Execute'}
              </button>
              {canMutate && (
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={
                    provider.trim() === '' ||
                    queryText.trim() === '' ||
                    !projectSelected ||
                    !canSaveCurrentQuery ||
                    translate.isPending ||
                    execute.isPending
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
              disabled={!projectSelected || translate.isPending || execute.isPending}
              value=""
              onChange={(event) => {
                const saved = savedQueries.data.find((entry) => entry.id === event.target.value)
                if (!saved) return
                if (saved.project_id && !saved.connection_id) return
                if (saved.connection_id) bindingRequestRef.current += 1
                const savedProject = (saved.project_id ?? project) || undefined
                if (saved.project_id && saved.project_id !== project) {
                  pendingSavedRunRef.current = {
                    provider: saved.provider,
                    query: saved.query,
                    project: saved.project_id,
                    ...(saved.connection_id
                      ? { connectionId: saved.connection_id }
                      : {}),
                  }
                  changeProject(saved.project_id)
                  return
                }
                setProvider(saved.provider)
                setQueryText(saved.query)
                setConfidence(null)
                setEditorConnectionId(saved.connection_id ?? null)
                runQuery(
                  { provider: saved.provider, query: saved.query },
                  0,
                  limit,
                  savedProject,
                  saved.connection_id ?? undefined,
                )
              }}
            >
              <option value="">Run a saved query…</option>
              {savedQueries.data.map((saved) => (
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
      ) : page === null && !execute.isError ? (
        <div className="dash-empty">
          <h2>Run a query to see work items</h2>
          <p className="dash-empty-hint">
            Describe what you need and translate it, run a saved query, or write a provider query in
            manual mode.
          </p>
        </div>
      ) : page !== null && items.length === 0 ? (
        <div className="dash-empty compact">
          <h2>No work items matched</h2>
          <p className="dash-empty-hint">Adjust the query and execute again.</p>
        </div>
      ) : page !== null ? (
        <>
          <ResultsTable
            items={items}
            provider={page.provider}
            project={submitted?.project}
            connectionId={page.connection_id}
          />
          {reachedResultWindow && (
            <p className="wi-result-window-note" role="status">
              Reached the provider result-window limit. Refine the query to inspect later items.
            </p>
          )}
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
                    submitted.connectionId,
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
                    submitted.connectionId,
                  )
                }
              >
                Next
              </button>
            </div>
          </footer>
        </>
      ) : null}

      {saving && (
        <SaveQueryModal
          provider={provider}
          query={queryText}
          project={project || undefined}
          connectionId={project ? saveConnectionId : undefined}
          onClose={() => setSaving(false)}
        />
      )}
      {creating && (
        <NewItemModal
          provider={mutationBinding!.provider}
          project={project || undefined}
          connectionId={mutationBinding!.connection_id}
          onClose={() => setCreating(false)}
        />
      )}
    </section>
  )
}
