/**
 * Documents tab — filtered list (?project, q committed on submit) over
 * /v1/documents, multipart upload (D4's useUploadDocument reused) and
 * delete with confirm (operator+).
 */
import { useEffect, useRef, useState, type FormEvent } from 'react'

import { useMutationState, useQueryClient } from '@tanstack/react-query'

import {
  documentUploadBatchMutationKey,
  useDeleteDocument,
  useDocumentUploadBatchOutcome,
  useDocumentsList,
  useUploadDocumentBatch,
  type DocumentOut,
  type UploadDocumentBatchInput,
} from '@/api/hooks/useDocuments'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { queryKeys } from '@/api/queryKeys'
import { useConsumer } from '@/auth/AuthProvider'
import { canMutateAudience } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
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

  const pendingBatches = useMutationState<UploadDocumentBatchInput | undefined>({
    filters: {
      exact: true,
      mutationKey: documentUploadBatchMutationKey(),
      status: 'pending',
    },
    select: (mutation) => mutation.state.variables as UploadDocumentBatchInput | undefined,
  })
  const pendingBatch = pendingBatches[0]
  const uploadOutcome = useDocumentUploadBatchOutcome()
  const restoredAudience = pendingBatch ?? uploadOutcome.data
  // Draft inputs commit on submit so the list query keys stay stable while typing.
  const [projectDraft, setProjectDraft] = useState(restoredAudience?.projectId ?? '')
  const [appDraft, setAppDraft] = useState(restoredAudience?.appId ?? '')
  const [qDraft, setQDraft] = useState(restoredAudience?.q ?? '')
  const [filters, setFilters] = useState<{
    project?: string
    app?: string
    q?: string
  }>(() =>
    restoredAudience
      ? {
          project: restoredAudience.projectId,
          app: restoredAudience.appId,
          q: restoredAudience.q,
        }
      : {},
  )

  const selectedProjectScopes = (consumer?.scopes ?? []).filter(
    (scope) => scope.project_id === projectDraft.trim(),
  )
  const hasProjectWideScope = selectedProjectScopes.some((scope) => !scope.app_id)
  const scopedAppIds = Array.from(
    new Set(
      selectedProjectScopes
        .map((scope) => scope.app_id?.trim())
        .filter((appId): appId is string => Boolean(appId)),
    ),
  ).sort()
  const requiresApplication =
    Boolean(consumer && consumer.scopes.length > 0) &&
    selectedProjectScopes.length > 0 &&
    !hasProjectWideScope

  const activeFilters = pendingBatch
    ? {
        project: pendingBatch.projectId,
        app: pendingBatch.appId,
        q: pendingBatch.q,
      }
    : filters
  const documents = useDocumentsList(
    activeFilters.project,
    activeFilters.q,
    activeFilters.app,
  )
  const upload = useUploadDocumentBatch()
  const queryClient = useQueryClient()
  const uploadCount = usePendingMutationCount(documentUploadBatchMutationKey())
  const uploadingBatch = uploadCount > 0
  const inputRef = useRef<HTMLInputElement | null>(null)
  const uploadingBatchRef = useRef(false)
  const [deleting, setDeleting] = useState<DocumentOut | null>(null)
  const uploadError = uploadOutcome.data?.errors.at(-1) ?? null
  const canUpload = canMutateAudience(
    consumer,
    projectDraft.trim() || null,
    appDraft.trim() || null,
  )

  useEffect(() => {
    if (!restoredAudience) return
    setProjectDraft(restoredAudience.projectId ?? '')
    setAppDraft(restoredAudience.appId ?? '')
    setQDraft(restoredAudience.q ?? '')
    setFilters({
      project: restoredAudience.projectId,
      app: restoredAudience.appId,
      q: restoredAudience.q,
    })
  }, [restoredAudience])

  function changeProject(projectId: string) {
    setProjectDraft(projectId)
    const nextScopes = (consumer?.scopes ?? []).filter(
      (scope) => scope.project_id === projectId.trim(),
    )
    const nextHasProjectWideScope = nextScopes.some((scope) => !scope.app_id)
    const nextAppIds = Array.from(
      new Set(
        nextScopes
          .map((scope) => scope.app_id?.trim())
          .filter((appId): appId is string => Boolean(appId)),
      ),
    )
    setAppDraft(
      !nextHasProjectWideScope && nextAppIds.length === 1 ? (nextAppIds[0] ?? '') : '',
    )
  }

  function applyFilters(event: FormEvent) {
    event.preventDefault()
    setFilters({
      project: projectDraft.trim() || undefined,
      app: appDraft.trim() || undefined,
      q: qDraft.trim() || undefined,
    })
  }

  async function uploadFiles(files: FileList) {
    if (uploadingBatchRef.current || uploadingBatch) return
    uploadingBatchRef.current = true
    const projectId = projectDraft.trim() || undefined
    const appId = appDraft.trim() || undefined
    const q = filters.q
    setFilters({ project: projectId, app: appId, q })
    try {
      // Upload follows the audience currently visible in the filter inputs,
      // even before the user presses Apply; never reuse the prior query key.
      await upload.mutateAsync({
        files: Array.from(files),
        projectId,
        appId,
        q,
      })
    } catch {
      // The mutation lifecycle publishes a durable, remount-safe outcome.
    } finally {
      uploadingBatchRef.current = false
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
          disabled={uploadingBatch}
          onChange={(event) => changeProject(event.target.value)}
        />
        {requiresApplication ? (
          <select
            className="field-select"
            aria-label="Filter by application"
            value={scopedAppIds.includes(appDraft) ? appDraft : ''}
            disabled={uploadingBatch}
            onChange={(event) => setAppDraft(event.target.value)}
          >
            <option value="" disabled>
              Select an authorized application…
            </option>
            {scopedAppIds.map((appId) => (
              <option key={appId} value={appId}>
                {appId}
              </option>
            ))}
          </select>
        ) : (
          <input
            type="text"
            className="field-input"
            aria-label="Filter by application"
            placeholder="Application (optional)"
            value={appDraft}
            disabled={uploadingBatch}
            onChange={(event) => setAppDraft(event.target.value)}
          />
        )}
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
              disabled={uploadingBatch}
              onClick={() => inputRef.current?.click()}
            >
              {uploadingBatch ? 'Uploading…' : 'Upload'}
            </button>
            <input
              ref={inputRef}
              type="file"
              multiple
              aria-label="Upload documents"
              className="ctx-file-input"
              disabled={uploadingBatch}
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
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() =>
              queryClient.setQueryData(queryKeys.documents.uploadOutcome(), null)
            }
          >
            Dismiss
          </button>
        </div>
      )}

      {documents.isError && documents.data && (
        <CachedDataWarning error={documents.error} onRetry={() => void documents.refetch()} />
      )}

      {documents.isPending ? (
        <div className="ctx-skeleton" role="status" aria-busy="true" aria-label="Loading documents">
          {Array.from({ length: SKELETON_ROWS }, (_, i) => (
            <div key={i} className="glass-panel ctx-skeleton-row" />
          ))}
        </div>
      ) : documents.isError && !documents.data ? (
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
