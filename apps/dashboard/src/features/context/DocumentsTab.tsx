/**
 * Documents tab — filtered list (?project, q committed on submit) over
 * /v1/documents, multipart upload (D4's useUploadDocument reused) and
 * delete with confirm (operator+).
 */
import { useRef, useState, type FormEvent } from 'react'

import {
  useDeleteDocument,
  useDocumentsList,
  useUploadDocument,
  type DocumentOut,
} from '@/api/hooks/useDocuments'
import { useConsumer } from '@/auth/AuthProvider'
import { canMutateAudience } from '@/auth/RequireRole'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'
import { formatRelative } from '@/utils/time'

import { formatBytes } from './contextLogic'

const EM_DASH = '—'
const SKELETON_ROWS = 3

/** Confirm dialog -> DELETE /v1/documents/{id} (operator+). */
function DeleteDocumentModal({ doc, onClose }: { doc: DocumentOut; onClose: () => void }) {
  const remove = useDeleteDocument()

  return (
    <Dialog
      overlayClassName="ctx-overlay"
      className="ctx-modal glass-panel"
      ariaLabel={`Delete document ${doc.name}`}
      onClose={onClose}
      closeOnBackdrop={!remove.isPending}
      closeOnEscape={!remove.isPending}
    >
      <h2 className="ctx-modal-title">Delete document</h2>
        <p className="ctx-modal-caption">
          This permanently removes <strong>{doc.name}</strong> ({formatBytes(doc.size_bytes)}).
          Runs that already consumed it keep their copies.
        </p>
        {remove.isError && (
          <div className="ctx-inline-error" role="alert">
            <span>Delete failed: {remove.error.message}</span>
          </div>
        )}
        <div className="ctx-actions">
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
            onClick={() => remove.mutate(doc.id, { onSuccess: onClose })}
          >
            {remove.isPending ? 'Deleting…' : 'Delete document'}
          </button>
        </div>
    </Dialog>
  )
}

export function DocumentsTab() {
  const consumer = useConsumer()

  // Draft inputs commit on submit so the list query keys stay stable while typing.
  const [projectDraft, setProjectDraft] = useState('')
  const [qDraft, setQDraft] = useState('')
  const [filters, setFilters] = useState<{ project?: string; q?: string }>({})

  const documents = useDocumentsList(filters.project, filters.q)
  const upload = useUploadDocument()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<DocumentOut | null>(null)
  const canUpload = canMutateAudience(consumer, projectDraft.trim() || null, null)

  function applyFilters(event: FormEvent) {
    event.preventDefault()
    setFilters({
      project: projectDraft.trim() || undefined,
      q: qDraft.trim() || undefined,
    })
  }

  async function uploadFiles(files: FileList) {
    setUploadError(null)
    for (const file of Array.from(files)) {
      try {
        // Upload follows the project currently visible in the filter input,
        // even before the user presses Apply; never reuse the prior query key.
        await upload.mutateAsync({ file, projectId: projectDraft.trim() || undefined })
      } catch (error) {
        setUploadError(error instanceof Error ? error.message : `Upload of ${file.name} failed`)
      }
    }
  }

  const items = documents.data ?? []

  return (
    <>
      <form className="ctx-toolbar glass-panel" aria-label="Document filters" onSubmit={applyFilters}>
        <input
          type="text"
          className="field-input"
          aria-label="Filter by project"
          placeholder="Project (proj-alpha)"
          value={projectDraft}
          onChange={(event) => setProjectDraft(event.target.value)}
        />
        <input
          type="search"
          className="field-input ctx-grow"
          aria-label="Search documents"
          placeholder="Search by name…"
          value={qDraft}
          onChange={(event) => setQDraft(event.target.value)}
        />
        <button type="submit" className="btn btn-secondary btn-sm">
          Apply
        </button>
        {canUpload && (
          <>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={upload.isPending}
              onClick={() => inputRef.current?.click()}
            >
              {upload.isPending ? 'Uploading…' : 'Upload'}
            </button>
            <input
              ref={inputRef}
              type="file"
              multiple
              aria-label="Upload documents"
              className="ctx-file-input"
              onChange={(event) => {
                if (event.target.files && event.target.files.length > 0) {
                  void uploadFiles(event.target.files)
                  event.target.value = ''
                }
              }}
            />
          </>
        )}
      </form>

      {uploadError && (
        <div className="ctx-inline-error" role="alert">
          <span>{uploadError}</span>
        </div>
      )}

      {documents.isPending ? (
        <div className="ctx-skeleton" role="status" aria-busy="true" aria-label="Loading documents">
          {Array.from({ length: SKELETON_ROWS }, (_, i) => (
            <div key={i} className="glass-panel ctx-skeleton-row" />
          ))}
        </div>
      ) : documents.isError ? (
        <ProblemCard
          title="Documents unavailable"
          message={documents.error.message}
          onRetry={() => void documents.refetch()}
        />
      ) : items.length === 0 ? (
        <div className="dash-empty">
          <h2>No documents</h2>
          <p className="dash-empty-hint">
            {filters.project || filters.q
              ? 'Nothing matches the current filters.'
              : 'Upload specs, runbooks or prior reports to make them available as run context.'}
          </p>
        </div>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table striped">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th className="num">Size</th>
                <th>Uploaded</th>
                <th className="ctx-actions-cell">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((doc) => (
                <tr key={doc.id} data-testid={`doc-row-${doc.id}`}>
                  <td className="strong">{doc.name}</td>
                  <td>
                    <span className="dash-context-chip">{doc.media_type}</span>
                  </td>
                  <td className="num ctx-num">{formatBytes(doc.size_bytes)}</td>
                  <td className="ctx-time" title={doc.created_at ?? undefined}>
                    {doc.created_at ? formatRelative(doc.created_at) : EM_DASH}
                  </td>
                  <td className="ctx-actions-cell">
                    {canMutateAudience(consumer, doc.project_id, doc.app_id) && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        aria-label={`Delete ${doc.name}`}
                        onClick={() => setDeleting(doc)}
                      >
                        Delete…
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {deleting && <DeleteDocumentModal doc={deleting} onClose={() => setDeleting(null)} />}
    </>
  )
}
