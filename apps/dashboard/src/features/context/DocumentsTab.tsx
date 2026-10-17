import { useTimestampPreference } from '@/theme/preferences'
import { TableToolbar } from '@/components/ux/TableToolbar'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
/**
 * Documents tab — filtered list (?project, q committed on submit) over
 * /v1/documents, multipart upload (D4's useUploadDocument reused) and
 * delete with confirm (operator+).
 */
import { Link } from 'react-router'
import { ProjectField } from '@/components/ux/ProjectField'
import { CopyableId } from '@/components/ux/CopyableId'
import { OverflowMenu } from '@/components/ux/OverflowMenu'
import { useEffect, useRef, useState, type FormEvent } from 'react'

import { useMutationState, useQueryClient } from '@tanstack/react-query'

import {
  documentUploadBatchMutationKey,
  useDeleteDocument,
  useDocument,
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
import { formatRelative, formatTimestamp } from '@/utils/time'

import { formatBytes } from './contextLogic'

const EM_DASH = '—'

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
  useTimestampPreference()
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
  const [selected, setSelected] = useState<DocumentOut | null>(null)
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
  const activeFilterCount = Object.values(activeFilters).filter(Boolean).length

  return (
    <>
      <form aria-label="Document filters" onSubmit={applyFilters}>
        <TableToolbar label="Document controls" className="ctx-toolbar glass-panel" filterCount={activeFilterCount} onClear={uploadingBatch ? undefined : () => { setProjectDraft(''); setAppDraft(''); setQDraft(''); setFilters({}) }}>
        <ProjectField label="Project" ariaLabel="Filter by project" value={projectDraft} disabled={uploadingBatch} onChange={changeProject} />
        {requiresApplication ? (
          <label className="ctx-filter-field">
            <span className="ctx-field-label">Application</span>
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
          </label>
        ) : (
          <label className="ctx-filter-field">
            <span className="ctx-field-label">Application</span>
            <input
              type="text"
              className="field-input"
              aria-label="Filter by application"
              placeholder="Optional"
              value={appDraft}
              disabled={uploadingBatch}
              onChange={(event) => setAppDraft(event.target.value)}
            />
          </label>
        )}
        <label className="ctx-filter-field ctx-grow">
          <span className="ctx-field-label">Search documents</span>
          <input
            type="search"
            className="field-input"
            aria-label="Search documents"
            placeholder="Search by name…"
            value={qDraft}
            onChange={(event) => setQDraft(event.target.value)}
          />
        </label>
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
        </TableToolbar>
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
        <Skeleton label="Loading documents…" />
      ) : documents.isError && !documents.data ? (
        <ProblemCard
          title="Documents unavailable"
          message={documents.error.message}
          onRetry={() => void documents.refetch()}
        />
      ) : items.length === 0 ? (
        <StateCard kind={activeFilterCount ? 'filtered' : 'empty'} title="No documents"
          action={activeFilterCount ? <button className="btn btn-secondary" disabled={uploadingBatch} onClick={() => { setProjectDraft(''); setAppDraft(''); setQDraft(''); setFilters({}) }}>Clear filters</button> : canUpload ? <button className="btn btn-secondary" onClick={() => inputRef.current?.click()}>Upload documents</button> : <Link to="/runs/new?step=context">Open test knowledge</Link>}>
          <p>{activeFilterCount ? 'Nothing matches the current filters.' : 'Upload specs, runbooks or prior reports to make them available as test knowledge.'}</p>
        </StateCard>
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
                  <td className="strong"><button type="button" className="btn btn-ghost" onClick={() => setSelected(doc)}>{doc.name}</button></td>
                  <td>
                    <span className="dash-context-chip">{doc.media_type}</span>
                  </td>
                  <td className="num ctx-num">{formatBytes(doc.size_bytes)}</td>
                  <td className="ctx-time" title={doc.created_at ?? undefined}>
                    {doc.created_at ? formatRelative(doc.created_at) : EM_DASH}
                  </td>
                  <td className="ctx-actions-cell">
                    <OverflowMenu label={`Document actions: ${doc.name}`} items={[
                      { label: 'View details', onSelect: () => setSelected(doc) },
                      ...(canMutateAudience(consumer, doc.project_id, doc.app_id) ? [{ label: 'Delete…', onSelect: () => setDeleting(doc) }] : []),
                    ]} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && <DocumentDetails doc={selected} onClose={() => setSelected(null)} />}
      {deleting && <DeleteDocumentModal doc={deleting} onClose={() => setDeleting(null)} />}
    </>
  )
}

function DocumentDetails({ doc, onClose }: { doc: DocumentOut; onClose: () => void }) {
  const detail = useDocument(doc.id)
  const current = detail.data ?? doc
  return <Dialog ariaLabel={`Document details: ${doc.name}`} className="ctx-modal glass-panel" overlayClassName="ctx-overlay" onClose={onClose}>
    <h2>{doc.name}</h2>
    <p>{current.media_type} · {formatBytes(current.size_bytes)} · Uploaded {formatTimestamp(current.created_at)}</p>
    <CopyableId value={doc.id} label="Document ID" />
    {detail.isPending && <p role="status">Loading document details…</p>}
    {detail.error && <p role="alert" className="tonal-card warning">{detail.error.message}<button type="button" className="btn btn-secondary" onClick={() => void detail.refetch()}>Retry</button></p>}
    {current.summary && <p>{current.summary}</p>}
    <h3>Text preview</h3>
    {current.text_preview ? <pre className="ctx-document-preview">{current.text_preview}</pre> : detail.isSuccess ? <p>No text preview is available for this document.</p> : null}
    {current.parse_error && <p className="tonal-card warning">{current.parse_error}</p>}
    <p>Attach this document in a new test’s Knowledge step to use it as source evidence.</p>
    <div className="ux-actions"><Link className="btn btn-secondary" to="/runs/new?step=context">Open test knowledge</Link><button className="btn btn-primary" onClick={onClose}>Close</button></div>
  </Dialog>
}
