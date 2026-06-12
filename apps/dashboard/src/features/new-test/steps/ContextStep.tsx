/**
 * Step 3 — Context (all optional): multipart document upload (file input +
 * drag-drop onto the dashed glass panel) and an existing-documents picker
 * scoped to the project. Selected ids persist in the draft; names/sizes for
 * chips come from upload results + the documents list.
 */
import { useRef, useState, type DragEvent } from 'react'

import { useDocumentsList, useUploadDocument, type DocumentOut } from '@/api/hooks/useDocuments'

import type { StepProps } from '../NewRunWizard'

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function ContextStep({ draft, onChange }: StepProps) {
  const upload = useUploadDocument()
  const documents = useDocumentsList(draft.scope.project_id.trim() || undefined)
  const inputRef = useRef<HTMLInputElement | null>(null)
  // Documents uploaded in this session (the list query may lag behind).
  const [uploaded, setUploaded] = useState<DocumentOut[]>([])
  const [dragOver, setDragOver] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)

  const known = new Map<string, DocumentOut>()
  for (const doc of documents.data ?? []) known.set(doc.id, doc)
  for (const doc of uploaded) known.set(doc.id, doc)

  function addDocument(id: string) {
    onChange((prev) =>
      prev.document_ids.includes(id)
        ? prev
        : { ...prev, document_ids: [...prev.document_ids, id] },
    )
  }

  function removeDocument(id: string) {
    onChange((prev) => ({
      ...prev,
      document_ids: prev.document_ids.filter((existing) => existing !== id),
    }))
  }

  async function uploadFiles(files: FileList | File[]) {
    setUploadError(null)
    for (const file of Array.from(files)) {
      try {
        const doc = await upload.mutateAsync({
          file,
          projectId: draft.scope.project_id.trim() || undefined,
        })
        setUploaded((prev) => [...prev, doc])
        addDocument(doc.id)
      } catch (error) {
        setUploadError(error instanceof Error ? error.message : `Upload of ${file.name} failed`)
      }
    }
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragOver(false)
    if (event.dataTransfer.files.length > 0) void uploadFiles(event.dataTransfer.files)
  }

  const pickerDocs = (documents.data ?? []).filter((doc) => !draft.document_ids.includes(doc.id))

  return (
    <section className="wizard-step" aria-label="Context">
      <p className="wizard-step-hint">
        Attach specs, runbooks or prior reports as context — or skip; documents are optional.
      </p>

      <div
        className={`glass-panel wizard-dropzone${dragOver ? ' wizard-dropzone--active' : ''}`}
        data-testid="document-dropzone"
        onDragOver={(event) => {
          event.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
      >
        <p className="wizard-dropzone-title">Drop files here</p>
        <p className="wizard-caption">or</p>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => inputRef.current?.click()}
        >
          Choose files
        </button>
        <input
          ref={inputRef}
          type="file"
          multiple
          aria-label="Upload documents"
          className="wizard-file-input"
          onChange={(event) => {
            if (event.target.files && event.target.files.length > 0) {
              void uploadFiles(event.target.files)
              event.target.value = ''
            }
          }}
        />
        {upload.isPending && <p className="wizard-caption">Uploading…</p>}
        {uploadError && (
          <p className="wizard-caption wizard-caption--danger" role="alert">
            {uploadError}
          </p>
        )}
      </div>

      {draft.document_ids.length > 0 && (
        <div className="wizard-field">
          <span className="wizard-label">Attached ({draft.document_ids.length})</span>
          <div className="wizard-chip-row" data-testid="attached-documents">
            {draft.document_ids.map((id) => {
              const doc = known.get(id)
              return (
                <span key={id} className="wizard-chip">
                  {doc ? `${doc.name} · ${formatBytes(doc.size_bytes)}` : id}
                  <button
                    type="button"
                    className="wizard-chip-remove"
                    aria-label={`Remove ${doc?.name ?? id}`}
                    onClick={() => removeDocument(id)}
                  >
                    ×
                  </button>
                </span>
              )
            })}
          </div>
        </div>
      )}

      <div className="wizard-field">
        <span className="wizard-label">Existing documents</span>
        {documents.isError ? (
          <p className="wizard-caption wizard-caption--danger">Documents failed to load</p>
        ) : pickerDocs.length === 0 ? (
          <p className="wizard-caption">
            {documents.isLoading ? 'Loading…' : 'No other documents in this project.'}
          </p>
        ) : (
          <ul className="wizard-doc-list">
            {pickerDocs.map((doc) => (
              <li key={doc.id} className="wizard-doc-row">
                <span className="wizard-doc-name">{doc.name}</span>
                <span className="wizard-caption">
                  {doc.media_type} · {formatBytes(doc.size_bytes)}
                </span>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => addDocument(doc.id)}
                >
                  Add
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
