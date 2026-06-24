/**
 * /work-items/saved — saved provider queries (plan Part 2 route table).
 * Row actions: Run (everyone — navigates to the console with
 * ?provider=&query= search params, which auto-execute on mount), Edit (modal
 * PATCH) and Delete (confirm) for operator+.
 */
import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'

import {
  useDeleteSavedQuery,
  useSavedQueries,
  useUpdateSavedQuery,
  type SavedQuery,
} from '@/api/hooks/useWorkTracking'
import { useConsumer } from '@/auth/AuthProvider'
import { roleAtLeast } from '@/auth/RequireRole'
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
  const update = useUpdateSavedQuery()
  const [name, setName] = useState(saved.name)
  const [provider, setProvider] = useState(saved.provider)
  const [query, setQuery] = useState(saved.query)
  const [description, setDescription] = useState(saved.description ?? '')
  const canSubmit =
    name.trim() !== '' && provider.trim() !== '' && query.trim() !== '' && !update.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    update.mutate(
      {
        savedQueryId: saved.id,
        body: {
          name: name.trim(),
          provider: provider.trim(),
          query,
          description: description.trim() || null,
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
      closeOnBackdrop={!update.isPending}
      closeOnEscape={!update.isPending}
      panelAs="form"
      onSubmit={submit}
    >
      <h2 className="wi-modal-title">Edit saved query</h2>
        <label className="wi-field">
          <span className="wi-field-label">Name</span>
          <input
            type="text"
            className="field-input"
            aria-label="Query name"
            value={name}
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
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        {update.isError && (
          <div className="wi-inline-error" role="alert">
            <span>Update failed: {update.error.message}</span>
          </div>
        )}
        <div className="wi-modal-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onClose}
            disabled={update.isPending}
          >
            Cancel
          </button>
          <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
            {update.isPending ? 'Saving…' : 'Save changes'}
          </button>
        </div>
    </Dialog>
  )
}

/** Simple confirm dialog (operator+). */
function DeleteQueryModal({ saved, onClose }: { saved: SavedQuery; onClose: () => void }) {
  const remove = useDeleteSavedQuery()

  return (
    <Dialog
      overlayClassName="wi-overlay"
      className="wi-modal glass-panel"
      ariaLabel={`Delete saved query ${saved.name}`}
      onClose={onClose}
      closeOnBackdrop={!remove.isPending}
      closeOnEscape={!remove.isPending}
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
          disabled={remove.isPending}
        >
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-danger btn-sm"
          disabled={remove.isPending}
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
              label: 'Run',
              onSelect: () => void navigate(consolePath(saved.provider, saved.query)),
            },
            ...(canMutate
              ? [
                  { label: 'Edit…', onSelect: () => onEdit(saved) },
                  { label: 'Delete…', onSelect: () => onDelete(saved) },
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
      ) : savedQueries.isError ? (
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
                  canMutate={canMutate}
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
