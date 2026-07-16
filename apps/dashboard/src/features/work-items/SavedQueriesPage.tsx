/**
 * /work-items/saved — saved provider queries (plan Part 2 route table).
 * Row actions: Run (everyone — navigates to the console with
 * ?provider=&query= search params, which auto-execute on mount), Edit (modal
 * PATCH) and Delete (confirm) for operator+.
 */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'

import {
  fetchWorkTrackingBinding,
  savedQueryWriteMutationKey,
  useDeleteSavedQuery,
  useSavedQueries,
  useUpdateSavedQuery,
  type SavedQuery,
} from '@/api/hooks/useWorkTracking'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { useConsumer } from '@/auth/AuthProvider'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import { hasFullProjectScope, isGlobalAdmin, roleAtLeast } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'
import { OverflowMenu } from '@/features/runs/PreflightModal'
import { formatRelative } from '@/utils/time'

import { consolePath } from './workItemsLogic'
import './work-items.css'

const EM_DASH = '—'
const SKELETON_ROWS = 4

/** Edit modal — PATCH name/provider/query/description. */
function EditQueryModal({ saved, onClose }: { saved: SavedQuery; onClose: () => void }) {
  const update = useUpdateSavedQuery(saved.id)
  const writeCount = usePendingMutationCount(savedQueryWriteMutationKey(saved.id))
  const writePending = writeCount > 0
  const mountedRef = useRef(true)
  const bindingOperationRef = useRef(0)
  const [name, setName] = useState(saved.name)
  const [provider, setProvider] = useState(saved.provider)
  const [query, setQuery] = useState(saved.query)
  const [description, setDescription] = useState(saved.description ?? '')
  const providerLocked = Boolean(saved.project_id)
  const needsBinding = Boolean(saved.project_id && !saved.connection_id)
  const [bindingState, setBindingState] = useState<{
    pending: boolean
    error: string | null
  }>({ pending: false, error: null })
  const canSubmit =
    name.trim() !== '' &&
    provider.trim() !== '' &&
    query.trim() !== '' &&
    !bindingState.pending &&
    !writePending
  const operationPending = bindingState.pending || writePending

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      bindingOperationRef.current += 1
    }
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    let connectionId = saved.connection_id
    if (needsBinding) {
      const operation = ++bindingOperationRef.current
      const keyRevision = getApiKeyRevision()
      const sessionRevision = getSessionRevision()
      const operationIsCurrent = () =>
        mountedRef.current &&
        operation === bindingOperationRef.current &&
        keyRevision === getApiKeyRevision() &&
        sessionRevision === getSessionRevision()
      setBindingState({ pending: true, error: null })
      try {
        const binding = await fetchWorkTrackingBinding({
          project: saved.project_id ?? undefined,
        })
        if (!operationIsCurrent()) return
        if (binding.provider.toLowerCase() !== saved.provider.toLowerCase()) {
          setBindingState({
            pending: false,
            error: `The current project connection uses ${binding.provider}, not ${saved.provider}.`,
          })
          return
        }
        connectionId = binding.connection_id
      } catch (error) {
        if (!operationIsCurrent()) return
        setBindingState({
          pending: false,
          error:
            error instanceof Error
              ? error.message
              : 'The project connection could not be resolved.',
        })
        return
      }
      if (!operationIsCurrent()) return
      setBindingState({ pending: false, error: null })
    }
    update.mutate(
      {
        savedQueryId: saved.id,
        body: {
          name: name.trim(),
          ...(!providerLocked ? { provider: provider.trim() } : {}),
          query,
          description: description.trim() || null,
          ...(needsBinding && connectionId ? { connection_id: connectionId } : {}),
        },
      },
      { onSuccess: onClose },
    )
  }

  return (
    <Dialog
      overlayClassName="wi-overlay"
      className="wi-modal glass-panel"
      ariaLabel={`Edit saved query ${saved.name}`}
      onClose={onClose}
      closeOnBackdrop={!operationPending}
      closeOnEscape={!operationPending}
      panelAs="form"
      onSubmit={submit}
    >
      <h2 className="wi-modal-title">Edit saved query</h2>
        {needsBinding && (
          <p className="wi-modal-caption">
            This legacy query is not bound to a connection. Saving will rebind it to the
            project’s current {saved.provider} connection.
          </p>
        )}
        <label className="wi-field">
          <span className="wi-field-label">Name</span>
          <input
            type="text"
            className="field-input"
            aria-label="Query name"
            value={name}
            disabled={operationPending}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="wi-field">
          <span className="wi-field-label">Provider</span>
          <input
            type="text"
            className="field-input wi-mono"
            aria-label="Query provider"
            value={provider}
            disabled={providerLocked || operationPending}
            onChange={(event) => setProvider(event.target.value)}
          />
        </label>
        <label className="wi-field">
          <span className="wi-field-label">Query</span>
          <textarea
            className="field-input wi-json-input"
            aria-label="Query text"
            rows={3}
            spellCheck={false}
            value={query}
            disabled={operationPending}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <label className="wi-field">
          <span className="wi-field-label">Description</span>
          <textarea
            className="field-input"
            aria-label="Query description"
            rows={2}
            value={description}
            disabled={operationPending}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        {update.isError && (
          <div className="wi-inline-error" role="alert">
            <span>Update failed: {update.error.message}</span>
          </div>
        )}
        {bindingState.error && (
          <div className="wi-inline-error" role="alert">
            <span>Rebind failed: {bindingState.error}</span>
          </div>
        )}
        <div className="wi-modal-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onClose}
            disabled={operationPending}
          >
            Cancel
          </button>
          <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
            {bindingState.pending
              ? 'Resolving connection…'
              : update.isPending
                ? 'Saving…'
                : needsBinding
                  ? 'Rebind and save'
                  : 'Save changes'}
          </button>
        </div>
    </Dialog>
  )
}

/** Simple confirm dialog (operator+). */
function DeleteQueryModal({ saved, onClose }: { saved: SavedQuery; onClose: () => void }) {
  const remove = useDeleteSavedQuery(saved.id)
  const writeCount = usePendingMutationCount(savedQueryWriteMutationKey(saved.id))
  const writePending = writeCount > 0

  return (
    <Dialog
      overlayClassName="wi-overlay"
      className="wi-modal glass-panel"
      ariaLabel={`Delete saved query ${saved.name}`}
      onClose={onClose}
      closeOnBackdrop={!writePending}
      closeOnEscape={!writePending}
    >
      <h2 className="wi-modal-title">Delete saved query</h2>
      <p className="wi-modal-caption">
        This permanently removes <strong>{saved.name}</strong>. Runs that used it are unaffected.
      </p>
      {remove.isError && (
        <div className="wi-inline-error" role="alert">
          <span>Delete failed: {remove.error.message}</span>
        </div>
      )}
      <div className="wi-modal-actions">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onClose}
          disabled={writePending}
        >
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-danger btn-sm"
          disabled={writePending}
          onClick={() => remove.mutate(saved.id, { onSuccess: onClose })}
        >
          {remove.isPending ? 'Deleting…' : 'Delete query'}
        </button>
      </div>
    </Dialog>
  )
}

function SavedQueryRow({
  saved,
  canMutate,
  onEdit,
  onDelete,
}: {
  saved: SavedQuery
  canMutate: boolean
  onEdit: (saved: SavedQuery) => void
  onDelete: (saved: SavedQuery) => void
}) {
  const navigate = useNavigate()
  const needsBinding = Boolean(saved.project_id && !saved.connection_id)
  const writeCount = usePendingMutationCount(savedQueryWriteMutationKey(saved.id))
  const writePending = writeCount > 0

  return (
    <tr data-testid={`saved-query-row-${saved.id}`}>
      <td className="strong">{saved.name}</td>
      <td>
        <span className="dash-context-chip">{saved.provider}</span>
      </td>
      <td>
        <span className="wi-query-cell" title={saved.query}>
          {saved.query}
        </span>
      </td>
      <td>
        {saved.description ? (
          <span className="wi-desc-cell" title={saved.description}>
            {saved.description}
          </span>
        ) : (
          <span className="wi-muted">{EM_DASH}</span>
        )}
      </td>
      <td className="wi-time" title={saved.updated_at ?? undefined}>
        {formatRelative(saved.updated_at)}
      </td>
      <td className="wi-actions-cell">
        <OverflowMenu
          label={`Saved query actions: ${saved.name}`}
          items={[
            {
              label: needsBinding ? 'Run (rebind required)' : 'Run',
              disabled: needsBinding || writePending,
              onSelect: () =>
                void navigate(
                  consolePath(
                    saved.provider,
                    saved.query,
                    saved.project_id,
                    saved.connection_id,
                  ),
                ),
            },
            ...(canMutate
              ? [
                  {
                    label: needsBinding ? 'Rebind…' : 'Edit…',
                    disabled: writePending,
                    onSelect: () => onEdit(saved),
                  },
                  { label: 'Delete…', disabled: writePending, onSelect: () => onDelete(saved) },
                ]
              : []),
          ]}
        />
      </td>
    </tr>
  )
}

export function SavedQueriesPage() {
  const savedQueries = useSavedQueries()
  const consumer = useConsumer()
  const canMutate = consumer ? roleAtLeast(consumer.role, 'operator') : false

  function canMutateRow(saved: SavedQuery): boolean {
    if (!consumer || !canMutate) return false
    if (!saved.project_id) return isGlobalAdmin(consumer)
    return hasFullProjectScope(consumer, saved.project_id)
  }

  const [editing, setEditing] = useState<SavedQuery | null>(null)
  const [deleting, setDeleting] = useState<SavedQuery | null>(null)

  const items = savedQueries.data ?? []

  return (
    <section className="wi-page animate-enter">
      <header className="wi-toolbar glass-panel">
        <h2 className="wi-page-title">Saved queries</h2>
        <Link className="btn btn-ghost btn-sm" to="/work-items">
          Query console
        </Link>
      </header>

      {savedQueries.isError && savedQueries.data && (
        <CachedDataWarning
          error={savedQueries.error}
          onRetry={() => void savedQueries.refetch()}
        />
      )}

      {savedQueries.isPending ? (
        <div
          className="wi-skeleton"
          role="status"
          aria-busy="true"
          aria-label="Loading saved queries"
        >
          {Array.from({ length: SKELETON_ROWS }, (_, i) => (
            <div key={i} className="glass-panel wi-skeleton-row" />
          ))}
        </div>
      ) : savedQueries.isError && !savedQueries.data ? (
        <ProblemCard
          title="Saved queries unavailable"
          message={savedQueries.error.message}
          onRetry={() => void savedQueries.refetch()}
        />
      ) : items.length === 0 ? (
        <div className="dash-empty">
          <h2>No saved queries yet</h2>
          <p className="dash-empty-hint">
            Save a provider query from the console to reuse it here and in the run wizard.
          </p>
          <Link className="btn btn-secondary" to="/work-items">
            Open the console
          </Link>
        </div>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table striped">
            <thead>
              <tr>
                <th>Name</th>
                <th>Provider</th>
                <th>Query</th>
                <th>Description</th>
                <th>Updated</th>
                <th className="wi-actions-cell">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((saved) => (
                <SavedQueryRow
                  key={saved.id}
                  saved={saved}
                  canMutate={canMutateRow(saved)}
                  onEdit={setEditing}
                  onDelete={setDeleting}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && <EditQueryModal saved={editing} onClose={() => setEditing(null)} />}
      {deleting && <DeleteQueryModal saved={deleting} onClose={() => setDeleting(null)} />}
    </section>
  )
}
